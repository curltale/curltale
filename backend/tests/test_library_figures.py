"""Figures in documents: found where they sit in PDF, Word, PowerPoint and web pages, filtered before any model call, described (or left with
their caption and alt text), kept with the source, shown on its page and handed to agents that can read images."""
import base64
import io
import json
import zlib

import pytest
from PIL import Image, ImageDraw

from studio import auth, db, llm
from studio.library import extract, figures, localocr, storage
from test_library_access import world, _as, MARK_A   # noqa: F401
from test_library_storage_jobs import _upload
from test_library_wiki import _run
from test_access import _rpc


# ------------------------------------------------------------------ documents made on the spot
def png(im):
    b = io.BytesIO()
    im.save(b, 'PNG')
    return b.getvalue()


def screenshot(w=400, h=260, label='Settings'):
    im = Image.new('RGB', (w, h), (245, 245, 250))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, w, 30), fill=(40, 60, 120))
    d.text((10, 8), label, fill='white')
    for i in range(5):
        d.text((20, 50 + i * 35), f'Field {i + 1}', fill='black')
        d.rectangle((120, 45 + i * 35, w - 20, 70 + i * 35), outline=(120, 120, 120), fill='white')
    d.rectangle((w - 110, h - 35, w - 20, h - 10), fill=(30, 140, 60))
    d.text((w - 95, h - 30), 'Save', fill='white')
    return im


def logo():
    im = Image.new('RGB', (120, 40), 'white')
    d = ImageDraw.Draw(im)
    d.ellipse((2, 2, 38, 38), fill=(200, 30, 30))
    d.text((45, 14), 'ACME', fill='black')
    return im


DIAGRAM = '\n'.join(['0 0 0 RG 1 w', '100 500 m 180 500 l 190 500 200 510 200 520 c 200 560 l 190 580 180 580 180 580 c 100 580 l 100 500 l S',
                     '300 500 100 80 re S', '450 500 100 80 re S', '200 540 m 300 540 l S', '290 545 m 300 540 l 290 535 l S',
                     '400 540 m 450 540 l S', '440 545 m 450 540 l 440 535 l S'])
TABLE = '\n'.join(['0 0 0 RG 0.5 w'] + [f'72 {300 - i * 20} m 540 {300 - i * 20} l S' for i in range(6)] + [f'{72 + j * 117} 300 m {72 + j * 117} 200 l S' for j in range(5)])


def make_pdf(pages):
    """A PDF whose pages hold ('text', x, y, s), ('image', x, y, w, h, PIL image) and ('raw', drawing operators)."""
    objs, kids, binary = ['<< /Type /Catalog /Pages 2 0 R >>', None, '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'], [], {}
    for items in pages:
        ops, xobjs = [], {}
        for it in items:
            if it[0] == 'text':
                ops.append(f'BT /F1 11 Tf {it[1]} {it[2]} Td ({it[3]}) Tj ET')
            elif it[0] == 'image':
                _, x, y, w, h, im = it
                im = im.convert('RGB')
                data = zlib.compress(im.tobytes())
                n = len(objs) + 1
                objs.append(None)
                binary[n] = (f'<< /Type /XObject /Subtype /Image /Width {im.width} /Height {im.height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length {len(data)} >>', data)
                xobjs[f'Im{n}'] = n
                ops.append(f'q {w} 0 0 {h} {x} {y} cm /Im{n} Do Q')
            else:
                ops.append(it[1])
        stream = '\n'.join(ops)
        n = len(objs) + 1
        xo = ' '.join(f'/{k} {v} 0 R' for k, v in xobjs.items())
        objs.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {n + 1} 0 R /Resources << /Font << /F1 3 0 R >> /XObject << {xo} >> >> >>')
        objs.append(f'<< /Length {len(stream)} >>\nstream\n{stream}\nendstream')
        kids.append(f'{n} 0 R')
    objs[1] = f'<< /Type /Pages /Kids [{" ".join(kids)}] /Count {len(pages)} >>'
    out, offs = b'%PDF-1.4\n', []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        if i in binary:
            out += f'{i} 0 obj\n{binary[i][0]}\nstream\n'.encode() + binary[i][1] + b'\nendstream\nendobj\n'
        else:
            out += f'{i} 0 obj\n{o}\nendobj\n'.encode()
    x = len(out)
    out += f'xref\n0 {len(objs) + 1}\n0000000000 65535 f \n'.encode() + b''.join(f'{o:010d} 00000 n \n'.encode() for o in offs)
    return out + f'trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF'.encode()


