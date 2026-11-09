import socket, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import httpx, pytest
from studio import db, settings
from studio.library import fetch, jobs, netguard
from test_library_access import world, _as, _admin   # noqa: F401


def resolver(monkeypatch, table):
    real = socket.getaddrinfo

    def fake(host, port, *a, **k):
        if host in table:
            return [(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port or 0)) for ip in table[host]]
        return real(host, port, *a, **k)
    monkeypatch.setattr(socket, 'getaddrinfo', fake)


# ------------------------------------------------------------------ the guard
@pytest.mark.parametrize('url', ['http://127.0.0.1/', 'http://localhost/', 'http://10.1.2.3/x', 'http://192.168.0.10/', 'http://172.16.5.5/', 'http://169.254.169.254/latest/meta-data/',
                                 'http://[::1]/', 'http://0.0.0.0/', 'http://2130706433/', 'http://0x7f.1/', 'http://[::ffff:127.0.0.1]/', 'http://100.64.0.1/', 'http://[fd00::1]/'])
def test_private_and_internal_addresses_are_refused(url):
    with pytest.raises(netguard.Refused, match='private or internal|could not be found'):
        netguard.resolve(url)


@pytest.mark.parametrize('url,msg', [('ftp://example.com/', 'only http'), ('file:///etc/passwd', 'only http'), ('http://user:pw@example.com/', 'user name'), ('javascript:alert(1)', 'only http'), ('http:///x', 'no host|valid'), ('', 'only http')])
def test_other_schemes_and_credentials_are_refused(url, msg):
    with pytest.raises(netguard.Refused, match=msg):
        netguard.resolve(url)


def test_a_name_with_any_private_address_is_refused_and_allowed_hosts_are_exact(monkeypatch):
    resolver(monkeypatch, {'public.test': ['93.184.216.34'], 'mixed.test': ['93.184.216.34', '10.0.0.7'], 'intranet.test': ['10.9.8.7']})
    assert netguard.resolve('https://public.test/a')[:3] == ('https', 'public.test', 443)
    with pytest.raises(netguard.Refused):
        netguard.resolve('https://mixed.test/')                                                   # rebinding trick: one good answer, one internal one
    with pytest.raises(netguard.Refused):
        netguard.resolve('https://intranet.test/')
    assert netguard.resolve('https://intranet.test/', netguard.parse_allowed('intranet.test'))[3].exploded.startswith('10.9')
    assert netguard.resolve('https://intranet.test/', netguard.parse_allowed('10.0.0.0/8'))
    with pytest.raises(netguard.Refused):
        netguard.resolve('https://intranet.test/', netguard.parse_allowed('other.test, 192.168.0.0/16'))


def test_the_connection_goes_to_the_checked_ip_and_every_redirect_is_checked_again(monkeypatch):
    resolver(monkeypatch, {'public.test': ['93.184.216.34'], 'evil.test': ['93.184.216.35']})
    seen = []

    def handler(req):
        seen.append((str(req.url), req.headers['host'], req.extensions.get('sni_hostname')))
        if req.url.path == '/go':
            return httpx.Response(302, headers={'location': 'https://evil.test/next'})
        if req.url.path == '/private':
            return httpx.Response(302, headers={'location': 'http://127.0.0.1:8000/admin'})
        if req.url.path == '/loop':
            return httpx.Response(302, headers={'location': '/loop'})
        return httpx.Response(200, headers={'content-type': 'text/html'}, content=b'<p>hello</p>')
    t = httpx.MockTransport(handler)
    final, status, headers, body = netguard.get('https://public.test/page', transport=t)
    assert seen[0] == ('https://93.184.216.34/page', 'public.test', 'public.test')                 # IP in the URL, name kept for Host and TLS
    final, *_ = netguard.get('https://public.test/go', transport=t)
    assert final == 'https://evil.test/next' and seen[-1][0] == 'https://93.184.216.35/next'
    with pytest.raises(netguard.Refused, match='private or internal'):
        netguard.get('https://public.test/private', transport=t)                                   # a public page may not bounce us inward
    with pytest.raises(netguard.Refused, match='too many redirects'):
        netguard.get('https://public.test/loop', transport=t)


