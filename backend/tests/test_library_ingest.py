import io, zipfile
import pytest
from studio import config, db, embeddings, vectors
from studio.library import chunking, extract, jobs, localocr, retrieval, scope as S
from test_library_access import world, _as, _admin, MARK_A, MARK_B   # noqa: F401
from test_library_storage_jobs import _upload


def make_pdf(pages):
    """A small valid PDF: each page is a text (drawn in Helvetica) or None (blank, like a scan with no text layer)."""
    objs = ['<< /Type /Catalog /Pages 2 0 R >>', None, '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    kids = []
    for t in pages:
        n = len(objs) + 1
        stream = f'BT /F1 12 Tf 72 720 Td ({t.replace("(", "[").replace(")", "]")}) Tj ET' if t else ''
        objs.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {n + 1} 0 R /Resources << /Font << /F1 3 0 R >> >> >>')
        objs.append(f'<< /Length {len(stream)} >>\nstream\n{stream}\nendstream')
        kids.append(f'{n} 0 R')
    objs[1] = f'<< /Type /Pages /Kids [{" ".join(kids)}] /Count {len(pages)} >>'
    out, offs = b'%PDF-1.4\n', []
    for i, o in enumerate(objs, 1):
        offs.append(len(out)); out += f'{i} 0 obj\n{o}\nendobj\n'.encode()
    x = len(out)
    out += f'xref\n0 {len(objs) + 1}\n0000000000 65535 f \n'.encode() + b''.join(f'{o:010d} 00000 n \n'.encode() for o in offs)
    return out + f'trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF'.encode()


# ------------------------------------------------------------------ extraction
def test_text_and_markdown_keep_their_headings_and_odd_encodings_are_read():
    r = extract.extract('a.md', b'# Title\n\nSome text.\n\n## Part\n\nMore.')
    assert '# Title' in r.text and '## Part' in r.text
    assert 'café' in extract.extract('a.txt', 'café au lait'.encode('cp1252')).text
    assert 'hello' in extract.extract('a.txt', b'\xff\xfe' + 'hello'.encode('utf-16-le')).text


def test_html_drops_page_chrome_and_turns_headings_into_markdown():
    html = b'<html><head><title>Guide</title></head><body><nav>MENU</nav><div class="cookie-banner">COOKIES</div><main><h1>Plans</h1><p>Premium has seats.</p><h2>Billing</h2><p>Yearly.</p></main><footer>FOOT</footer><script>x=1</script></body></html>'
    r = extract.extract('p.html', html)
    assert r.title == 'Guide' and '# Plans' in r.text and '## Billing' in r.text and 'Premium has seats.' in r.text
    assert not any(w in r.text for w in ('MENU', 'COOKIES', 'FOOT', 'x=1'))


def test_tables_and_office_files_are_read_with_their_places():
    assert 'Row 1: name: Ann, city: Oslo' in extract.extract('t.csv', b'name,city\nAnn,Oslo\nBob,').text
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active; ws.title = 'Costs'; ws.append(['item', 'price']); ws.append(['tea', 3]); b = io.BytesIO(); wb.save(b)
    r = extract.extract('c.xlsx', b.getvalue())
    assert 'Sheet: Costs' in r.text and 'item: tea, price: 3' in r.text and r.pages == 1
    from docx import Document
    d = Document(); d.core_properties.title = 'Handbook'; d.add_heading('Refunds', 1); d.add_paragraph('Thirty days.'); t = d.add_table(rows=1, cols=2); t.rows[0].cells[0].text = 'a'; t.rows[0].cells[1].text = 'b'; b = io.BytesIO(); d.save(b)
    r = extract.extract('h.docx', b.getvalue())
    assert r.title == 'Handbook' and '# Refunds' in r.text and 'Thirty days.' in r.text and 'a | b' in r.text
    from pptx import Presentation
    p = Presentation(); s = p.slides.add_slide(p.slide_layouts[1]); s.shapes.title.text = 'Roadmap'; s.placeholders[1].text = 'Ship it'; b = io.BytesIO(); p.save(b)
    r = extract.extract('d.pptx', b.getvalue())
    assert r.title == 'Roadmap' and 'Slide 1' in r.text and 'Ship it' in r.text and r.pages == 1


def test_pdf_native_text_and_scanned_pages_with_ocr():
    r = extract.extract('n.pdf', make_pdf(['First page about refunds', 'Second page about plans']))
    assert 'Page 1\nFirst page about refunds' in r.text and 'Page 2' in r.text and r.pages == 2 and r.ocr_pages == 0
    seen = []
    r = extract.extract('s.pdf', make_pdf(['Typed page here', None]), ocr=lambda png, mime: (seen.append((png[:4], mime)) or 'Scanned words'))
    assert 'Page 2 (OCR)\nScanned words' in r.text and r.ocr_pages == 1 and seen == [(b'\x89PNG', 'image/png')]      # the page was really rendered to an image
    with pytest.raises(extract.ExtractError, match='no OCR model'):
        extract.extract('s.pdf', make_pdf([None]))
    with pytest.raises(extract.ExtractError, match='cannot be read'):
        extract.extract('bad.pdf', b'not a pdf at all')


def test_images_need_ocr_and_damaged_or_unsafe_files_are_refused(monkeypatch):
    from PIL import Image
    b = io.BytesIO(); Image.new('RGB', (20, 20), 'white').save(b, 'PNG')
    assert extract.extract('i.png', b.getvalue(), ocr=lambda png, mime: 'Words in a picture').text == 'Words in a picture'
    with pytest.raises(extract.ExtractError, match='OCR'):
        extract.extract('i.png', b.getvalue())
    with pytest.raises(extract.ExtractError, match='damaged'):
        extract.extract('x.docx', b'PK-not-really')
    z = io.BytesIO()
    with zipfile.ZipFile(z, 'w', zipfile.ZIP_DEFLATED) as f:
        f.writestr('word/big.xml', b'0' * 100000)
    monkeypatch.setattr(extract, 'MAX_ZIP_BYTES', 1000)
    with pytest.raises(extract.ExtractError, match='unreasonable'):
        extract.extract('bomb.docx', z.getvalue())
    with pytest.raises(extract.ExtractError, match='not supported'):
        extract.extract('a.exe', b'MZ')
    with pytest.raises(extract.ExtractError, match='no readable text'):
        extract.extract('e.txt', b'   \n\n  ')


def test_word_tables_stay_in_place_and_keep_each_value_with_its_column():
    from docx import Document
    d = Document(); d.add_paragraph('Limits are set in the table below.')
    t = d.add_table(rows=3, cols=3)
    for r, row in enumerate([['Tier', 'Max overtime', 'Audit'], ['Tier 1', '12', 'Every 12 months'], ['Tier 2', '', 'Every 24 months']]):
        for c, v in enumerate(row):
            t.rows[r].cells[c].text = v
    d.add_paragraph('Local law applies when it sets a lower limit.')
    b = io.BytesIO(); d.save(b)
    text = extract.extract('coc.docx', b.getvalue()).text
    assert text.index('table below') < text.index('Tier | Max overtime | Audit') < text.index('Local law applies')       # where it is in the document, not at the end
    assert 'Row 1: Tier: Tier 1; Max overtime: 12; Audit: Every 12 months' in text
    assert 'Row 2: Tier: Tier 2; Audit: Every 24 months' in text                     # the empty cell does not shift the audit into the overtime column
    d = Document(); t = d.add_table(rows=2, cols=3)
    t.rows[0].cells[0].merge(t.rows[0].cells[1]).text = 'Period'; t.rows[0].cells[2].text = 'Hours'
    t.rows[1].cells[0].text = 'Q1'; t.rows[1].cells[1].text = 'Jan-Mar'; t.rows[1].cells[2].text = '480'
    b = io.BytesIO(); d.save(b)
    text = extract.extract('m.docx', b.getvalue()).text
    assert 'Period | Hours' in text and 'Row 1: Period: Q1, Jan-Mar; Hours: 480' in text      # a merged header is read once and names both its columns


def test_powerpoint_tables_keep_their_columns():
    from pptx import Presentation
    from pptx.util import Inches
    p = Presentation(); s = p.slides.add_slide(p.slide_layouts[5]); s.shapes.title.text = 'Targets'
    tb = s.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(6), Inches(1)).table
    tb.cell(0, 0).text, tb.cell(0, 1).text, tb.cell(1, 0).text, tb.cell(1, 1).text = 'Region', 'Target', 'North', '1.2M'
    b = io.BytesIO(); p.save(b)
    assert 'Row 1: Region: North; Target: 1.2M' in extract.extract('t.pptx', b.getvalue()).text