def manual_pdf():
    pages = []
    for p in range(3):
        items = [('image', 470, 740, 120, 40, logo()), ('text', 72, 700, f'Page {p + 1} of the admin manual')]
        if p == 0:
            items += [('text', 72, 680, 'Open Settings as shown in the figure below.'), ('image', 72, 380, 400, 260, screenshot())]
        if p == 1:
            items += [('raw', DIAGRAM), ('text', 110, 535, 'Upload'), ('text', 310, 535, 'Read'), ('text', 460, 535, 'Index'), ('raw', TABLE)]
            items += [('text', 80, 285 - i * 20, f'Row {i} cell') for i in range(5)]
        pages.append(items)
    return make_pdf(pages)


def manual_docx():
    from docx import Document
    from docx.shared import Inches
    from lxml import etree
    d = Document()
    d.add_heading('Admin manual', 1)
    d.add_paragraph('Open the settings screen shown below.')
    d.add_picture(io.BytesIO(png(screenshot())), width=Inches(4))
    d.add_paragraph('Figure 1: The settings screen', style='Caption')
    d.add_paragraph('A bullet icon:')
    d.add_picture(io.BytesIO(png(logo().resize((20, 8)))), width=Inches(0.2))
    d.add_picture(io.BytesIO(png(screenshot(label='Decoration'))), width=Inches(3))
    pr = d.inline_shapes[-1]._inline.docPr
    pr.set('descr', 'Image 3')
    ext = etree.SubElement(etree.SubElement(pr, '{http://schemas.openxmlformats.org/drawingml/2006/main}extLst'), '{http://schemas.openxmlformats.org/drawingml/2006/main}ext')
    etree.SubElement(ext, '{http://schemas.microsoft.com/office/drawing/2017/decorative}decorative').set('val', '1')
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text, t.cell(1, 0).text = 'Step', 'Screen', 'Save the form'
    t.cell(1, 1).paragraphs[0].add_run().add_picture(io.BytesIO(png(screenshot(label='Save dialog'))), width=Inches(2))
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def slides_pptx(hidden_first=False):
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
    from pptx.util import Inches as I
    prs = Presentation()
    if hidden_first:
        s0 = prs.slides.add_slide(prs.slide_layouts[5])
        s0.shapes.title.text = 'Hidden'
        s0._element.set('show', '0')
    s1 = prs.slides.add_slide(prs.slide_layouts[5])
    s1.shapes.title.text = 'Revenue'
    cd = CategoryChartData()
    cd.categories = ['Q1', 'Q2']
    cd.add_series('2026', (10.5, 12))
    cd.add_series('2025', (9, 11))
    s1.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, I(1), I(2), I(6), I(4), cd)
    s2 = prs.slides.add_slide(prs.slide_layouts[5])
    s2.shapes.title.text = 'Flow'
    for i, w in enumerate(['Upload', 'Read', 'Index']):
        s2.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, I(0.5 + i * 3), I(3), I(2), I(1)).text = w
    s2.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, I(2.5), I(3.5), I(3.5), I(3.5))
    s2.shapes.add_picture(io.BytesIO(png(screenshot(label='On the flow slide'))), I(1), I(5), I(2))
    s3 = prs.slides.add_slide(prs.slide_layouts[5])
    s3.shapes.title.text = 'Sign in'
    pic = s3.shapes.add_picture(io.BytesIO(png(screenshot(label='Login'))), I(1), I(2), I(5))
    pic._element.nvPicPr.cNvPr.set('descr', 'The login screen with the Sign in button')
    b = io.BytesIO()
    prs.save(b)
    return b.getvalue()


