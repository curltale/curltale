"""Turn an uploaded file into plain text, keeping the places it came from ("Page 3", "Slide 2", "Sheet: Costs") as marker lines so that a
passage can later be cited. Scanned pages and images go through an OCR function if one is given. Files are untrusted: Office files are
checked for zip bombs before they are opened, and nothing is executed."""
import csv
import html
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

MAX_ZIP_BYTES = 300 * 1024 * 1024        # uncompressed size an Office file may expand to
MAX_ZIP_ENTRIES = 20000


class ExtractError(Exception):
    """The file cannot be read; the message is shown to the person who uploaded it."""


@dataclass
class Extracted:
    text: str
    title: str | None = None
    pages: int | None = None                  # pages, slides or sheets
    ocr_pages: int = 0
    notes: list = field(default_factory=list)


def normalize(text):
    text = html.unescape(text).replace('\xa0', ' ').replace('\r\n', '\n').replace('\r', '\n').replace('\x00', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n[ \t]+', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _decode(content):
    for bom, enc in ((b'\xef\xbb\xbf', 'utf-8-sig'), (b'\xff\xfe', 'utf-16'), (b'\xfe\xff', 'utf-16')):
        if content.startswith(bom):
            return content.decode(enc, errors='replace')
    try:
        return content.decode('utf-8')
    except UnicodeDecodeError:
        return content.decode('cp1252', errors='replace')


def _check_zip(content):
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            infos = z.infolist()
            if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_ZIP_BYTES:
                raise ExtractError('the file expands to an unreasonable size')
    except zipfile.BadZipFile:
        raise ExtractError('the file is damaged or is not the format its name says')


# ---------------------------------------------------------------- formats
_NOISE_ROLES = {'navigation', 'banner', 'contentinfo', 'complementary', 'search'}
_NOISE_CLASS = re.compile(r'\b(footer|header|topnav|top-nav|navbar|nav-bar|navigation|breadcrumb|cookie|gdpr|consent|copyright|sidebar|side-bar|widget|advertisement|promo|banner|skip-link)\b', re.I)


def html_to_text(raw):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(raw, 'html.parser')
    title = soup.title.get_text(' ', strip=True) if soup.title else None
    for n in soup(['script', 'style', 'noscript', 'svg', 'canvas', 'iframe', 'nav', 'footer', 'header', 'aside', 'form']):
        n.decompose()
    for n in soup.find_all(True):
        if n.attrs is not None and (n.get('role') in _NOISE_ROLES or _NOISE_CLASS.search(' '.join(n.get('class') or []) + ' ' + (n.get('id') or ''))):
            n.decompose()
    root = soup.find('main') or soup.find(attrs={'role': 'main'}) or soup.find('article') or soup.body or soup
    for tag in root.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):                 # headings become Markdown headings: they steer chunking and citations
        tag.insert_before('\n\n' + '#' * int(tag.name[1]) + ' ')
        tag.append('\n\n')
    for tag in root.find_all(['p', 'li', 'tr', 'section', 'article', 'div', 'br']):
        tag.append('\n')
    return normalize(root.get_text(' ', strip=False)), title


def csv_to_text(content, delimiter=','):
    rows = list(csv.reader(io.StringIO(_decode(content)), delimiter=delimiter))
    if not rows:
        return ''
    head = rows[0]
    out = []
    for i, row in enumerate(rows[1:], 1):
        pairs = ', '.join(f'{h}: {v}' for h, v in zip(head, row) if v.strip())
        if pairs:
            out.append(f'Row {i}: {pairs}')
    return normalize('\n'.join(out))


def xlsx_to_text(content):
    _check_zip(content)
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    parts, sheets = [], 0
    for ws in wb.worksheets:
        rows = [[('' if c is None else str(c)) for c in row] for row in ws.iter_rows(values_only=True)]
        rows = [r for r in rows if any(v.strip() for v in r)]
        if not rows:
            continue
        sheets += 1
        parts.append(f'Sheet: {ws.title}')
        head = rows[0]
        for i, row in enumerate(rows[1:], 1):
            pairs = ', '.join(f'{h}: {v}' for h, v in zip(head, row) if v.strip())
            if pairs:
                parts.append(f'Row {i}: {pairs}')
    return normalize('\n'.join(parts)), sheets


def docx_to_text(content):
    _check_zip(content)
    from docx import Document
    doc = Document(io.BytesIO(content))
    title = (doc.core_properties.title or '').strip() or None
    parts = []
    for p in doc.paragraphs:
        t = p.text.strip()
        if not t:
            continue
        m = re.match(r'Heading (\d)', p.style.name or '') if p.style is not None else None
        parts.append(('#' * int(m.group(1)) + ' ' + t) if m else t)
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(' | '.join(cells))
    return normalize('\n\n'.join(parts)), title