def test_table_lines_handle_layout_tables_and_missing_headers():
    assert extract.table_lines([['Signed', 'Dated']]) == ['Signed | Dated']                # one row: plain line
    assert extract.table_lines([['a'], ['b']]) == ['a', 'b']                             # one column: plain lines
    assert extract.table_lines([['', 'Hours'], ['Ann', '7']]) == ['Hours', 'Row 1: Column 1: Ann; Hours: 7']
    assert extract.table_lines([['', ''], [' ', '']]) == []


def test_local_ocr_pages_are_marked_and_unsure_pages_are_left_out():
    def fake(png, mime):
        fake.n += 1
        return extract.OcrText('Clearly read words', 'local OCR', 91.0) if fake.n == 1 else extract.OcrText('M4x 43 h0urs', 'local OCR', 41.0, rejected=True)
    fake.n = 0
    r = extract.extract('s.pdf', make_pdf(['Typed page here', None, None]), ocr=fake)
    assert not any('gave no text' in n for n in r.notes)
    assert 'Page 2 (local OCR)\nClearly read words' in r.text and '43' not in r.text           # the unsure page is not a source
    assert r.ocr_pages == 1 and r.local_ocr_pages == 1
    assert any('read with local OCR' in n for n in r.notes) and any('left out' in n and '3: 41%' in n for n in r.notes)
    assert chunking.MARKER.match('Page 2 (local OCR)')
    r = extract.extract('b.pdf', make_pdf(['Typed page here', None]), ocr=lambda png, mime: extract.OcrText('', 'local OCR'))
    assert any('1 scanned page(s) gave no text when read (2)' in n for n in r.notes)                                  # passages still carry their page
    from PIL import Image
    b = io.BytesIO(); Image.new('RGB', (20, 20), 'white').save(b, 'PNG')
    with pytest.raises(extract.ExtractError, match='too little confidence'):
        extract.extract('i.png', b.getvalue(), ocr=lambda png, mime: extract.OcrText('blurry', 'local OCR', 30.0, rejected=True))
    r = extract.extract('i.png', b.getvalue(), ocr=lambda png, mime: extract.OcrText('A sign', 'local OCR', 88.0))
    assert r.text == 'A sign' and r.local_ocr_pages == 1