class Describer:
    """Stands in for the image model: records what it was given."""
    def __init__(self, answer='A screen with five fields and a Save button.'):
        self.calls, self.answer = [], answer

    def __call__(self, image, mime, ctx):
        self.calls.append((image, mime, ctx))
        return self.answer(ctx) if callable(self.answer) else self.answer


# ------------------------------------------------------------------ reading
def test_a_pdf_gives_its_pictures_and_drawings_but_not_its_tables_or_its_logo():
    d = Describer()
    r = extract.extract('m.pdf', manual_pdf(), figures=figures.Options(describe=d, max_described=5))
    assert [(f.label, f.kind, f.location, f.described_by) for f in r.figures] == [('Figure 1', 'image', 'Page 1', 'ai'), ('Figure 2', 'drawing', 'Page 2', 'ai')]
    assert '[Figure 1, page 1, described by AI: A screen with five fields and a Save button.]' in r.text
    assert r.text.index('Open Settings') < r.text.index('[Figure 1') < r.text.index('Page 2')           # the block sits on its page
    for f in r.figures:
        assert r.text[f.char_start:].startswith('[' + f.label)
    assert r.figure_stats['repeated'] == 3 and r.figure_stats['found'] == 5                               # the logo on every page is not a figure
    assert 'Open Settings as shown' in d.calls[0][2]['before'] and d.calls[0][2]['location'] == 'Page 1'   # the model gets the text around it
    im = Image.open(io.BytesIO(d.calls[1][0]))
    assert im.width > 2.5 * im.height                                                                    # the diagram's region, not the page with its table
    assert any(n.startswith('5 figure(s): 2 described by AI, 3 skipped') for n in r.notes)


def test_a_bar_chart_drawn_in_a_pdf_is_a_figure_with_its_labels_and_a_shaded_table_is_not():
    bars = '\n'.join(['0 0 0 RG 0.5 w 100 400 m 500 400 l S'] + [f'{c} rg {120 + i * 90} 400 50 {h} re f' for i, (c, h) in enumerate([('0.2 0.4 0.8', 120), ('0.8 0.3 0.3', 80),
                                                                                                                                     ('0.2 0.4 0.8', 160), ('0.8 0.3 0.3', 100)])])
    shaded = '\n'.join(['0.85 0.85 0.85 rg 72 280 468 20 re f', '0 0 0 RG 0.5 w'] + [f'72 {300 - i * 20} m 540 {300 - i * 20} l S' for i in range(6)])
    labels = [('text', 135, 385, 'Q1'), ('text', 225, 385, 'Q2'), ('text', 315, 385, 'Q3'), ('text', 405, 385, 'Q4'), ('text', 80, 520, '150')]
    rows = [('text', 80, 285 - i * 20, f'Region {i} total revenue in euro for the year') for i in range(5)]
    r = extract.extract('c.pdf', make_pdf([[('text', 72, 700, 'Revenue by quarter is shown below.'), ('raw', bars)] + labels, [('text', 72, 700, 'The table'), ('raw', shaded)] + rows]),
                        figures=figures.Options())
    assert [(f.kind, f.location) for f in r.figures] == [('drawing', 'Page 1')]
    f = r.figures[0]
    assert f.width >= 2 * (500 - 100) and f.height >= 2 * (560 - 385) - 20                             # the bars, and the labels around them


def test_without_figure_options_nothing_changes():
    r = extract.extract('m.pdf', manual_pdf())
    assert '[Figure' not in r.text and r.figures == [] and '⟦' not in r.text


def test_word_figures_keep_their_place_caption_and_decorative_flag():
    d = Describer()
    r = extract.extract('m.docx', manual_docx(), figures=figures.Options(describe=d, max_described=1))
    assert '[Figure 1: "Figure 1: The settings screen", described by AI: A screen with five fields and a Save button.]' in r.text
    assert r.text.index('shown below') < r.text.index('[Figure 1') < r.text.index('\nFigure 1: The settings screen') < r.text.index('Save the form')
    st = r.figure_stats
    assert st['decorative'] == 1 and st['small'] == 1 and st['described'] == 1 and st['over_limit'] == 1                 # one call allowed: the captioned one went first
    assert [f.described_by for f in r.figures] == ['ai', None] and r.figures[1].png is not None                        # the table's picture is kept, nothing said
    assert len(d.calls) == 1 and d.calls[0][2]['caption'] == 'Figure 1: The settings screen' and d.calls[0][2]['section'] == 'Admin manual'


