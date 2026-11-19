"""Figures in documents: pictures, screenshots, diagrams and charts. Reading a document collects them where they sit; `resolve` then decides which
ones carry information, has the image model describe them (with the text around them, so the description uses the document's own names), and puts a
labelled block in the text where each one sits:

    [Figure 3, page 4, described by AI: the Settings > AI screen; "Model that reads images" is highlighted ...]
    [Figure 4, slide 2: Quarterly revenue by region]            (the document's own caption or alt text, when no AI model describes it)
    [Chart 1, slide 5: Revenue 2026] followed by its values, read from the file itself

From there the text is a source like any other: passages, the source page, topics and claims are unchanged. A description is the model's words, not
the document's, which is why its block says so: a fact quoted from it visibly comes from a figure.

The images themselves are kept with the source (option: shown on the source page and handed to agents that can see images), except scanned pages,
which are only described: the figure cannot be cut out of a scan reliably.

Cheap checks come before any model call: a picture marked decorative, a tiny icon, a thin divider, or an image that repeats on many pages (a logo, a
page header) is skipped; a repeated screenshot is described once. Figures with a caption, or that the text points to, go first when the per-document
limit is reached. Files are untrusted: images are decoded by Pillow with a pixel limit and re-encoded, SVG is rendered without any external resource,
and nothing is fetched except, for a captured web page, its own images through the same address guard as web capture."""
import base64
import hashlib
import io
import re
import secrets
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

MAX_CANDIDATES = 400                 # figures looked at in one document; beyond that the rest are counted, not read
MAX_KEPT = 200                       # figures kept with one source
MAX_PIXELS = 40_000_000              # a larger image is refused before it is decoded (a decompression bomb)
MIN_SIDE = 48                        # px: smaller is an icon or a bullet
MIN_AREA = 12_000                    # px²
ICON_SIDE = 256                      # a web page's picture this small, in a few colours, is an icon
ICON_COLOURS = 16
MIN_DETAIL = 0.005                   # less than this share of detail: empty, a flat colour or a gradient
MAX_ASPECT = 9                       # wider or taller than this is a divider, a banner or a rule
REPEATS = 3                          # the same image in this many places is a logo or a page decoration
KEEP_SIDE = 1600                     # longest side of the image that is kept
SEND_SIDE = 1568                     # longest side of the image sent to the model
MAX_FETCH = 60                       # images downloaded for one web page
MAX_FETCH_BYTES = 10 * 1024 * 1024
CONTEXT_CHARS = 500                  # text before and after a figure given to the model (enough for names; more gives it material to invent from)
MAX_DESCRIPTION = 2500
EMU_PER_PX = 9525

RASTER = {'png', 'jpeg', 'jpg', 'gif', 'bmp', 'tiff', 'tif', 'webp'}
VECTOR_OFFICE = {'emf', 'wmf', 'x-emf', 'x-wmf'}
_MIME_FMT = {'image/png': 'png', 'image/jpeg': 'jpeg', 'image/jpg': 'jpeg', 'image/gif': 'gif', 'image/bmp': 'bmp', 'image/tiff': 'tiff', 'image/webp': 'webp',
             'image/svg+xml': 'svg', 'image/x-emf': 'emf', 'image/emf': 'emf', 'image/x-wmf': 'wmf', 'image/wmf': 'wmf'}
PLACEHOLDER_ALT = re.compile(r'^\s*(?:(?:image|picture|img|photo|graphic|figure|screenshot|grafik|immagine|bild|imagen)\s*[-_ ]?\d*|[\w-]+\.(?:png|jpe?g|gif|bmp|svg|webp|emf|wmf|tiff?)|'
                             r'a picture containing.*|description automatically generated.*|untitled|logo|icon|\W*)\s*$', re.I)
REFERENCE = re.compile(r'\b(?:fig(?:ure)?\.?|image|screenshot|screen ?shot|diagram|chart|graph|illustration|picture|shown (?:below|above|here)|see (?:below|above)|as shown|'
                       r'following (?:screen|image|figure|diagram)|abbildung|figura|immagine|schermata)\b', re.I)
CAPTION = re.compile(r'^\s*(?:fig(?:ure)?\.?|image|screenshot|diagram|chart|table|illustration|abbildung|figura|immagine|tabella)\s*\d+[\s.:\-–]', re.I)
NONE_ANSWER = re.compile(r'(?:^|\n)\W*none\W*$', re.I)               # "NONE", or a line of explanation and then NONE
SCAN_SPLIT = '[[FIGURES]]'


@dataclass
class Candidate:
    n: int
    kind: str                                 # image | svg | drawing (a PDF region) | slide (a slide drawn with shapes) | chart | scan (described while reading a scan)
    location: str | None = None               # 'Page 4', 'Slide 2'
    data: bytes | None = None                 # the image as stored in the file
    fmt: str | None = None                    # png, jpeg, svg, emf, wmf ...
    url: str | None = None                    # a web page's image, downloaded when the page is read
    web: bool = False                         # on a web page, where small pictures are icons (in a manual they may be a button worth showing)
    variables: dict | None = None             # the page's CSS custom properties, for an inline SVG that uses them
    render: object = None                     # () -> PNG bytes, for a region of a PDF page or a slide
    alt: str = ''
    caption: str = ''
    decorative: bool = False                  # the author marked it decorative
    width: int | None = None                  # displayed size in px, when the file says it
    height: int | None = None
    text: str = ''                            # a chart's values, read from the file
    description: str = ''                     # a scanned page's figures, already described while reading the page
    needs_converter: bool = False
    covers: list = field(default_factory=list)  # pictures a whole-slide figure already shows
    unsupported: str = ''                     # why it cannot be read at all ('shapes': a Word drawing made of shapes)


class Collector:
    """Figures found while a document is read. `add` returns the marker that holds the figure's place in the text."""

    def __init__(self):
        self.items = []
        self.token = secrets.token_hex(4)                 # a document cannot forge a marker: it does not know the token
        self.extra = 0                                    # figures beyond MAX_CANDIDATES
        self.cache = {}                                   # per-document state of the readers (a PowerPoint converted once, ...)

    def add(self, kind, **kw):
        if len(self.items) >= MAX_CANDIDATES:
            self.extra += 1
            return ''
        c = Candidate(len(self.items), kind, **kw)
        self.items.append(c)
        return self.marker(c.n)

    def marker(self, n):
        return f'\n\n⟦fig:{self.token}:{n}⟧\n\n'

    def pattern(self):
        return re.compile('⟦fig:' + self.token + r':(\d+)⟧')