def test_oversized_pages_are_refused(monkeypatch):
    resolver(monkeypatch, {'public.test': ['93.184.216.34']})
    big = httpx.MockTransport(lambda r: httpx.Response(200, content=b'x' * 3000))
    with pytest.raises(netguard.Refused, match='larger than'):
        netguard.get('https://public.test/', max_bytes=2048, transport=big)
    declared = httpx.MockTransport(lambda r: httpx.Response(200, headers={'content-length': '99999999999'}, content=b'x'))
    with pytest.raises(netguard.Refused, match='larger than'):
        netguard.get('https://public.test/', max_bytes=1024 * 1024, transport=declared)


# ------------------------------------------------------------------ turning an address into a document
def test_pages_files_and_errors(client, monkeypatch):
    monkeypatch.setattr(netguard, 'get', lambda url, allowed=(), max_bytes=0, **k: PAGES[url])
    html = b'<html><head><title>T</title></head><body><main><h1>Plans</h1><p>' + b'Premium has unlimited seats. ' * 30 + b'</p></main></body></html>'
    PAGES = {'https://a.test/p': ('https://a.test/p', 200, {'content-type': 'text/html; charset=utf-8'}, html),
             'https://a.test/doc.pdf': ('https://a.test/doc.pdf', 200, {'content-type': 'application/pdf'}, b'%PDF-1.4 x'),
             'https://a.test/dl': ('https://a.test/dl', 200, {'content-type': 'application/octet-stream', 'content-disposition': 'attachment; filename="Report 2024.docx"'}, b'PK'),
             'https://a.test/bin': ('https://a.test/bin', 200, {'content-type': 'application/zip'}, b'PK'),
             'https://a.test/gone': ('https://a.test/gone', 404, {}, b''),
             'https://a.test/thin': ('https://a.test/thin', 200, {'content-type': 'text/html'}, b'<html><body><div id="app"></div><script>x</script></body></html>')}
    f = fetch.fetch('https://a.test/p')
    assert f.filename == 'page.html' and f.title == 'T' and not f.rendered and b'Premium' in f.content
    assert fetch.fetch('https://a.test/doc.pdf').filename == 'doc.pdf' and fetch.fetch('https://a.test/dl').filename == 'Report 2024.docx'
    with pytest.raises(fetch.FetchError, match='not supported'):
        fetch.fetch('https://a.test/bin')
    with pytest.raises(fetch.FetchError, match='answered 404'):
        fetch.fetch('https://a.test/gone')
    # a page with no text of its own is rendered by the browser service, when there is one and it is wanted
    rendered = []
    monkeypatch.setattr(fetch, 'render', lambda url: (rendered.append(url) or '<html><title>R</title><body><main>' + 'Built by script. ' * 40 + '</main></body></html>', 'R', url))
    assert not fetch.fetch('https://a.test/thin').rendered                                         # no service set: plain fetch
    settings.put('FETCHER_URL', 'http://fetcher:8001', 't')
    try:
        r = fetch.fetch('https://a.test/thin')
        assert r.rendered and r.title == 'R' and rendered == ['https://a.test/thin'] and b'Built by script' in r.content
        fetch.fetch('https://a.test/p'); assert len(rendered) == 1                                    # enough text already: not rendered
        settings.put('FETCH_RENDER', 'never', 't'); assert not fetch.fetch('https://a.test/thin').rendered
        settings.put('FETCH_RENDER', 'always', 't'); assert fetch.fetch('https://a.test/p').rendered
    finally:
        for k in ('FETCHER_URL', 'FETCH_RENDER'):
            settings.reset(k, 't')