def test_without_ai_a_figure_keeps_its_alt_text_and_placeholder_alt_text_is_ignored():
    r = extract.extract('s.pptx', slides_pptx(), figures=figures.Options())
    assert '[Figure 2, slide 3: The login screen with the Sign in button]' in r.text
    assert '[Chart 1, slide 1]\nCategory | 2026 | 2025\nRow 1: Category: Q1; 2026: 10.5; 2025: 9\nRow 2: Category: Q2; 2026: 12; 2025: 11' in r.text
    assert r.figure_stats['needs_converter'] == 1 and any('1 need the document converter' in n for n in r.notes)        # the flow slide is drawn with shapes
    assert [f.label for f in r.figures] == ['Chart 1', 'Figure 1', 'Figure 2']                                        # its picture stands on its own meanwhile
    c = figures.Candidate(0, 'image', alt='image1.png')
    assert figures._informative_alt(c) == '' and figures._informative_alt(figures.Candidate(0, 'image', alt='Picture 3')) == ''


def test_a_slide_drawn_with_shapes_is_rendered_whole_by_the_converter_and_hidden_slides_are_skipped():
    sent = []

    def converter(name, data):                                   # a PDF with one page per shown slide, the flow slide's page carrying a drawing
        sent.append(name)
        return make_pdf([[('text', 72, 700, 'Revenue')], [('text', 72, 700, 'Flow'), ('raw', DIAGRAM)], [('text', 72, 700, 'Sign in')]])
    d = Describer('A flow from Upload to Read to Index.')
    r = extract.extract('s.pptx', slides_pptx(hidden_first=True), figures=figures.Options(describe=d, converter=converter))
    assert sent == ['slides.pptx']                                                              # converted once for the whole file
    flow = next(f for f in r.figures if f.kind == 'slide')
    assert flow.location == 'Slide 3' and flow.described_by == 'ai' and '[Figure 1, slide 3, described by AI: A flow from Upload to Read to Index.]' in r.text
    assert 'On the flow slide' not in json.dumps([c[2] for c in d.calls]) and r.figure_stats['found'] == 3        # the slide's own picture is part of it
    assert Image.open(io.BytesIO(flow.png)).size == (flow.width, flow.height) and flow.height > 1000                         # the whole page the converter printed


def test_web_page_figures_inline_svg_data_images_and_downloads_only_for_a_captured_page():
    shot = base64.b64encode(png(screenshot(label='inline'))).decode()
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="300" height="120"><title>Flow</title><rect x="10" y="10" width="100" height="50" fill="none" stroke="black"/>'
           '<image href="file:///etc/hostname" width="50" height="50"/><line x1="110" y1="35" x2="200" y2="35" stroke="black"/></svg>')
    html = (f'<html><body><nav><img src="/nav.png" alt="Menu"></nav><main><h1>Guide</h1><p>See the screenshot below.</p><figure><img src="shots/a.png" alt="image1.png">'
            f'<figcaption>The upload screen</figcaption></figure><img src="data:image/png;base64,{shot}" alt="Inline screen"><img src="/icon.png" width="16" height="16">'
            f'<svg width="10" height="10"><circle r="4"/></svg>{svg}<p>End.</p></main></body></html>').encode()
    fetched = []

    def fetch(url):
        fetched.append(url)
        return 'image/png', png(screenshot(label='fetched'))
    r = extract.extract('page.html', html, figures=figures.Options(fetch=fetch, base_url='https://docs.example.com/guide/'))
    assert fetched == ['https://docs.example.com/guide/shots/a.png']                                  # not the menu's, not the 16 px icon
    assert '[Figure 1: The upload screen]' in r.text and '[Figure 2: Inline screen]' in r.text and '[Figure 3: Flow]' in r.text
    assert r.text.index('See the screenshot') < r.text.index('[Figure 1') < r.text.index('End.')
    r = extract.extract('upload.html', html, figures=figures.Options(fetch=fetch))                   # an uploaded file: no address to resolve, nothing fetched
    assert len(fetched) == 1 and '[Figure 1: The upload screen]' in r.text and r.figures[0].png is None     # its caption still says what it is


