"""The browser sidecar: renders a web page with a real browser so that pages built with JavaScript can be read. It runs in its own container
(Dockerfile.fetcher) that has no access to the database, the files or the secrets of the app. `python -m studio.fetcher`.

Safety: the browser itself never touches the network. Every request it makes is intercepted and answered by our own checked fetch (netguard:
public addresses only, the IP pinned, redirects re-checked), and the browser is started with a proxy that goes nowhere, so anything that slips
past the interception (a WebSocket, a prefetch) fails; loopback is not exempt from that. Only GET is served, images, media and fonts are not loaded, and the number of requests
and bytes per page is capped. The app calls it with a shared token (FETCHER_TOKEN)."""
import hmac
import os
import threading
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

try:
    from .library import netguard
except ImportError:                                  # in the sidecar image the file sits next to this one
    import netguard  # type: ignore

TOKEN = os.environ.get('FETCHER_TOKEN', '')
ALLOWED = netguard.parse_allowed(os.environ.get('FETCH_ALLOWED_HOSTS', ''))
MAX_REQUESTS = int(os.environ.get('FETCH_MAX_REQUESTS', '60'))
MAX_BYTES = int(os.environ.get('FETCH_MAX_MB', '25')) * 1024 * 1024
BLOCKED_TYPES = {'image', 'media', 'font'}
app = FastAPI(title='Library browser service')
_gate = threading.Semaphore(int(os.environ.get('FETCH_PARALLEL', '2')))     # a browser page is heavy: a few at a time


class Req(BaseModel):
    url: str


@app.get('/health')
def health():
    return {'ok': True}


def render(url):
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    netguard.resolve(url, ALLOWED)
    spent = {'n': 0, 'bytes': 0}
    with sync_playwright() as p:
        # a proxy that goes nowhere, with no bypass even for loopback: whatever the interception below does not answer (a WebSocket, a prefetch) fails
        args = ['--proxy-server=http://127.0.0.1:9', '--proxy-bypass-list=<-loopback>', '--disable-dev-shm-usage'] + (['--no-sandbox'] if os.environ.get('FETCH_NO_SANDBOX') else [])
        browser = p.chromium.launch(args=args)
        try:
            ctx = browser.new_context(service_workers='block', accept_downloads=False, java_script_enabled=True, user_agent=netguard.USER_AGENT)

            def handle(route):
                req = route.request
                spent['n'] += 1
                if req.method != 'GET' or req.resource_type in BLOCKED_TYPES or spent['n'] > MAX_REQUESTS or spent['bytes'] > MAX_BYTES:
                    return route.abort()
                try:
                    final, status, headers, body = netguard.get(req.url, ALLOWED, MAX_BYTES - spent['bytes'], timeout=20)
                except Exception:
                    return route.abort()
                spent['bytes'] += len(body)
                keep = {k: v for k, v in headers.items() if k.lower() in ('content-type', 'content-language')}
                route.fulfill(status=status, headers=keep, body=body)

            ctx.route('**/*', handle)
            page = ctx.new_page()
            page.goto(url, wait_until='load', timeout=45000)
            try:
                page.wait_for_load_state('networkidle', timeout=4000)
            except PWTimeout:
                pass
            return {'final_url': page.url, 'title': page.title() or None, 'html': page.content()}
        finally:
            browser.close()


@app.post('/render')
def render_endpoint(req: Req, x_fetch_token: str = Header(default='')):
    if not TOKEN or not hmac.compare_digest(x_fetch_token, TOKEN):
        raise HTTPException(401, 'bad token')
    try:
        netguard.resolve(req.url, ALLOWED)
    except netguard.Refused as e:
        raise HTTPException(422, str(e))
    with _gate:
        try:
            return render(req.url)
        except netguard.Refused as e:
            raise HTTPException(422, str(e))
        except Exception as e:
            raise HTTPException(502, f'the page could not be rendered ({type(e).__name__})')


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8001)