def test_tesseract_output_is_read_with_its_confidence(monkeypatch):
    head = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
    rows = [(1, 1, 1, 1, '96.5', 'Records'), (1, 1, 1, 2, '93.0', 'kept'), (1, 1, 2, 1, '40.0', 'five'), (1, 1, 2, 2, '-1', ''), (2, 1, 1, 1, '90.0', 'years')]
    tsv = head + ''.join(f'5\t1\t{b}\t{p}\t{l}\t{w}\t0\t0\t1\t1\t{c}\t{t}\n' for b, p, l, w, c, t in rows)
    text, conf = localocr.parse_tsv(tsv)
    assert text == 'Records kept\nfive\n\nyears' and round(conf, 1) == round((96.5 * 7 + 93 * 4 + 40 * 4 + 90 * 5) / 20, 1)
    assert localocr.parse_tsv(head) == ('', None)
    monkeypatch.setattr(localocr, 'installed_languages', lambda: {'eng', 'ita'})
    assert localocr.languages('ita, xyz;eng') == 'ita+eng' and localocr.languages('') == 'eng' and localocr.languages('ara') == 'eng'
    with pytest.raises(RuntimeError, match='only PNG'):
        localocr.read(b'GIF89a...')


@pytest.mark.skipif(not localocr.available(), reason='Tesseract is not installed here (it is in the image)')
def test_tesseract_reads_a_clear_page_and_doubts_noise():
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new('RGB', (1400, 300), 'white'); dr = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype('DejaVuSans.ttf', 44)
    except OSError:
        font = ImageFont.load_default(size=44)
    dr.text((40, 60), 'Overtime must not exceed 12 hours per week.', fill='black', font=font)
    b = io.BytesIO(); img.save(b, 'PNG')
    r = localocr.read(b.getvalue())
    assert '12 hours' in r.text and r.label == 'local OCR' and r.confidence > 80 and not r.rejected
    import random
    rnd = random.Random(1); noise = Image.new('L', (600, 300))
    noise.putdata([rnd.choice((0, 255)) for _ in range(600 * 300)])
    b = io.BytesIO(); noise.convert('RGB').save(b, 'PNG')
    r = localocr.read(b.getvalue())
    assert not r.text or r.rejected