def test_empty_pictures_never_reach_the_model_and_white_icons_are_shown_on_dark():
    """A model shown nothing makes something up from the text around it: blank and gradient-only pictures are dropped before any call."""
    blank = Image.new('RGBA', (600, 400), (255, 255, 255, 0))
    ImageDraw.Draw(blank).rectangle((100, 100, 500, 300), outline=(255, 255, 255, 255), width=8)       # a white drawing on transparency
    grad = Image.linear_gradient('L').resize((1600, 300)).convert('RGB')                                # a banner background
    flat = Image.new('RGB', (700, 700), 'white')
    shot = base64.b64encode(png(screenshot())).decode()
    html = ('<html><body><main><p>Plans from AED 15.</p>' + ''.join(f'<img src="data:image/png;base64,{base64.b64encode(png(i)).decode()}">' for i in (grad, flat)) +
            f'<img src="data:image/png;base64,{shot}"><p>Daily pass.</p></main></body></html>').encode()
    d = Describer()
    r = extract.extract('p.html', html, figures=figures.Options(describe=d))
    assert len(d.calls) == 1 and r.figure_stats['decorative'] == 2                                       # only the screenshot was sent
    shown = figures._flatten(blank)
    assert shown.getpixel((10, 10)) == (43, 43, 43) and shown.getpixel((100, 200)) == (255, 255, 255)   # the white drawing stands out
    assert figures._detail(blank) > figures.MIN_DETAIL and figures._detail(grad) < figures.MIN_DETAIL