@dataclass
class Options:
    """How figures are read for one document. Without `describe`, figures keep their caption or alt text only."""
    describe: object = None                   # (png bytes, context dict) -> description, or '' / 'NONE'
    max_described: int = 30
    converter: object = None                  # (filename, bytes) -> PDF bytes, the optional LibreOffice service
    fetch: object = None                      # (url) -> (content type, bytes), for a captured web page's images
    base_url: str | None = None
    title: str | None = None
    ground: object = None                     # (png bytes) -> the words read in it locally, to check a description against the picture


@dataclass
class Figure:
    label: str                                # 'Figure 3', 'Chart 1'
    n: int
    kind: str
    location: str | None
    png: bytes | None                         # the kept image (PNG or JPEG), None for a chart or a scanned page
    mime: str | None
    sha256: str | None
    width: int | None
    height: int | None
    caption: str
    alt: str
    description: str
    described_by: str | None                  # ai | alt | caption | data | None
    char_start: int = 0
    same_as: str | None = None


# ---------------------------------------------------------------- images
def _pil():
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    return Image


def fmt_of(content_type=None, name=None, data=None):
    if content_type:
        f = _MIME_FMT.get(content_type.split(';')[0].strip().lower())
        if f:
            return f
    if name and '.' in name:
        ext = name.rsplit('.', 1)[-1].lower()
        return {'jpg': 'jpeg', 'tif': 'tiff', 'x-emf': 'emf', 'x-wmf': 'wmf'}.get(ext, ext)
    if data:
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            return 'png'
        if data[:3] == b'\xff\xd8\xff':
            return 'jpeg'
        if data[:6] in (b'GIF87a', b'GIF89a'):
            return 'gif'
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            return 'webp'
        if data.lstrip()[:5] in (b'<?xml', b'<svg ') or b'<svg' in data[:300]:
            return 'svg'
    return None


def _open(data):
    """Decode an image with Pillow, refusing what is too large. -> RGB or RGBA image, or None."""
    Image = _pil()
    try:
        im = Image.open(io.BytesIO(data))
        if im.width * im.height > MAX_PIXELS:
            return None
        im.seek(0)
        im.load()
    except Exception:
        return None
    if im.mode not in ('RGB', 'RGBA'):
        im = im.convert('RGBA' if 'A' in im.getbands() or im.mode == 'P' and 'transparency' in im.info else 'RGB')
    return im


def _flatten(im):
    """A picture with transparency on the background it shows best against: white, unless the drawing is light and shows more on dark grey (a white
    icon on a white page shows nothing, and a model shown nothing makes something up from the text around it)."""
    if im.mode != 'RGBA':
        return im
    Image = _pil()
    alpha = im.getchannel('A')
    if alpha.getextrema()[0] >= 250:
        return im.convert('RGB')
    out = {}
    for name, colour in (('white', (255, 255, 255)), ('dark', (43, 43, 43))):
        bg = Image.new('RGB', im.size, colour)
        bg.paste(im, mask=alpha)
        out[name] = bg
    on_white = _residual(out['white'])
    return out['dark'] if on_white < 0.01 and _residual(out['dark']) > 2 * on_white + 0.002 else out['white']   # dark only when white hides it


def _residual(rgb):
    from PIL import ImageChops, ImageFilter
    g = rgb.convert('L')
    g.thumbnail((256, 256))
    hist = ImageChops.difference(g, g.filter(ImageFilter.GaussianBlur(3))).histogram()
    return sum(hist[24:]) / max(1, g.width * g.height)


def _detail(im):
    """Share of the picture that stands out from its blurred self (lines, letters, shapes): 0 for an empty frame, a flat colour or a gradient banner."""
    return _residual(_flatten(im))


def _colours(im):
    """How many colours really make up the picture (each covering at least half a percent of it): an icon has a handful."""
    small = _flatten(im).convert('RGB').resize((64, 64))
    small = small.quantize(colors=64).convert('RGB')
    return len([c for c in (small.getcolors(4096) or []) if c[0] >= 20])


def _encode(im, side):
    """-> (bytes, mime). Screenshots and drawings stay PNG (sharp text); photographs become JPEG (a tenth of the size)."""
    im = _flatten(im)
    if max(im.size) > side:
        im = im.copy()
        im.thumbnail((side, side))
    small = im.copy()
    small.thumbnail((96, 96))
    colours = small.getcolors(4096)
    buf = io.BytesIO()
    if colours is None or len(colours) > 1500:
        im.save(buf, 'JPEG', quality=85, optimize=True)
        return buf.getvalue(), 'image/jpeg'
    im.save(buf, 'PNG', optimize=True)
    return buf.getvalue(), 'image/png'


def _trim(im, pad=8):
    """A rendered page or file reduced to what is drawn on it (white margins cut)."""
    from PIL import ImageChops
    Image = _pil()
    rgb = _flatten(im)
    diff = ImageChops.difference(rgb, Image.new('RGB', rgb.size, 'white')).convert('L').point(lambda v: 255 if v > 12 else 0)
    box = diff.getbbox()
    if not box:
        return None
    l, t, r, b = box
    return rgb.crop((max(0, l - pad), max(0, t - pad), min(rgb.width, r + pad), min(rgb.height, b + pad)))


_SVG_EXTERNAL = re.compile(r'''(?:xlink:)?href\s*=\s*["'](?!#|data:image/)[^"']*["']''', re.I)
_SVG_IMPORT = re.compile(r'''@import[^;]+;|url\(\s*["']?(?!#|data:)[^)]*\)''', re.I)


_VAR = re.compile(r'var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*(?:\([^()]*\))?[^()]*))?\)')


def css_vars(css):
    """The page's own colours and sizes: the custom properties of its plain `:root { ... }` rules (the light theme, as the page first shows)."""
    out = {}
    for m in re.finditer(r'(?:^|[;}])\s*(?:/\*.*?\*/\s*)*:root\s*\{([^}]*)\}', css or '', re.S):
        for name, val in re.findall(r'(--[\w-]+)\s*:\s*([^;]+)', m.group(1)):
            out.setdefault(name, val.strip())
    return out