def test_scans_are_read_locally_without_an_ai_model_and_the_source_says_so(client, world, monkeypatch):
    from studio import llm
    monkeypatch.setattr(llm, 'available', lambda: False)
    monkeypatch.setattr(localocr, 'available', lambda: True)
    monkeypatch.setattr(localocr, 'read', lambda png, langs, conf: extract.OcrText('Scanned policy text about refunds', 'local OCR', 90.0))
    a = world['a']['id']
    _as(client, 'ann'); _upload(client, a, 'scan.pdf', make_pdf([None]))
    _run()
    src = client.get(f'/api/library/collections/{a}/sources').json()['sources'][0]
    assert src['status'] == 'ready' and src['stats']['ocr_pages'] == 1 and src['stats']['local_ocr_pages'] == 1
    assert any('local OCR' in n for n in src['stats']['notes'])
    from studio import settings
    settings.put('OCR_LOCAL', False, 't')
    try:
        _upload(client, a, 'scan2.pdf', make_pdf([None, None])); _run()      # another file: the same bytes would be refused as a duplicate
        src = [s for s in client.get(f'/api/library/collections/{a}/sources').json()['sources'] if s['name'] == 'scan2.pdf'][0]
        assert src['status'] == 'failed'                                                 # turned off: skipped as before
    finally:
        settings.reset('OCR_LOCAL', 't')


def test_a_failing_ai_model_falls_back_to_local_ocr_page_by_page(client, world, monkeypatch):
    from studio import llm
    monkeypatch.setattr(llm, 'available', lambda: True)
    def broken(png, mime):
        raise TimeoutError('the model did not answer')
    monkeypatch.setattr(llm, 'ocr', broken)
    monkeypatch.setattr(localocr, 'available', lambda: True)
    monkeypatch.setattr(localocr, 'read', lambda png, langs, conf: extract.OcrText('Read on the server instead', 'local OCR', 88.0))
    monkeypatch.setattr('studio.library.wiki.run', lambda *a, **k: 'skipped')         # no source page is written in this test
    from studio import embeddings
    monkeypatch.setattr(embeddings, 'available', lambda: False)
    a = world['a']['id']
    _as(client, 'ann'); _upload(client, a, 'fallback.pdf', make_pdf([None]))
    _run()
    src = client.get(f'/api/library/collections/{a}/sources').json()['sources'][0]
    assert src['status'] == 'ready' and src['stats']['local_ocr_pages'] == 1
    assert any('read with local OCR instead' in n for n in src['stats']['notes'])