# ------------------------------------------------------------------ end to end with a real local web server
PAGE = ('<html><head><title>Plan guide</title></head><body><nav>MENU LINKS</nav><main><h1>Premium plan</h1><p>The premium plan includes unlimited seats and priority support. '
        'It can be cancelled within thirty days for a full refund.</p></main><footer>FOOTER JUNK</footer></body></html>')


@pytest.fixture
def site(monkeypatch):
    state = {'body': PAGE, 'hits': 0}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            state['hits'] += 1
            if self.path == '/pdf':
                self.send_response(200); self.send_header('content-type', 'application/pdf'); self.end_headers(); self.wfile.write(b'%PDF-1.4'); return
            if self.path == '/missing':
                self.send_response(404); self.end_headers(); return
            self.send_response(200); self.send_header('content-type', 'text/html; charset=utf-8'); self.end_headers(); self.wfile.write(state['body'].encode())

        def log_message(self, *a):
            pass
    srv = HTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state['base'] = f'http://127.0.0.1:{srv.server_port}'
    yield state
    srv.shutdown()


def _run():
    while jobs.run_one('t'):
        pass


def test_a_web_address_becomes_a_source_and_an_unchanged_recapture_does_nothing(client, world, site):
    a = world['a']['id']
    _as(client, 'ann')
    r = client.post(f'/api/library/collections/{a}/sources/url', json={'url': site['base'] + '/guide'})
    assert r.status_code == 422 and 'private or internal' in r.json()['detail']                   # the test server is on loopback: refused by default
    settings.put('FETCH_ALLOWED_HOSTS', '127.0.0.1', 't')
    try:
        r = client.post(f'/api/library/collections/{a}/sources/url', json={'url': site['base'] + '/guide'})
        assert r.status_code == 200
        sid = r.json()['id']
        assert client.post(f'/api/library/collections/{a}/sources/url', json={'url': site['base'] + '/guide'}).status_code == 409
        _run()
        s = client.get(f'/api/library/collections/{a}/sources').json()['sources'][0]
        assert s['status'] == 'ready' and s['kind'] == 'url' and s['title'] == 'Plan guide' and s['stats']['passages'] >= 1 and 'without running JavaScript' in ' '.join(s['stats']['notes'])
        hits = client.get(f'/api/library/collections/{a}/search', params={'q': 'unlimited seats'}).json()['passages']
        assert hits and 'unlimited seats' in hits[0]['text'] and 'MENU' not in hits[0]['text'] and 'FOOTER' not in hits[0]['text']
        assert client.get(f'/api/library/collections/{a}/sources/{sid}/download/original').content.startswith(b'<html>')       # the page as captured is kept
        n = site['hits']
        assert client.post(f'/api/library/collections/{a}/sources/{sid}/refresh').status_code == 200
        _run()
        s = client.get(f'/api/library/collections/{a}/sources').json()['sources'][0]
        assert s['status'] == 'ready' and s['stats'].get('recaptured_unchanged') is True and site['hits'] == n + 1
        site['body'] = PAGE.replace('thirty days', 'sixty days')                                    # the page changes: it is read again
        client.post(f'/api/library/collections/{a}/sources/{sid}/refresh'); _run()
        assert 'sixty days' in client.get(f'/api/library/collections/{a}/search', params={'q': 'refund'}).json()['passages'][0]['text']
        # the same content under another address is not stored twice
        dup = client.post(f'/api/library/collections/{a}/sources/url', json={'url': site['base'] + '/copy'}).json()['id']
        _run()
        d = [x for x in client.get(f'/api/library/collections/{a}/sources').json()['sources'] if x['id'] == dup][0]
        assert d['status'] == 'failed' and 'same content' in d['error']
        bad = client.post(f'/api/library/collections/{a}/sources/url', json={'url': site['base'] + '/missing'}).json()['id']
        _run()
        b = [x for x in client.get(f'/api/library/collections/{a}/sources').json()['sources'] if x['id'] == bad][0]
        assert b['status'] == 'failed' and 'answered 404' in b['error'] and b['job']['attempts'] == 1       # not retried
    finally:
        settings.reset('FETCH_ALLOWED_HOSTS', 't')