def apply_vars(text, values):
    """var(--x) -> its value (or the fallback written after it): the SVG renderer knows no CSS custom properties, and draws them black."""
    for _ in range(4):
        new = _VAR.sub(lambda m: values.get(m.group(1)) or (m.group(2) or '').strip() or 'currentColor', text)
        if new == text:
            break
        text = new
    return text


def svg_to_png(svg, size=None, variables=None):
    """An SVG rendered on this server. External references (other files, web addresses) are removed first: rendering must not read anything else.
    size: a list that receives the drawing's own size (before a small drawing is drawn larger)."""
    try:
        import resvg_py
    except ImportError:
        return None
    text = svg.decode('utf-8', 'replace') if isinstance(svg, bytes) else svg
    if len(text) > 5_000_000 or '<!ENTITY' in text:
        return None
    text = _SVG_IMPORT.sub('', _SVG_EXTERNAL.sub('', text))
    if 'var(' in text:
        text = apply_vars(text, variables or {})
    fonts = {'sans_serif_family': 'DejaVu Sans', 'font_family': 'DejaVu Sans'}         # web fonts are not on the server: draw labels in a font that is
    try:
        png = bytes(resvg_py.svg_to_bytes(svg_string=text, skip_system_fonts=False, **fonts))          # transparent: _flatten picks the background
    except Exception:
        return None
    im = _open(png)
    if im is None:
        return None
    if size is not None:
        size[:] = list(im.size)
    if max(im.size) < 400:                                  # an SVG has no pixels of its own: draw a small drawing larger, so its labels can be read
        try:
            png = bytes(resvg_py.svg_to_bytes(svg_string=text, zoom=min(4.0, 800 / max(1, max(im.size))), **fonts))
        except Exception:
            pass
    return png


def pdf_page_png(pdf, index=0, scale=2.0, crop=None, trim=False):
    """One page of a PDF (bytes or an open pypdfium2 document) as a PNG; crop = (left, bottom, right, top) in points to keep."""
    import pypdfium2
    doc = pypdfium2.PdfDocument(pdf) if isinstance(pdf, (bytes, bytearray)) else pdf
    try:
        page = doc[index]
        w, h = page.get_size()
        cut = (0, 0, 0, 0)
        if crop:
            l, b, r, t = crop
            cut = (max(0, l), max(0, b), max(0, w - r), max(0, h - t))
        im = page.render(scale=scale, crop=cut).to_pil()
    finally:
        if doc is not pdf:
            doc.close()
    if trim:
        im = _trim(im)
        if im is None:
            return None
    buf = io.BytesIO()
    _flatten(im).save(buf, 'PNG')
    return buf.getvalue()


def to_png(c, opts, notes):
    """The candidate's image as PNG bytes, or None (with c.needs_converter set when only the converter could draw it)."""
    if c.render is not None:
        try:
            return c.render()
        except NeedsConverter:
            c.needs_converter = True
            return None
        except Exception as e:
            notes.append(f'a figure could not be drawn: {type(e).__name__}')
            return None
    data, fmt = c.data, c.fmt
    if data is None and c.url:
        if not opts.fetch:
            return None
        try:
            ctype, data = opts.fetch(c.url)
        except Exception:
            return None
        fmt = fmt_of(ctype, urlsplit(c.url).path, data)
    if data is None:
        return None
    fmt = fmt or fmt_of(None, None, data)
    if fmt == 'svg':
        own = []
        out = svg_to_png(data, own, c.variables)
        if own and not (c.width and c.height):
            c.width, c.height = own                                   # its own size, for telling an icon from a drawing
        return out
    if fmt in VECTOR_OFFICE:
        if not opts.converter:
            c.needs_converter = True
            return None
        try:
            pdf = opts.converter(f'figure.{fmt.replace("x-", "")}', data)
            return pdf_page_png(pdf, 0, scale=2.0, trim=True)
        except Exception as e:
            notes.append(f'the document converter could not draw a figure: {type(e).__name__}')
            return None
    im = _open(data)
    if im is None:
        return None
    buf = io.BytesIO()
    im.save(buf, 'PNG')                                                 # transparency kept: _flatten decides the background later
    return buf.getvalue()


class NeedsConverter(Exception):
    pass


# ---------------------------------------------------------------- context, filters, blocks
_HEADING = re.compile(r'^#{1,6} (.+)$', re.M)


def _context(text, pos, location, opts, c):
    before = text[max(0, pos - CONTEXT_CHARS * 3):pos]
    heads = _HEADING.findall(before)
    clean = lambda s: re.sub(r'⟦fig:[0-9a-f]+:\d+⟧', ' ', s)
    return {'title': opts.title or '', 'location': location or '', 'section': heads[-1] if heads else '', 'caption': c.caption, 'alt': '' if PLACEHOLDER_ALT.match(c.alt or '') else c.alt,
            'before': clean(before)[-CONTEXT_CHARS:].strip(), 'after': clean(text[pos:pos + CONTEXT_CHARS * 2])[:CONTEXT_CHARS].strip()}


def _where(location):
    return f', {location[0].lower()}{location[1:]}' if location else ''


def _one_line(s, limit):
    s = re.sub(r'\s*\n\s*', ' ', (s or '').strip())
    s = s.replace('[', '(').replace(']', ')')
    return s if len(s) <= limit else s[:limit].rsplit(' ', 1)[0] + ' ...'


def block(fig):
    where = _where(fig.location)
    if fig.same_as:
        return f'[{fig.label}{where}: the same image as {fig.same_as}]'
    if fig.kind == 'chart':
        head = f'[{fig.label}{where}' + (f': {_one_line(fig.caption, 300)}]' if fig.caption else ']')
        return head + ('\n' + fig.description if fig.description else '')
    cap = _one_line(fig.caption, 300)
    if fig.described_by == 'ai':
        desc = (fig.description or '').strip().replace('[', '(').replace(']', ')')
        return f'[{fig.label}{where}' + (f': "{cap}"' if cap else '') + f', described by AI: {desc}]'
    text = cap or _one_line(fig.alt, 600)
    return f'[{fig.label}{where}: {text}]' if text else ''


def _informative_alt(c):
    return '' if PLACEHOLDER_ALT.match(c.alt or '') else (c.alt or '').strip()


