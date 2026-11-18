"""Read a page image with Tesseract, the open-source OCR program installed in the image: no AI model is called and nothing leaves the server.
It is the fallback when no AI model reads images (none is set, or the collection does not allow AI) and when the model fails on a page.

Tesseract misreads more than a vision model, above all on tables, columns, handwriting and poor photos. A misread number would pass the
Library's check, because claims are checked against the text that was read, so every word's confidence is kept: a page read with too little
confidence is left out with a note rather than becoming a source, and a page that is kept is marked "local OCR".

Only PNG images the Library made itself are passed (rendered PDF pages, or uploads re-encoded by Pillow), on stdin, never a file name or URL."""
import csv
import io
import os
import re
import shutil
import subprocess
import threading

from .extract import OcrText

TIMEOUT = 120                                     # seconds per page
LABEL = 'local OCR'
_langs = {'value': None}
_lock = threading.Lock()


def binary():
    return shutil.which('tesseract')


def available():
    return binary() is not None and bool(installed_languages())


def installed_languages():
    """The language packs Tesseract has, read once ('osd' is page orientation, not a language)."""
    with _lock:
        if _langs['value'] is None:
            found = set()
            if binary():
                try:
                    out = subprocess.run([binary(), '--list-langs'], capture_output=True, text=True, timeout=20, env=_env()).stdout
                    found = {l.strip() for l in out.splitlines()[1:] if l.strip() and l.strip() != 'osd'}
                except (OSError, subprocess.SubprocessError):
                    found = set()
            _langs['value'] = found
        return _langs['value']


def languages(wanted):
    """'eng, ita' -> 'eng+ita', keeping only the installed ones; English when none of them is installed."""
    have = installed_languages()
    picked = [l for l in re.split(r'[\s,+;]+', wanted or '') if l in have]
    if not picked:
        picked = ['eng'] if 'eng' in have else sorted(have)[:1]
    return '+'.join(dict.fromkeys(picked))


def _env():
    return {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'OMP_THREAD_LIMIT': '1', 'LANG': 'C.UTF-8'}     # one core per page: workers run several at once


def parse_tsv(tsv):
    """Tesseract's TSV -> (text with its lines and paragraphs, confidence 0-100 weighted by word length, or None when no word was read)."""
    lines, weight, total = {}, 0, 0.0
    for row in csv.DictReader(io.StringIO(tsv), delimiter='\t', quoting=csv.QUOTE_NONE):
        word = (row.get('text') or '').strip()
        try:
            conf = float(row.get('conf') or -1)
        except ValueError:
            conf = -1
        if not word or conf < 0:
            continue
        key = (int(row['block_num']), int(row['par_num']), int(row['line_num']))
        lines.setdefault(key, []).append(word)
        weight += len(word)
        total += conf * len(word)
    if not weight:
        return '', None
    out, prev = [], None
    for key in sorted(lines):
        if prev is not None and key[:2] != prev[:2]:
            out.append('')                                                # a new paragraph
        out.append(' '.join(lines[key]))
        prev = key
    return '\n'.join(out).strip(), total / weight


def read(png, wanted_languages='eng', min_confidence=70):
    """-> OcrText. Raises RuntimeError when Tesseract fails or takes too long (the caller notes it and goes on with the other pages)."""
    if not png.startswith(b'\x89PNG'):
        raise RuntimeError('local OCR reads only PNG images made by the Library')
    try:
        proc = subprocess.run([binary(), 'stdin', 'stdout', '-l', languages(wanted_languages), '--psm', '3', 'tsv'],
                              input=png, capture_output=True, timeout=TIMEOUT, env=_env())
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'local OCR took longer than {TIMEOUT} s')
    except (OSError, TypeError) as e:
        raise RuntimeError(f'local OCR could not start ({type(e).__name__})')
    if proc.returncode != 0:
        raise RuntimeError('local OCR failed: ' + proc.stderr.decode('utf-8', 'replace').strip()[-160:])
    text, conf = parse_tsv(proc.stdout.decode('utf-8', 'replace'))
    if not text:
        return OcrText('', LABEL, None)
    return OcrText(text, LABEL, round(conf, 1), rejected=conf < min_confidence)