def test_only_writers_add_addresses_and_strangers_learn_nothing(client, world, site):
    a, b = world['a']['id'], world['b']['id']
    settings.put('FETCH_ALLOWED_HOSTS', '127.0.0.1', 't')
    try:
        _as(client, 'bob')
        assert client.post(f'/api/library/collections/{a}/sources/url', json={'url': site['base'] + '/x'}).status_code == 403
        _as(client, 'eve')
        assert client.post(f'/api/library/collections/{a}/sources/url', json={'url': site['base'] + '/x'}).status_code == 404
        assert client.post(f'/api/library/collections/{a}/sources/00000000-0000-0000-0000-000000000000/refresh').status_code == 404
        assert site['hits'] == 0
    finally:
        settings.reset('FETCH_ALLOWED_HOSTS', 't')


# ------------------------------------------------------------------ the browser sidecar's front door
def test_the_sidecar_wants_its_token_and_refuses_internal_addresses(monkeypatch):
    from fastapi.testclient import TestClient
    from studio import fetcher
    monkeypatch.setattr(fetcher, 'TOKEN', 'secret-token')
    c = TestClient(fetcher.app)
    assert c.get('/health').json() == {'ok': True}
    assert c.post('/render', json={'url': 'https://example.com/'}).status_code == 401
    assert c.post('/render', json={'url': 'https://example.com/'}, headers={'X-Fetch-Token': 'wrong'}).status_code == 401
    r = c.post('/render', json={'url': 'http://169.254.169.254/latest/'}, headers={'X-Fetch-Token': 'secret-token'})
    assert r.status_code == 422 and 'internal' in r.json()['detail']
    monkeypatch.setattr(fetcher, 'render', lambda url: {'final_url': url, 'title': 'T', 'html': '<p>x</p>'})
    monkeypatch.setattr(netguard, 'resolve', lambda *a, **k: ('https', 'example.com', 443, None))
    assert c.post('/render', json={'url': 'https://example.com/'}, headers={'X-Fetch-Token': 'secret-token'}).json()['title'] == 'T'


def test_adding_documents_is_limited_per_user_and_only_after_the_access_check(client, world, site):
    from studio import ratelimit
    a = world['a']['id']
    settings.put('INGEST_PER_MIN', 3, 't')
    ratelimit.reconfigure(); ratelimit.INGEST.buckets.clear()
    settings.put('FETCH_ALLOWED_HOSTS', '127.0.0.1', 't')
    try:
        _as(client, 'ann')
        codes = [client.post(f'/api/library/collections/{a}/sources/url', json={'url': site['base'] + f'/p{i}'}).status_code for i in range(5)]
        assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]
        r = client.post(f'/api/library/collections/{a}/sources/url', json={'url': site['base'] + '/more'})
        assert r.status_code == 429 and int(r.headers['retry-after']) >= 1 and '3 heavy actions a minute' in r.json()['detail']
        assert client.post(f'/api/library/collections/{a}/sources', files={'file': ('x.txt', b'abc', 'text/plain')}).status_code == 429          # uploads share the budget
        _as(client, 'bob')                                                                       # another user has a budget of their own, and a stranger's attempts do not spend anyone's
        ratelimit.INGEST.buckets.clear()
        assert client.post(f'/api/library/collections/{a}/sources/url', json={'url': site['base'] + '/q'}).status_code == 403
        _as(client, 'eve')
        assert client.post(f'/api/library/collections/{a}/sources/url', json={'url': site['base'] + '/q'}).status_code == 404
        assert ratelimit.INGEST.buckets.get('eve') is None and ratelimit.INGEST.buckets.get('bob') is None
    finally:
        settings.reset('INGEST_PER_MIN', 't'); settings.reset('FETCH_ALLOWED_HOSTS', 't'); ratelimit.reconfigure(); ratelimit.INGEST.buckets.clear()