def resolve(text, col, opts, notes):
    """Text with markers -> (text with figure blocks, [Figure], stats). Skipped figures leave nothing in the text."""
    pat = col.pattern()
    stats = {'found': len(col.items) + col.extra, 'described': 0, 'from_text': 0, 'charts': 0, 'kept': 0, 'decorative': 0, 'repeated': 0, 'small': 0,
             'over_limit': 0, 'needs_converter': 0, 'unreadable': 0, 'not_supported': 0, 'not_informative': 0, 'scans_described': 0}
    if col.extra:
        notes.append(f'{col.extra} more figure(s) were not looked at: a document is read up to {MAX_CANDIDATES}')
    if not col.items:
        return pat.sub('', text), [], stats
    pos_of = {int(m.group(1)): m.start() for m in pat.finditer(text)}
    items = sorted((c for c in col.items if c.n in pos_of), key=lambda c: pos_of[c.n])     # document order (a slide's own figure comes before its pictures)
    covered, textonly = set(), set()
    imgs, sha_places = {}, {}
    for c in items:                                                             # 1. what can be drawn, and cheap checks
        if c.kind in ('chart', 'scan') or c.unsupported:
            continue
        if c.decorative:
            stats['decorative'] += 1
            continue
        if c.width and c.height and (min(c.width, c.height) < MIN_SIDE or c.width * c.height < MIN_AREA or max(c.width, c.height) / max(1, min(c.width, c.height)) > MAX_ASPECT):
            stats['small'] += 1                                                 # known small from the file: not even decoded or downloaded
            continue
        if c.n in covered:
            continue
        png = to_png(c, opts, notes)
        if png is None:
            stats['needs_converter' if c.needs_converter else 'unreadable'] += 1
            if c.caption or _informative_alt(c):
                textonly.add(c.n)                                               # no image, but the document says what it is: that much stays in the text
            continue
        im = _open(png)
        if im is None:
            stats['unreadable'] += 1
            continue
        w, h = im.size
        if min(w, h) < MIN_SIDE or w * h < MIN_AREA or max(w, h) / max(1, min(w, h)) > MAX_ASPECT:
            stats['small'] += 1
            continue
        if _detail(im) < MIN_DETAIL:
            stats['decorative'] += 1                                            # empty, a flat colour or a gradient: nothing for a model to see
            continue
        if c.web and max(c.width or w, c.height or h) <= ICON_SIDE and _colours(im) <= ICON_COLOURS:
            stats['decorative'] += 1                                            # an icon
            continue
        sha = hashlib.sha256(png).hexdigest()
        imgs[c.n] = (im, sha)
        sha_places.setdefault(sha, set()).add(c.location or f'#{c.n}')
        if c.kind == 'slide':
            covered.update(c.covers)                                            # the slide is drawn whole: its pictures need no figure of their own
    for n in list(imgs):
        if n in covered:
            del imgs[n]
    stats['found'] -= len({c.n for c in items if c.n in covered})                 # shown inside a slide that is a figure of its own
    repeated = {s for s, places in sha_places.items() if len(places) >= REPEATS}
    for n in [n for n, (_, s) in imgs.items() if s in repeated]:
        stats['repeated'] += 1
        del imgs[n]

    keep = [c for c in items if c.kind in ('chart', 'scan') or c.n in imgs or (c.n in textonly and c.n not in covered)]   # 2. numbering, in document order
    for c in items:
        if c.unsupported:
            stats['not_supported'] += 1
    budget = opts.max_described if opts.describe else 0
    seen_sha, wanted = {}, []
    for c in keep:
        if c.n in imgs:
            s = imgs[c.n][1]
            if s not in seen_sha:
                seen_sha[s] = c.n
                wanted.append(c)
    ctx = {c.n: _context(text, pos_of[c.n], c.location, opts, c) for c in wanted}
    first = [c for c in wanted if c.caption or REFERENCE.search(ctx[c.n]['before'][-300:] + ' ' + ctx[c.n]['after'][:300])]
    rest = sorted((c for c in wanted if c not in first), key=lambda c: -(imgs[c.n][0].width * imgs[c.n][0].height))
    to_describe = {c.n for c in (first + rest)[:budget]}
    stats['over_limit'] = max(0, len(wanted) - budget) if opts.describe else 0

    figs, by_n, label_of_sha, counters = [], {}, {}, {'Figure': 0, 'Chart': 0}
    errors, dropped = [], set()
    for c in keep:
        if c.kind == 'chart':
            counters['Chart'] += 1
            f = Figure(f"Chart {counters['Chart']}", counters['Chart'], 'chart', c.location, None, None, None, None, None, c.caption, c.alt, c.text, 'data')
            stats['charts'] += 1
        elif c.kind == 'scan':
            counters['Figure'] += 1
            f = Figure(f"Figure {counters['Figure']}", counters['Figure'], 'scan', c.location, None, None, None, None, None, '', '', c.description, 'ai')
            stats['scans_described'] += 1
        elif c.n in textonly:
            counters['Figure'] += 1
            f = Figure(f"Figure {counters['Figure']}", counters['Figure'], c.kind, c.location, None, None, None, None, None, c.caption, _informative_alt(c), '',
                       'caption' if c.caption else 'alt')
            stats['from_text'] += 1
        else:
            im, sha = imgs[c.n]
            if sha in dropped:
                continue
            if sha in label_of_sha and seen_sha.get(sha) != c.n:
                counters['Figure'] += 1
                first_fig = label_of_sha[sha]
                f = Figure(f"Figure {counters['Figure']}", counters['Figure'], c.kind, c.location, first_fig.png, first_fig.mime, sha, first_fig.width, first_fig.height,
                           c.caption, c.alt, first_fig.description, first_fig.described_by, same_as=first_fig.label)
            else:
                counters['Figure'] += 1
                data, mime = _encode(im, KEEP_SIDE)
                desc, by = '', None
                if c.n in to_describe:
                    try:
                        send, smime = _encode(im, SEND_SIDE)
                        desc = (opts.describe(send, smime, ctx[c.n]) or '').strip()
                    except Exception as e:
                        errors.append(f'{type(e).__name__}: {str(e)[:120]}')
                        desc = ''
                    if desc and not NONE_ANSWER.search(desc) and opts.ground is not None and not grounded(desc, send, opts.ground):
                        stats['ungrounded'] = stats.get('ungrounded', 0) + 1       # it quotes labels the picture does not show: made up from the text around it
                        desc = 'NONE'
                    if desc and NONE_ANSWER.search(desc):
                        stats['not_informative'] += 1                          # the model saw nothing worth saying: kept out of the text, and not kept
                        counters['Figure'] -= 1
                        dropped.add(sha)
                        continue
                    if desc:
                        desc, by = desc[:MAX_DESCRIPTION], 'ai'
                        stats['described'] += 1
                if not by:
                    if c.caption:
                        by = 'caption'
                    elif _informative_alt(c):
                        by = 'alt'
                    if by:
                        stats['from_text'] += 1
                kw, kh = _pil().open(io.BytesIO(data)).size                        # the size of the image as kept
                f = Figure(f"Figure {counters['Figure']}", counters['Figure'], c.kind, c.location, data, mime, sha, kw, kh, c.caption, _informative_alt(c), desc, by)
                label_of_sha[sha] = f
        by_n[c.n] = f
        figs.append(f)
    if errors:
        notes.append(f'the image model failed on {len(errors)} figure(s), which keep their caption or alt text: {errors[0]}')

    out, last = [], 0                                                          # 3. the blocks in place of the markers
    for m in pat.finditer(text):
        out.append(text[last:m.start()])
        f = by_n.get(int(m.group(1)))
        if f is not None:
            b = block(f)
            f.char_start = sum(len(x) for x in out)
            out.append(b)
        last = m.end()
    out.append(text[last:])
    joined = ''.join(out)
    final = re.sub(r'\n{3,}', '\n\n', joined).strip()
    if final != joined:                                                        # collapsing blank lines moved the blocks: find them again
        at = 0
        for f in figs:
            b = block(f)
            if b:
                i = final.find(b, at)
                if i >= 0:
                    f.char_start, at = i, i + len(b)
    kept = [f for f in figs if f.png is not None][:MAX_KEPT]
    stats['kept'] = len(kept)
    figs = [f for f in figs if f.png is None or f in kept]
    return final, figs, stats


