"""The optional document converter: LibreOffice in a container of its own (docker-compose.converter.yml, the Gotenberg image). It draws what Python
cannot: a PowerPoint slide whose diagram is built from shapes, and pictures stored as EMF or WMF (Visio drawings and pasted diagrams in Office files).
Without it those figures are counted as needing the converter and everything else is read as usual.

The container has no database, no files, no app secrets and no way out to the internet (an internal network shared only with the app and the
worker), so a document that links to outside resources cannot make it fetch them. Only Office files and pictures the Library is reading are sent."""
import httpx

from .. import settings

TIMEOUT = 180
USER = 'curltale'


def available():
    return bool(settings.get('CONVERTER_URL'))


def _auth():
    token = settings.get('CONVERTER_TOKEN')
    return (USER, token) if token else None


def to_pdf(filename, data):
    """A file -> its PDF, as LibreOffice prints it. Raises RuntimeError with a short reason."""
    base = (settings.get('CONVERTER_URL') or '').rstrip('/')
    if not base:
        raise RuntimeError('no document converter is set')
    try:
        r = httpx.post(f'{base}/forms/libreoffice/convert', files={'files': (filename, data)}, auth=_auth(), timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise RuntimeError(f'the document converter could not be reached ({type(e).__name__})')
    if r.status_code != 200:
        raise RuntimeError(f'the document converter answered {r.status_code}')
    if not r.content.startswith(b'%PDF'):
        raise RuntimeError('the document converter did not return a PDF')
    return r.content


def check():
    """For the Settings page: {'ok', 'detail' | 'error'}."""
    base = (settings.get('CONVERTER_URL') or '').rstrip('/')
    if not base:
        return {'ok': False, 'error': 'no address set'}
    try:
        r = httpx.get(f'{base}/health', auth=_auth(), timeout=10)
        if r.status_code != 200:
            return {'ok': False, 'error': f'answered {r.status_code}'}
        up = ((r.json().get('details') or {}).get('libreoffice') or {}).get('status')
        return {'ok': up == 'up', 'detail': 'LibreOffice is up'} if up == 'up' else {'ok': False, 'error': f'LibreOffice is {up or "not reported"}'}
    except Exception as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {str(e)[:160]}'}