# ------------------------------------------------------------------ chunking
def test_passages_fit_the_embedding_model_and_carry_their_place(monkeypatch):
    monkeypatch.setattr(config, 'EMBEDDING_MAX_TOKENS', 128)
    assert chunking.target_tokens() == 96
    body = '\n\n'.join(f'Sentence number {i} says something useful about the plan. ' * 3 for i in range(40))
    text = f'Page 1\n\n# Plans\n\n{body}\n\nPage 2\n\n## Billing\n\n{body[:600]}'
    parts = chunking.chunk(text)
    assert len(parts) > 6 and all(p['tokens'] <= 96 + 8 for p in parts)
    assert parts[0]['location'] == 'Page 1' and parts[0]['heading_path'] == ['Plans']
    assert parts[-1]['location'] == 'Page 2' and parts[-1]['heading_path'] == ['Plans', 'Billing']
    assert [p['idx'] for p in parts] == list(range(len(parts)))
    assert all(text[p['char_start']:p['char_start'] + 20].strip() for p in parts)
    assert any(parts[i]['text'].split('\n\n')[0] in parts[i - 1]['text'] for i in range(1, len(parts)) if parts[i]['location'] == parts[i - 1]['location'])   # overlap
    huge = chunking.chunk('x' * 5000 + ' ' + 'word ' * 800)                       # no paragraph break, no sentence ends
    assert all(p['tokens'] <= 96 + 8 for p in huge) and len(huge) > 10
    assert chunking.chunk('   ') == []


# ------------------------------------------------------------------ the whole pipeline
def fake_embed(texts):
    """Words hashed into a vector: texts sharing words are close. Stands in for a real model."""
    import hashlib, math
    out = []
    for t in texts:
        v = [0.0] * config.EMBEDDING_DIM
        for w in t.lower().split():
            v[int(hashlib.md5(w.strip('.,?!').encode()).hexdigest(), 16) % config.EMBEDDING_DIM] += 1
        n = math.sqrt(sum(x * x for x in v)) or 1
        out.append([x / n for x in v])
    return out


@pytest.fixture
def embedded(monkeypatch):
    monkeypatch.setattr(embeddings, 'available', lambda: True)
    monkeypatch.setattr(embeddings, 'embed', fake_embed)
    monkeypatch.setattr(embeddings, 'embed_query', lambda q: fake_embed([q])[0])


def _run():
    while jobs.run_one('t'):
        pass


def test_a_document_is_read_split_embedded_and_found(client, world, embedded):
    a = world['a']['id']
    _as(client, 'ann')
    doc = b'# Refunds\n\nA yearly subscription can be cancelled within thirty days for a full refund.\n\n# Seats\n\nThe premium plan includes unlimited seats and priority support.'
    sid = _upload(client, a, 'policy.md', doc).json()['id']
    _run()
    src = client.get(f'/api/library/collections/{a}/sources').json()['sources'][0]
    assert src['status'] == 'ready' and src['stats']['passages'] >= 1 and src['stats']['embedded'] == src['stats']['passages']
    ps = client.get(f'/api/library/collections/{a}/sources/{sid}/passages').json()
    assert ps['total'] == src['stats']['passages'] and ps['passages'][0]['embedded'] is True
    r = client.get(f'/api/library/collections/{a}/search', params={'q': 'how many days to cancel for a refund'}).json()
    assert r['usage'] == 'public' and 'may be shared with customers' in r['usage_note']
    assert r['passages'][0]['source'] == 'policy.md' and 'refund' in r['passages'][0]['text'] and 'meaning' in r['passages'][0]['found_by']
    assert 0 < r['passages'][0]['similarity'] <= 1
    from studio import settings
    settings.put('LIBRARY_MIN_SIMILARITY', 0.3, 't')
    try:
        assert client.get(f'/api/library/collections/{a}/search', params={'q': 'qzxv'}).json()['passages'] == []        # nothing close enough: no answer rather than a weak one
    finally:
        settings.reset('LIBRARY_MIN_SIMILARITY', 't')


def test_keywords_work_without_an_embedding_model(client, world):
    a = world['a']['id']
    _as(client, 'ann'); _upload(client, a, 'k.txt', b'Invoices are issued on the first day of every month.')
    _run()
    src = client.get(f'/api/library/collections/{a}/sources').json()['sources'][0]
    assert src['status'] == 'ready' and src['stats']['embedded'] == 0 and any('keywords only' in n for n in src['stats']['notes'])
    assert client.get(f'/api/library/collections/{a}/search', params={'q': 'invoices month'}).json()['passages'][0]['source'] == 'k.txt'