_QUOTED = re.compile(r'["\u201c\u00ab]([^"\u201d\u00bb]{2,80})["\u201d\u00bb]')


def _words(text):
    return set(re.findall(r'[^\W_]{2,}', (text or '').lower()))


def grounded(desc, image, ground):
    """Does the picture show the labels the description quotes? Read locally (Tesseract); a description that quotes several labels of which almost none
    can be found in the picture was made up from the text around it. A description quoting nothing, or a reader that fails, is not judged."""
    quotes = [q for q in _QUOTED.findall(desc) if _words(q)]
    if len(quotes) < 3:
        return True
    try:
        seen = _words(ground(image))
    except Exception:
        return True
    found = sum(1 for q in quotes if len(_words(q) & seen) >= max(1, len(_words(q)) // 2))
    return found >= max(1, len(quotes) // 4)


def summary(stats):
    """One line for the source's notes, or None when the document has no figures."""
    if not stats or not stats.get('found'):
        return None
    parts = []
    if stats['described']:
        parts.append(f"{stats['described']} described by AI")
    if stats['scans_described']:
        parts.append(f"figures described on {stats['scans_described']} scanned page(s)")
    if stats['from_text']:
        parts.append(f"{stats['from_text']} with their caption or alt text only")
    if stats['charts']:
        parts.append(f"{stats['charts']} chart(s) read from their data")
    skipped = stats['decorative'] + stats['repeated'] + stats['small'] + stats['not_informative'] - stats.get('ungrounded', 0)
    if skipped:
        parts.append(f'{skipped} skipped as decorative, repeated or too small')
    if stats.get('ungrounded'):
        parts.append(f"{stats['ungrounded']} left out: their description named things the picture does not show")
    if stats['over_limit']:
        parts.append(f"{stats['over_limit']} over the limit of figures described per document")
    if stats['needs_converter']:
        parts.append(f"{stats['needs_converter']} need the document converter")
    if stats['not_supported']:
        parts.append(f"{stats['not_supported']} drawn with shapes in Word, not read")
    if stats['unreadable']:
        parts.append(f"{stats['unreadable']} could not be read")
    return f"{stats['found']} figure(s): " + ', '.join(parts) if parts else None


# ---------------------------------------------------------------- charts (Word and PowerPoint keep their values)
C = 'http://schemas.openxmlformats.org/drawingml/2006/chart'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'


def _pts(el):
    if el is None:
        return {}
    out = {}
    for pt in el.iter(f'{{{C}}}pt'):
        v = pt.find(f'{{{C}}}v')
        if v is not None and v.text is not None:
            out[int(pt.get('idx', len(out)))] = v.text.strip()
    return out


def chart_text(chart_space):
    """A chart's XML (lxml element or bytes) -> (title, lines): its values as table rows, the way table_lines writes tables."""
    from lxml import etree
    from .extract import table_lines
    root = etree.fromstring(chart_space) if isinstance(chart_space, (bytes, bytearray)) else chart_space
    chart = root.find(f'{{{C}}}chart')
    if chart is None:
        return '', []
    t = chart.find(f'{{{C}}}title')
    title = ' '.join(x.text for x in t.iter(f'{{{A}}}t') if x.text).strip() if t is not None else ''
    if not title and t is not None:
        title = ' '.join(_pts(t).values())
    series, cats = [], {}
    for ser in chart.iter(f'{{{C}}}ser'):
        tx = ser.find(f'{{{C}}}tx')
        name = ' '.join(_pts(tx).values()) if tx is not None else ''
        if not name and tx is not None:
            v = tx.find(f'{{{C}}}v')
            name = v.text if v is not None else ''
        cat = ser.find(f'{{{C}}}cat') if ser.find(f'{{{C}}}cat') is not None else ser.find(f'{{{C}}}xVal')
        val = ser.find(f'{{{C}}}val') if ser.find(f'{{{C}}}val') is not None else ser.find(f'{{{C}}}yVal')
        c, v = _pts(cat), _pts(val)
        cats.update({k: x for k, x in c.items() if k not in cats})
        series.append((name or f'Series {len(series) + 1}', v))
    if not series:
        return title, []
    idx = sorted(set(cats) | {k for _, v in series for k in v})
    rows = [['Category'] + [s for s, _ in series]] + [[cats.get(i, str(i + 1))] + [v.get(i, '') for _, v in series] for i in idx[:500]]
    return title, table_lines(rows)


# ---------------------------------------------------------------- Office files
NS = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main', 'wp': 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing',
      'a': A, 'pic': 'http://schemas.openxmlformats.org/drawingml/2006/picture', 'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
      'c': C, 'v': 'urn:schemas-microsoft-com:vml', 'mc': 'http://schemas.openxmlformats.org/markup-compatibility/2006',
      'p': 'http://schemas.openxmlformats.org/presentationml/2006/main', 'adec': 'http://schemas.microsoft.com/office/drawing/2017/decorative'}
URI_PIC = 'http://schemas.openxmlformats.org/drawingml/2006/picture'
URI_CHART = 'http://schemas.openxmlformats.org/drawingml/2006/chart'
URI_DIAGRAM = 'http://schemas.openxmlformats.org/drawingml/2006/diagram'


def _decorative(props):
    return props is not None and any(d.get('val') in ('1', 'true') for d in props.iter(f'{{{NS["adec"]}}}decorative'))


def _in_fallback(el):
    p = el.getparent()
    while p is not None:
        if p.tag == f'{{{NS["mc"]}}}Fallback':
            return True
        p = p.getparent()
    return False


def _related(part, rid):
    """The part a relationship id points to (python-docx and python-pptx name the lookup differently)."""
    if not rid:
        return None
    try:
        return part.related_parts[rid] if hasattr(part, 'related_parts') else part.related_part(rid)
    except (KeyError, AttributeError):
        return None


def docx_figures(element, part, col, caption=''):
    """The figures inside one Word paragraph or table (its XML element), in order, as markers. caption: the next paragraph, when it is a caption."""
    out = []
    for el in element.iter(f'{{{NS["w"]}}}drawing', f'{{{NS["w"]}}}pict'):
        if _in_fallback(el):
            continue                                                            # the same picture again, for old readers
        if el.tag.endswith('}pict'):
            for img in el.iter(f'{{{NS["v"]}}}imagedata'):
                p = _related(part, img.get(f'{{{NS["r"]}}}id'))
                if p is not None:
                    out.append(col.add('image', data=p.blob, fmt=fmt_of(p.content_type, getattr(p, 'partname', None)), alt=img.get('title') or '', caption=caption))
            continue
        props = el.find('.//wp:docPr', NS)
        alt = ((props.get('descr') or props.get('title') or '') if props is not None else '').strip()
        ext = el.find('.//wp:extent', NS)
        w, h = ((int(ext.get('cx', 0)) // EMU_PER_PX, int(ext.get('cy', 0)) // EMU_PER_PX) if ext is not None else (None, None))
        gd = el.find('.//a:graphicData', NS)
        uri = gd.get('uri') if gd is not None else ''
        if uri == URI_CHART:
            ch = gd.find('c:chart', NS)
            p = _related(part, ch.get(f'{{{NS["r"]}}}id')) if ch is not None else None
            if p is not None:
                try:
                    title, lines = chart_text(p.blob)
                except Exception:
                    title, lines = '', []
                if lines:
                    out.append(col.add('chart', text='\n'.join(lines), caption=title or caption, alt=alt))
            continue
        if uri == URI_DIAGRAM:
            out.append(col.add('image', unsupported='smartart', alt=alt))
            continue
        blips = [b for b in el.iter(f'{{{NS["a"]}}}blip') if b.get(f'{{{NS["r"]}}}embed')]
        if not blips:
            out.append(col.add('image', unsupported='shapes', alt=alt))
            continue
        for b in blips[:20]:
            p = _related(part, b.get(f'{{{NS["r"]}}}embed'))
            if p is None:
                continue
            out.append(col.add('image', data=p.blob, fmt=fmt_of(p.content_type, str(getattr(p, 'partname', ''))), alt=alt, caption=caption,
                               decorative=_decorative(props), width=w if len(blips) == 1 else None, height=h if len(blips) == 1 else None))
    return ''.join(out)


def _cnv(el):
    return el.find('.//p:cNvPr', NS)


def pptx_slide_figures(slide, index, col, pptx_bytes, opts):
    """The figures of one slide, as markers: its pictures and charts, and the whole slide when it is a diagram built from shapes."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    out, pics, shapes, smart, lines = [], [], 0, 0, 0
    where = f'Slide {index}'

    def walk(container):
        nonlocal shapes, smart, lines
        for sh in container:
            st = getattr(sh, 'shape_type', None)
            if st == MSO_SHAPE_TYPE.GROUP:
                walk(sh.shapes)
                continue
            props = _cnv(sh._element)
            alt = ((props.get('descr') or props.get('title') or '') if props is not None else '').strip()
            if getattr(sh, 'has_chart', False) and sh.has_chart:
                try:
                    title, rows = chart_text(sh.chart._chartSpace)
                except Exception:
                    title, rows = '', []
                if rows:
                    out.append(col.add('chart', location=where, text='\n'.join(rows), caption=title, alt=alt))
                continue
            gd = sh._element.find('.//a:graphicData', NS)
            if gd is not None and gd.get('uri') == URI_DIAGRAM:
                smart += 1
                continue
            blip = sh._element.find('.//a:blip', NS)
            if blip is not None and blip.get(f'{{{NS["r"]}}}embed') and (st in (MSO_SHAPE_TYPE.PICTURE, MSO_SHAPE_TYPE.PLACEHOLDER) or sh._element.tag.endswith('}pic')):
                p = _related(slide.part, blip.get(f'{{{NS["r"]}}}embed'))
                if p is not None:
                    w = int(sh.width or 0) // EMU_PER_PX or None
                    h = int(sh.height or 0) // EMU_PER_PX or None
                    pics.append(len(col.items))
                    out.append(col.add('image', location=where, data=p.blob, fmt=fmt_of(p.content_type, str(getattr(p, 'partname', ''))), alt=alt,
                                       decorative=_decorative(props), width=w, height=h))
                continue
            if sh._element.tag.endswith('}cxnSp') or st == MSO_SHAPE_TYPE.LINE:
                lines += 1
            elif st in (MSO_SHAPE_TYPE.AUTO_SHAPE, MSO_SHAPE_TYPE.FREEFORM) and not getattr(sh, 'is_placeholder', False):
                shapes += 1

    walk(slide.shapes)
    if smart or (lines and shapes >= 2) or shapes >= 4:                         # a diagram made of shapes: only the slide as a whole shows it
        render = None
        page = col.cache.get('pdf_page_of_slide', {}).get(index)
        if opts and opts.converter and pptx_bytes is not None and page is not None:
            def render(p=page):
                pdf = col.cache.get('pptx_pdf')
                if pdf is None:
                    pdf = col.cache['pptx_pdf'] = opts.converter('slides.pptx', pptx_bytes)
                import pypdfium2
                doc = pypdfium2.PdfDocument(pdf)
                try:
                    if len(doc) != col.cache.get('shown_slides', len(doc)):
                        raise RuntimeError('the converted file has a different number of slides')
                    return pdf_page_png(doc, p, scale=1.6)
                finally:
                    doc.close()
        elif page is None and opts and opts.converter:
            render = None                                                        # a hidden slide is not printed: its pictures stand on their own
        else:
            def render():
                raise NeedsConverter()
        if render is not None:
            out.insert(0, col.add('slide', location=where, render=render, covers=list(pics)))
    return ''.join(out)


# ---------------------------------------------------------------- PDF pages
_IMG, _PATH, _TEXT, _FORM = 3, 2, 1, 5                                          # FPDF_PAGEOBJ_IMAGE, _PATH, _TEXT, _FORM
MAX_PATHS = 1500
LABEL_REACH = 36                                                                 # pt around a drawing where its labels sit


def _segments_curvy(obj):
    """Has a path a curve or a slanted line? (Tables and underlines are only straight horizontal and vertical lines.)"""
    import pypdfium2.raw as R
    n = R.FPDFPath_CountSegments(obj)
    if n <= 0 or n > 5000:
        return n > 5000
    prev = None
    import ctypes
    x, y = ctypes.c_float(), ctypes.c_float()
    for i in range(n):
        seg = R.FPDFPath_GetPathSegment(obj, i)
        t = R.FPDFPathSegment_GetType(seg)
        if t == R.FPDF_SEGMENT_BEZIERTO:
            return True
        R.FPDFPathSegment_GetPoint(seg, x, y)
        pt = (x.value, y.value)
        if t == R.FPDF_SEGMENT_LINETO and prev is not None and abs(pt[0] - prev[0]) > 0.5 and abs(pt[1] - prev[1]) > 0.5:
            return True
        prev = pt
    return False


def _graphic(obj, w, h):
    """Does a path draw something (a curve, a slanted line, or a coloured filled shape such as a bar), rather than a rule or a table border?"""
    if _segments_curvy(obj):
        return True
    import ctypes
    import pypdfium2.raw as R
    mode, stroke = ctypes.c_int(), ctypes.c_int()
    if not R.FPDFPath_GetDrawMode(obj, mode, stroke) or mode.value == 0 or min(w, h) < 3 or max(w, h) / max(min(w, h), 0.1) > 15:
        return False                                                            # not filled, or a thin band (a rule, a shaded table row)
    r, g, b, a = (ctypes.c_uint() for _ in range(4))
    if not R.FPDFPageObj_GetFillColor(obj, r, g, b, a):
        return True
    return a.value > 0 and min(r.value, g.value, b.value) < 235                   # white or transparent fills are backgrounds


def _merge(boxes, gap):
    boxes = [list(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        out = []
        for b in boxes:
            for o in out:
                if b[0] <= o[2] + gap and o[0] <= b[2] + gap and b[1] <= o[3] + gap and o[1] <= b[3] + gap:
                    o[0], o[1], o[2], o[3] = min(o[0], b[0]), min(o[1], b[1]), max(o[2], b[2]), max(o[3], b[3])
                    o[4] += b[4]
                    o[5] += b[5]
                    changed = True
                    break
            else:
                out.append(b)
        boxes = out
    return boxes


def pdf_page_figures(doc, index, col):
    """The figures of one PDF page, top to bottom, as markers: each picture as it is shown, and each drawing (a diagram or a chart drawn with lines and
    curves) as the region it covers. A picture that fills the page is a scan or a background, and is left to OCR."""
    import pypdfium2
    page = doc[index]
    pw, ph = page.get_size()
    area = pw * ph
    where = f'Page {index + 1}'
    found, paths, labels = [], [], []
    for obj in page.get_objects(max_depth=0):                                    # page-level objects: their bounds are on the page
        try:
            l, b, r, t = obj.get_bounds()
        except Exception:
            continue
        l, b, r, t = max(0, l), max(0, b), min(pw, r), min(ph, t)
        if r - l <= 0 or t - b <= 0:
            continue
        kind, curvy = obj.type, False
        if kind == _TEXT:
            if t - b < 24:
                labels.append((l, b, r, t))                                      # a line of text: maybe an axis or a box label of a drawing
            continue
        if kind == _FORM:                                                        # a group (Office exports wrap pictures in them): judged by what it holds
            inner = [o.type for o in page.get_objects(max_depth=3, form=obj)]
            if _IMG in inner:
                kind = _IMG
            elif _PATH in inner:
                kind, curvy = _PATH, any(_segments_curvy(o.raw) for o in page.get_objects(filter=[_PATH], max_depth=3, form=obj))   # noqa: E501
            else:
                continue
        if kind == _IMG:
            if (r - l) * (t - b) > 0.85 * area:
                continue                                                         # a scan or a page background: OCR's business
            found.append([l, b, r, t])
        elif kind == _PATH:
            if (r - l) * (t - b) > 0.8 * area:
                continue                                                         # a page frame or background
            if len(paths) < MAX_PATHS:
                paths.append([l, b, r, t, 1, int(curvy or (obj.type == _PATH and _graphic(obj.raw, r - l, t - b)))])
            else:
                paths.append([l, b, r, t, 1, 1])                                 # a dense drawing (a map, a detailed chart): no need to look at every curve
    if len(paths) > MAX_PATHS * 2:                                               # merging would take too long: the paths together are one drawing
        paths = [[min(p[0] for p in paths), min(p[1] for p in paths), max(p[2] for p in paths), max(p[3] for p in paths), len(paths), sum(p[5] for p in paths)]]
    drawings, textpage = [], None
    for d in _merge([p for p in paths], 4):
        l, b, r, t, count, graphic = d
        w, h = r - l, t - b
        if w < 60 or h < 40 or w * h < 0.03 * area or count < 4 or graphic < 2:
            continue                                                            # rules, underlines, table borders and small marks
        if textpage is None:
            textpage = page.get_textpage()
        chars = len((textpage.get_text_bounded(l, b, r, t) or '').split())
        if chars * 1000 / (w * h) > 1.2 and graphic < 6:
            continue                                                            # full of words between its lines: a table, not a drawing
        drawings.append([l, b, r, t])
    for dr in drawings:                                                         # the labels around a drawing belong to it: axis values, legends, captions on shapes
        l0, b0, r0, t0 = dr
        for l, b, r, t in labels:
            near = l >= l0 - LABEL_REACH and r <= r0 + LABEL_REACH and b >= b0 - LABEL_REACH and t <= t0 + LABEL_REACH
            if near and (r - l) < (r0 - l0):
                dr[0], dr[1], dr[2], dr[3] = min(dr[0], l), min(dr[1], b), max(dr[2], r), max(dr[3], t)
    for img in list(found):                                                     # a picture overlapping a drawing is part of it (a shape's shadow, an icon in a diagram)
        for dr in drawings:
            ix = max(0, min(img[2], dr[2]) - max(img[0], dr[0])) * max(0, min(img[3], dr[3]) - max(img[1], dr[1]))
            if ix > 0.3 * (img[2] - img[0]) * (img[3] - img[1]):
                dr[0], dr[1], dr[2], dr[3] = min(dr[0], img[0]), min(dr[1], img[1]), max(dr[2], img[2]), max(dr[3], img[3])
                found.remove(img)
                break
    regions = [x + [0, 0] for x in found] + [x + [1, 0] for x in drawings]
    merged = []
    for reg in sorted(regions, key=lambda x: -(x[2] - x[0]) * (x[3] - x[1])):    # a picture inside a drawing is part of it
        if any(reg[0] >= m[0] - 2 and reg[1] >= m[1] - 2 and reg[2] <= m[2] + 2 and reg[3] <= m[3] + 2 for m in merged):
            continue
        merged.append(reg)
    out = []
    for l, b, r, t, is_drawing, _ in sorted(merged, key=lambda x: -x[3]):
        pad = 6 if is_drawing else 0
        box = (l - pad, b - pad, r + pad, t + pad)
        w_px, h_px = int((r - l) * 2), int((t - b) * 2)

        def render(box=box, i=index):
            return pdf_page_png(doc, i, scale=2.0, crop=box)
        out.append(col.add('drawing' if is_drawing else 'image', location=where, render=render, width=w_px, height=h_px))
    return ''.join(out)


# ---------------------------------------------------------------- web pages
def _pick_src(img):
    for attr in ('data-src', 'data-lazy-src', 'data-original', 'src'):
        v = (img.get(attr) or '').strip()
        if v and not v.startswith('data:image/gif;base64,R0lGOD'):             # a 1x1 placeholder of a lazy-loading page
            src = v
            break
    else:
        src = ''
    best, best_w = None, 0
    for cand in re.split(r',\s+', img.get('srcset') or img.get('data-srcset') or ''):
        bits = cand.strip().split()
        if not bits:
            continue
        m = re.match(r'(\d+)w$', bits[1]) if len(bits) > 1 else None
        w = int(m.group(1)) if m else 1
        if w > best_w:
            best, best_w = bits[0], w
    return best or src


def _num(v):
    m = re.match(r'\s*(\d+)', str(v or ''))
    return int(m.group(1)) if m else None


def html_figures(root, col, base_url, css=''):
    """Replace each picture and inline SVG of a web page's main content with a marker. Images are fetched later, and only for a captured page.
    css: the page's own <style> text, whose custom properties inline SVG drawings often use for their colours."""
    from bs4 import NavigableString
    variables = css_vars(css)
    for svg in list(root.find_all('svg')):
        if svg.find_parent('svg') is not None:
            continue
        w, h = _num(svg.get('width')), _num(svg.get('height'))
        if svg.get('aria-hidden') == 'true' or (w and h and (w < MIN_SIDE or h < MIN_SIDE)):
            svg.decompose()
            continue
        title = svg.find('title')
        cap = _figcaption(svg)
        svg.replace_with(NavigableString(col.add('svg', web=True, variables=variables, data=str(svg).encode(), fmt='svg', alt=title.get_text(' ', strip=True) if title else (svg.get('aria-label') or ''), caption=cap)))
    for img in list(root.find_all('img')):
        src = _pick_src(img)
        alt = (img.get('alt') or '').strip()
        decorative = img.get('role') in ('presentation', 'none') or img.get('aria-hidden') == 'true'
        cap = _figcaption(img)
        w, h = _num(img.get('width')), _num(img.get('height'))
        data, fmt, url = None, None, None
        if src.startswith('data:image/'):
            m = re.match(r'data:(image/[\w.+-]+)(;base64)?,(.*)', src, re.S)
            if m and m.group(2):
                try:
                    data, fmt = base64.b64decode(m.group(3), validate=False), fmt_of(m.group(1))
                except ValueError:
                    data = None
        elif src and base_url:
            full = urljoin(base_url, src)
            if urlsplit(full).scheme in ('http', 'https'):
                url = full
        marker = col.add('image', web=True, data=data, fmt=fmt, url=url, alt=alt, caption=cap, decorative=decorative, width=w, height=h) if (data or url or alt or cap) else ''
        img.replace_with(NavigableString(marker))


def _figcaption(el):
    fig = el.find_parent('figure')
    if fig is None:
        return ''
    cap = fig.find('figcaption')
    return cap.get_text(' ', strip=True) if cap else ''


def fetcher(allowed_hosts, max_bytes=MAX_FETCH_BYTES):
    """For a captured web page: download its images through the address guard, at most MAX_FETCH of them, each address once (a page repeats
    the same picture in every card)."""
    from . import netguard
    left, seen = {'n': MAX_FETCH}, {}

    def download(url):
        if left['n'] <= 0:
            raise RuntimeError('too many images on this page')
        left['n'] -= 1
        final, status, headers, body = netguard.get(url, allowed_hosts, max_bytes, timeout=20, headers={'Accept': 'image/*'})
        ctype = headers.get('content-type', '').split(';')[0].strip().lower()
        if status >= 400 or not (ctype.startswith('image/') or fmt_of(None, None, body)):
            raise RuntimeError(f'not an image ({status} {ctype})')
        return ctype, body

    def get(url):
        if url not in seen:
            try:
                seen[url] = (True, download(url))
            except Exception as e:
                seen[url] = (False, e)
        ok, got = seen[url]
        if not ok:
            raise got
        return got
    return get


# ---------------------------------------------------------------- scanned pages
def split_scan(answer):
    """The image model's answer for a scanned page -> (exact text, description of its figures)."""
    if SCAN_SPLIT not in (answer or ''):
        return (answer or '').strip(), ''
    text, figs = answer.split(SCAN_SPLIT, 1)
    figs = figs.strip()
    return text.strip(), '' if NONE_ANSWER.search(figs or 'none') else figs