def pptx_to_text(content):
    _check_zip(content)
    from pptx import Presentation
    prs = Presentation(io.BytesIO(content))
    title, parts, slides = None, [], 0
    for i, slide in enumerate(prs.slides, 1):
        texts = [p.text.strip() for sh in slide.shapes if sh.has_text_frame for p in sh.text_frame.paragraphs if p.text.strip()]
        for sh in slide.shapes:
            if getattr(sh, 'has_table', False) and sh.has_table:
                texts += [' | '.join(c.text.strip() for c in r.cells if c.text.strip()) for r in sh.table.rows]
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            n = slide.notes_slide.notes_text_frame.text.strip()
            if n:
                texts.append('Notes: ' + n)
        if not texts:
            continue
        slides += 1
        title = title or texts[0]
        parts.append(f'Slide {i}\n' + '\n'.join(texts))
    return normalize('\n\n'.join(parts)), title, slides


def pdf_to_text(content, ocr=None, max_ocr_pages=100):
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise ExtractError('the PDF is password protected')
        native = [(p.extract_text() or '').strip() for p in reader.pages]
        meta_title = (reader.metadata.title or '').strip() if reader.metadata else ''
    except ExtractError:
        raise
    except (PdfReadError, ValueError, KeyError, OSError) as e:
        raise ExtractError(f'the PDF cannot be read ({type(e).__name__})')
    scanned = [i for i, t in enumerate(native) if len(t) < 5]
    notes, todo = [], set()
    if scanned and ocr:
        if len(scanned) > max_ocr_pages:                              # spread the effort over the whole document rather than only its start
            step = len(scanned) / max_ocr_pages
            todo = {scanned[int(i * step)] for i in range(max_ocr_pages)}
            notes.append(f'{len(scanned)} pages have no text; {len(todo)} of them were read with OCR (evenly spread)')
        else:
            todo = set(scanned)
    elif scanned:
        notes.append(f'{len(scanned)} page(s) have no text and no OCR model is set (Settings, Alfred and AI)')
    parts, done = [], 0
    pdf = None
    try:
        for i, t in enumerate(native):
            if len(t) >= 5:
                parts.append(f'Page {i + 1}\n{t}')
            elif i in todo:
                if pdf is None:
                    import pypdfium2
                    pdf = pypdfium2.PdfDocument(content)
                buf = io.BytesIO()
                pdf[i].render(scale=2).to_pil().convert('RGB').save(buf, 'PNG')
                text = (ocr(buf.getvalue(), 'image/png') or '').strip()
                if text:
                    parts.append(f'Page {i + 1} (OCR)\n{text}')
                    done += 1
    finally:
        if pdf is not None:
            pdf.close()
    return normalize('\n\n'.join(parts)), meta_title or None, len(native), done, notes


def image_to_text(content, ocr):
    if not ocr:
        return '', ['an image needs an OCR model (Settings, Alfred and AI)']
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(content))
        img.load()
        buf = io.BytesIO()
        img.convert('RGB').save(buf, 'PNG')
    except Exception:
        raise ExtractError('the image cannot be read')
    return normalize(ocr(buf.getvalue(), 'image/png') or ''), []


TEXT_SUFFIXES = {'.txt', '.md', '.json', '.xml', '.tsv', '.log'}
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.tif', '.tiff', '.bmp'}


def extract(filename, content, ocr=None, max_ocr_pages=100):
    """-> Extracted. Raises ExtractError with a plain message when the file cannot be read or holds no text."""
    suffix = Path(filename).suffix.lower()
    res = Extracted('')
    if suffix in ('.html', '.htm'):
        res.text, res.title = html_to_text(_decode(content))
    elif suffix == '.pdf':
        res.text, res.title, res.pages, res.ocr_pages, res.notes = pdf_to_text(content, ocr, max_ocr_pages)
    elif suffix == '.docx':
        res.text, res.title = docx_to_text(content)
    elif suffix == '.pptx':
        res.text, res.title, res.pages = pptx_to_text(content)
    elif suffix == '.xlsx':
        res.text, res.pages = xlsx_to_text(content)
    elif suffix == '.csv':
        res.text = csv_to_text(content)
    elif suffix == '.tsv':
        res.text = csv_to_text(content, '\t')
    elif suffix in IMAGE_SUFFIXES:
        res.text, res.notes = image_to_text(content, ocr)
        res.ocr_pages = 1 if res.text else 0
    elif suffix in TEXT_SUFFIXES:
        res.text = normalize(_decode(content))
    else:
        raise ExtractError(f"files of type '{suffix}' are not supported")
    if not res.text.strip():
        raise ExtractError('no readable text was found' + (': ' + '; '.join(res.notes) if res.notes else ''))
    return res


SUPPORTED = {'pdf', 'docx', 'pptx', 'xlsx', 'csv', 'tsv', 'txt', 'md', 'json', 'xml', 'png', 'jpg', 'jpeg', 'webp', 'gif', 'tif', 'tiff', 'bmp', 'html', 'htm'}