def test_an_inline_svg_gets_the_colours_the_page_defines_for_it():
    css = ':root { --fg: #1b2033; --accent: #0b7a75; }\n@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --fg: #ffffff; } }\n:root[data-theme="dark"] { --fg: #eeeeee; }'
    assert figures.css_vars(css) == {'--fg': '#1b2033', '--accent': '#0b7a75'}                      # the light theme, as the page first shows
    assert figures.apply_vars('fill="var(--fg)" stroke="var(--nope, #123456)" x="var(--gone)"', figures.css_vars(css)) == 'fill="#1b2033" stroke="#123456" x="currentColor"'
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="120"><rect x="10" y="10" width="280" height="100" fill="var(--accent)"/></svg>'
    im = Image.open(io.BytesIO(figures.svg_to_png(svg, variables=figures.css_vars(css)))).convert('RGB')
    assert im.getpixel((im.width // 2, im.height // 2)) == (11, 122, 117)                           # teal, not the black a renderer draws for var()


def test_svg_is_drawn_without_reading_anything_else():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="200" height="100"><image xlink:href="/etc/passwd"/><style>@import url(http://x.example/a.css);</style><rect width="80" height="40"/></svg>'
    cleaned = figures._SVG_IMPORT.sub('', figures._SVG_EXTERNAL.sub('', svg))
    assert '/etc/passwd' not in cleaned and 'x.example' not in cleaned
    out = figures.svg_to_png(svg)
    assert out and Image.open(io.BytesIO(out)).width >= 400                                          # small drawings are drawn larger, for legibility
    assert figures.svg_to_png('<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><svg>&e;</svg>') is None


def test_the_model_can_say_a_figure_is_decorative_and_a_repeated_screenshot_is_described_once():
    from docx import Document
    from docx.shared import Inches
    d = Document()
    for i in range(2):
        d.add_paragraph(f'Step {i + 1}: open the screen.')
        d.add_picture(io.BytesIO(png(screenshot(label='Same'))), width=Inches(3))
    d.add_paragraph('A nice photo:')
    d.add_picture(io.BytesIO(png(screenshot(label='Photo of the team'))), width=Inches(3))
    b = io.BytesIO()
    d.save(b)
    m = Describer(lambda ctx: 'NONE' if 'photo' in ctx['before'] else 'The same screen.')
    r = extract.extract('d.docx', b.getvalue(), figures=figures.Options(describe=m))
    assert [(f.label, f.same_as) for f in r.figures] == [('Figure 1', None), ('Figure 2', 'Figure 1')] and len(m.calls) == 2
    assert '[Figure 2: the same image as Figure 1]' in r.text and r.figure_stats['not_informative'] == 1 and 'Figure 3' not in r.text


def test_a_description_quoting_labels_the_picture_does_not_show_is_left_out():
    invented = 'A plan card: "Daily Combo Plan", "AED 60 /Month", "Validity: 1 Day", "Dial *177#" and "3GB".'
    assert not figures.grounded(invented, b'png', lambda img: '')                                     # nothing readable in the picture
    assert figures.grounded(invented, b'png', lambda img: 'Daily Combo Plan AED 60 Month Validity 1 Day')
    assert figures.grounded('A red phone icon.', b'png', lambda img: '')                               # quotes nothing: not judged
    d = Describer(invented)
    r = extract.extract('m.docx', manual_docx(), figures=figures.Options(describe=d, ground=lambda img: 'Field 1 Field 2 Save'))
    assert r.figure_stats['ungrounded'] == 2 and 'Daily Combo' not in r.text and any('does not show' in n for n in r.notes)


def test_a_failing_model_leaves_the_caption_and_says_so():
    def broken(image, mime, ctx):
        raise TimeoutError('no answer')
    r = extract.extract('m.docx', manual_docx(), figures=figures.Options(describe=broken))
    assert '[Figure 1: Figure 1: The settings screen]' in r.text and any('the image model failed on 2 figure(s)' in n for n in r.notes)


def test_a_document_cannot_forge_a_figure_marker():
    r = extract.extract('n.md', 'Before ⟦fig:00000000:0⟧ after'.encode(), figures=figures.Options())
    assert '⟦fig:00000000:0⟧' in r.text and r.figures == []


def test_a_scanned_page_keeps_its_text_and_its_figures_apart():
    assert figures.split_scan('Signed by the director.\n[[FIGURES]]\nA round stamp reading "APPROVED 3 May 2026".') == ('Signed by the director.', 'A round stamp reading "APPROVED 3 May 2026".')
    assert figures.split_scan('Only text.') == ('Only text.', '') and figures.split_scan('Text\n[[FIGURES]]\nNONE') == ('Text', '')
    assert figures.NONE_ANSWER.search('A red circle next to "ACME". This is a logo.\n\nNONE') and not figures.NONE_ANSWER.search('Press None to clear the field.')
    pdf = make_pdf([[('text', 72, 700, 'A typed page with enough words')], []])
    r = extract.extract('s.pdf', pdf, ocr=lambda p, m: extract.OcrText('Signed by the director.', 'OCR', figures='A round stamp reading APPROVED.'), figures=figures.Options())
    assert 'Page 2 (OCR)\nSigned by the director.\n\n[Figure 1, page 2, described by AI: A round stamp reading APPROVED.]' in r.text
    assert r.figures[0].png is None and r.figure_stats['scans_described'] == 1                       # a scan is described, not cut up


def test_chart_values_come_from_the_file():
    xml = b'''<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><c:chart>
      <c:title><c:tx><c:rich><a:p><a:r><a:t>Tickets</a:t></a:r></a:p></c:rich></c:tx></c:title><c:plotArea><c:barChart>
      <c:ser><c:tx><c:strRef><c:strCache><c:pt idx="0"><c:v>Open</c:v></c:pt></c:strCache></c:strRef></c:tx>
      <c:cat><c:strRef><c:strCache><c:pt idx="0"><c:v>Mon</c:v></c:pt><c:pt idx="1"><c:v>Tue</c:v></c:pt></c:strCache></c:strRef></c:cat>
      <c:val><c:numRef><c:numCache><c:pt idx="0"><c:v>4</c:v></c:pt><c:pt idx="1"><c:v>7</c:v></c:pt></c:numCache></c:numRef></c:val></c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>'''
    assert figures.chart_text(xml) == ('Tickets', ['Category | Open', 'Row 1: Category: Mon; Open: 4', 'Row 2: Category: Tue; Open: 7'])


# ------------------------------------------------------------------ the whole way: ingest, page, agents
@pytest.fixture
def described(monkeypatch):
    from studio import embeddings
    monkeypatch.setattr(llm, 'available', lambda: True)
    monkeypatch.setattr(embeddings, 'available', lambda: False)
    monkeypatch.setattr('studio.library.wiki.run', lambda *a, **k: 'skipped')                   # no source page is written in these tests
    m = Describer(lambda ctx: f"A settings screen ({ctx['location'] or 'no page'}).")
    monkeypatch.setattr(llm, 'describe_figure', m)
    return m


def _source(client, col, name):
    return next(s for s in client.get(f'/api/library/collections/{col}/sources').json()['sources'] if s['name'] == name)


def test_figures_are_kept_with_the_source_shown_and_replaced_with_it(client, world, described):
    a = world['a']['id']
    _as(client, 'ann')
    sid = _upload(client, a, 'manual.pdf', manual_pdf()).json()['id']
    _run()
    s = _source(client, a, 'manual.pdf')
    assert s['status'] == 'ready' and s['stats']['figures']['kept'] == 2 and any('2 described by AI' in n for n in s['stats']['notes'])
    with db.conn() as c:
        text = c.execute('SELECT text FROM library.source_text WHERE source_id=%s', (sid,)).fetchone()['text']
        keys = [r['storage_key'] for r in c.execute('SELECT storage_key FROM library.source_figure WHERE source_id=%s', (sid,)).fetchall()]
    assert '[Figure 1, page 1, described by AI: A settings screen (Page 1).]' in text and all(k.startswith(f'{a}/{sid}/figures/') for k in keys)
    figs = client.get(f'/api/library/collections/{a}/sources/{sid}/figures').json()['figures']
    assert [(f['label'], f['described_by'], f['has_image']) for f in figs] == [('Figure 1', 'ai', True), ('Figure 2', 'ai', True)]
    img = client.get(f"/api/library/collections/{a}/sources/{sid}/figures/{figs[0]['id']}")
    assert img.status_code == 200 and img.headers['content-type'] == 'image/png' and img.content[:4] == b'\x89PNG'
    _as(client, 'eve')                                                                           # no grant: no figures, no images
    assert client.get(f'/api/library/collections/{a}/sources/{sid}/figures').status_code in (403, 404)
    assert client.get(f"/api/library/collections/{a}/sources/{sid}/figures/{figs[0]['id']}").status_code in (403, 404)
    _as(client, 'ann')                                                                           # a new version: its figures replace the old ones
    one_page = make_pdf([[('text', 72, 700, 'The new manual has one screen.'), ('image', 72, 380, 400, 260, screenshot(label='New'))]])
    assert client.post(f'/api/library/collections/{a}/sources/{sid}/replace', files={'file': ('manual.pdf', one_page, 'application/pdf')}).status_code == 200
    _run()
    figs = client.get(f'/api/library/collections/{a}/sources/{sid}/figures').json()['figures']
    assert [f['label'] for f in figs] == ['Figure 1']
    gone = [k for k in keys if not storage.store().exists(k)]
    assert len(gone) == 2                                                                        # the old images are deleted
    assert client.delete(f'/api/library/collections/{a}/sources/{sid}').status_code == 200
    with db.conn() as c:
        assert c.execute('SELECT count(*) AS n FROM library.source_figure WHERE source_id=%s', (sid,)).fetchone()['n'] == 0


def test_without_ai_figures_are_kept_but_not_described(client, world, described, monkeypatch):
    monkeypatch.setattr(llm, 'available', lambda: False)
    a = world['a']['id']
    _as(client, 'ann')
    sid = _upload(client, a, 'manual.docx', manual_docx()).json()['id']
    _run()
    assert described.calls == []
    figs = client.get(f'/api/library/collections/{a}/sources/{sid}/figures').json()['figures']
    assert [(f['label'], f['described_by'], f['has_image']) for f in figs] == [('Figure 1', 'caption', True), ('Figure 2', None, True)]


def test_scanned_pages_get_their_figures_described_when_ai_reads_them(client, world, described, monkeypatch):
    monkeypatch.setattr(localocr, 'available', lambda: False)
    asked = []

    def ocr(png_, mime, figures=False):
        asked.append(figures)
        return 'Contract signed in Dubai.\n[[FIGURES]]\nA blue stamp reading "PAID".'
    monkeypatch.setattr(llm, 'ocr', ocr)
    a = world['a']['id']
    _as(client, 'ann')
    sid = _upload(client, a, 'scan.pdf', make_pdf([[]])).json()['id']
    _run()
    with db.conn() as c:
        text = c.execute('SELECT text FROM library.source_text WHERE source_id=%s', (sid,)).fetchone()['text']
    assert asked == [True] and 'Contract signed in Dubai.' in text and '[Figure 1, page 1, described by AI: A blue stamp reading "PAID".]' in text
    from studio import settings
    settings.put('FIGURES_DESCRIBE', False, 't')
    try:
        sid = _upload(client, a, 'scan2.pdf', make_pdf([[], []])).json()['id']
        _run()
        assert asked[-1] is False                                                                 # off: text only, as before
    finally:
        settings.reset('FIGURES_DESCRIBE', 't')


def test_agents_get_the_figure_list_and_the_image_when_they_may_see_sources(client, world, described):
    a = world['a']['id']
    _as(client, 'ann')
    sid = _upload(client, a, 'manual.pdf', manual_pdf()).json()['id']
    _run()
    toks = {n: auth.create_api_token(f'fig-{n}', {'data': False, **({'sources': False} if n == 'quiet' else {})}) for n in ('full', 'quiet')}
    with db.conn() as c:
        for n in toks:
            tid = c.execute('SELECT id FROM api_token WHERE name=%s', (f'fig-{n}',)).fetchone()['id']
            c.execute('INSERT INTO library.grant_ (collection_id, token_id, level) VALUES (%s,%s,%s)', (a, tid, 'read'))
    try:
        call = lambda tok, name, args: _rpc(client, tok, 'tools/call', {'name': name, 'arguments': args})
        res = json.loads(call(toks['full'], 'library_source', {'collection': a, 'source': 'manual.pdf', 'original': True})['content'][0]['text'])
        assert [f['figure'] for f in res['figures']] == ['Figure 1', 'Figure 2'] and 'library_figure' in res['figures_note']
        assert res['figures'][0]['described'] == 'by AI' and res['figures'][0]['image'] is True and 'original_passage' in res['figures'][0]
        out = call(toks['full'], 'library_figure', {'collection': a, 'source': 'manual.pdf', 'figure': '1'})['content']
        assert [b['type'] for b in out] == ['text', 'image'] and out[1]['mimeType'] == 'image/png'
        meta = json.loads(out[0]['text'])
        assert meta['figure'] == 'Figure 1' and meta['location'] == 'Page 1' and 'not by the document' in meta['description_note'] and '_image' not in meta
        Image.open(io.BytesIO(base64.b64decode(out[1]['data']))).verify()
        missing = json.loads(call(toks['full'], 'library_figure', {'collection': a, 'source': 'manual.pdf', 'figure': 'Figure 9'})['content'][0]['text'])
        assert 'library_source lists its figures' in missing['error']
        quiet = json.loads(call(toks['quiet'], 'library_source', {'collection': a, 'source': 'manual.pdf', 'original': True})['content'][0]['text'])
        assert 'figures' not in quiet
        assert 'library_figure' not in [t['name'] for t in _rpc(client, toks['quiet'], 'tools/list')['tools']]
        assert 'error' in json.loads(call(toks['quiet'], 'library_figure', {'collection': a, 'source': 'manual.pdf', 'figure': '1'})['content'][0]['text'])
    finally:
        with db.conn() as c:
            c.execute("DELETE FROM api_token WHERE name LIKE 'fig-%'")
