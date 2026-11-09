"""Getting a web address as a document. A page is fetched safely (netguard); a page that builds itself with JavaScript can be rendered by the
browser sidecar when one is set up; an address that is a file (PDF, Word...) is downloaded as that file."""
import re
from dataclasses import dataclass
from email.message import Message
from urllib.parse import unquote, urlsplit

import httpx

from .. import settings
from . import extract, netguard

MB = 1024 * 1024
TYPES = {'text/html': 'html', 'application/xhtml+xml': 'html', 'application/pdf': 'pdf', 'text/plain': 'txt', 'text/markdown': 'md', 'text/csv': 'csv',
         'application/json': 'json', 'application/xml': 'xml', 'text/xml': 'xml',
         'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx', 'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'pptx',
         'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx', 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp'}
THIN_TEXT = 500                      # a page whose plain HTML yields less text than this is probably built by JavaScript


class FetchError(Exception):
    """The address could not be turned into a document; the message is shown to the person who asked."""


@dataclass
class Fetched:
    final_url: str
    filename: str
    content: bytes
    title: str | None = None
    rendered: bool = False


def _charset(headers):
    m = Message()
    m['content-type'] = headers.get('content-type', '')
    return m.get_content_charset()


def allowed_hosts():
    return netguard.parse_allowed(settings.get('FETCH_ALLOWED_HOSTS'))


def render(url):
    """Ask the browser sidecar for the page as a browser shows it. -> (html, title, final_url)"""
    base, token = (settings.get('FETCHER_URL') or '').rstrip('/'), settings.get('FETCHER_TOKEN')
    try:
        r = httpx.post(f'{base}/render', json={'url': url}, headers={'X-Fetch-Token': token or ''}, timeout=120)
    except httpx.HTTPError as e:
        raise FetchError(f'the browser service could not be reached ({type(e).__name__})')
    if r.status_code != 200:
        try:
            detail = r.json().get('detail')
        except ValueError:
            detail = None
        raise FetchError(f'the browser service refused: {detail or r.status_code}')
    j = r.json()
    return j['html'], j.get('title'), j.get('final_url') or url


def fetch(url):
    try:
        final, status, headers, body = netguard.get(url, allowed_hosts(), settings.get('FETCH_MAX_MB') * MB)
    except netguard.Refused as e:
        raise FetchError(str(e))
    if status >= 400:
        raise FetchError(f'the web server answered {status}')
    ctype = headers.get('content-type', '').split(';')[0].strip().lower()
    cd = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', headers.get('content-disposition', ''), re.I)
    name = unquote(cd.group(1)) if cd else unquote(urlsplit(final).path.rsplit('/', 1)[-1])
    ext = TYPES.get(ctype) or (name.rsplit('.', 1)[-1].lower() if '.' in name else '')
    if ext in ('html', 'htm') or (not ext and body[:200].lstrip().lower().startswith((b'<!doctype html', b'<html'))):
        html = body.decode(_charset(headers) or 'utf-8', errors='replace')
        text, title = extract.html_to_text(html)
        mode, used = settings.get('FETCH_RENDER'), False
        if settings.get('FETCHER_URL') and (mode == 'always' or (mode == 'auto' and len(text) < THIN_TEXT)):
            html, t2, final = render(final)
            if len(html.encode()) > settings.get('FETCH_MAX_MB') * MB:
                raise FetchError('the rendered page is too large')
            title, used = t2 or title, True
        return Fetched(final, 'page.html', html.encode(), title, used)
    if ext not in extract.SUPPORTED:
        raise FetchError(f"that address is a '{ctype or ext or 'unknown'}' file, which is not supported")
    from .storage import clean_name
    return Fetched(final, clean_name(name if name.lower().endswith('.' + ext) else f'document.{ext}'), body)