def test_an_unreadable_file_fails_once_with_a_plain_reason(client, world, monkeypatch):
    monkeypatch.setattr(localocr, 'available', lambda: False)           # whether or not Tesseract is installed where the suite runs
    a = world['a']['id']
    _as(client, 'ann'); _upload(client, a, 'blank.pdf', make_pdf([None]))
    _run()
    src = client.get(f'/api/library/collections/{a}/sources').json()['sources'][0]
    assert src['status'] == 'failed' and 'no OCR model' in src['error'] and src['job']['status'] == 'failed' and src['job']['attempts'] == 1       # not retried


def test_search_and_passages_never_cross_collections(client, world, embedded):
    a, b = world['a']['id'], world['b']['id']
    with db.conn() as c:
        c.execute("INSERT INTO library.grant_ (collection_id, username, level) VALUES (%s,'bob','read') ON CONFLICT DO NOTHING", (b,))
    _as(client, 'ann'); sa = _upload(client, a, 'a.txt', f'Alpha only secret {MARK_A} lives here'.encode()).json()['id']
    with db.conn() as c:
        c.execute("UPDATE library.grant_ SET level='manage' WHERE collection_id=%s AND username='ann'", (a,))
    _admin(client); client.post(f'/api/library/collections/{b}/self-grant', json={'level': 'manage'}); client.post(f'/api/library/collections/{b}/grants', json={})
    with db.conn() as c:
        c.execute("INSERT INTO library.grant_ (collection_id, username, level) VALUES (%s,'ed','write') ON CONFLICT DO NOTHING", (b,))
    _as(client, 'ed'); _upload(client, b, 'b.txt', f'Bravo only secret {MARK_B} lives here'.encode())
    _run()
    _as(client, 'bob')                                                          # reads A and B
    ra = client.get(f'/api/library/collections/{a}/search', params={'q': 'secret lives here'}).json()
    rb = client.get(f'/api/library/collections/{b}/search', params={'q': 'secret lives here'}).json()
    assert MARK_A in str(ra) and MARK_B not in str(ra) and MARK_B in str(rb) and MARK_A not in str(rb)
    assert client.get(f'/api/library/collections/{b}/sources/{sa}/passages').status_code == 404        # A's source id under B's collection means nothing
    _as(client, 'eve')
    assert client.get(f'/api/library/collections/{a}/search', params={'q': 'secret'}).status_code == 404
    # the database itself refuses a passage that names a source from another collection
    with db.conn() as c:
        with pytest.raises(Exception, match='foreign key'):
            c.execute("INSERT INTO library.source_chunk (collection_id,source_id,idx,text,tokens) VALUES (%s,%s,99,'x',1)", (b, sa))


def test_the_passage_table_follows_the_embedding_dimension():
    with db.conn() as c:
        c.execute('SAVEPOINT s')
        assert vectors.ensure_library_chunks(c) is False
        other = config.EMBEDDING_DIM + 128
        assert vectors.ensure_library_chunks(c, other) is True
        assert c.execute("SELECT format_type(atttypid, atttypmod) AS t FROM pg_attribute WHERE attrelid='library.source_chunk'::regclass AND attname='embedding'").fetchone()['t'] == vectors.spec(other)['type']
        assert vectors.ensure_library_chunks(c, 3000) is True                       # beyond 2000: half precision, still indexed
        assert 'halfvec(3000)' in c.execute("SELECT format_type(atttypid, atttypmod) AS t FROM pg_attribute WHERE attrelid='library.source_chunk'::regclass AND attname='embedding'").fetchone()['t']
        assert c.execute("SELECT count(*) AS n FROM pg_indexes WHERE indexname='source_chunk_embedding'").fetchone()['n'] == 1
        assert vectors.ensure_library_chunks(c, 5000) is True                       # beyond 4000: no index, exact search
        assert c.execute("SELECT count(*) AS n FROM pg_indexes WHERE indexname='source_chunk_embedding'").fetchone()['n'] == 0
        c.execute('ROLLBACK TO SAVEPOINT s')
