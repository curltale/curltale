from types import SimpleNamespace

import pytest

from studio import clientinfo, config, ratelimit


def req(peer, headers=None, scheme='http'):
    return SimpleNamespace(scope={'client': (peer, 1234)}, headers={k.lower(): v for k, v in (headers or {}).items()}, url=SimpleNamespace(scheme=scheme))


@pytest.fixture
def header(monkeypatch):
    def use(h):
        monkeypatch.setattr(config, 'CLIENT_IP_HEADER', h)
    return use


def test_no_proxy_uses_the_socket_address_and_ignores_headers(header):
    header('')
    assert clientinfo.client_ip(req('203.0.113.5', {'X-Forwarded-For': '1.2.3.4'})) == '203.0.113.5'


def test_forwarded_for_is_believed_only_from_a_trusted_proxy(header):
    header('X-Forwarded-For')
    assert clientinfo.client_ip(req('172.19.0.14', {'X-Forwarded-For': '198.51.100.7'})) == '198.51.100.7'     # Traefik on the docker network
    assert clientinfo.client_ip(req('203.0.113.5', {'X-Forwarded-For': '1.2.3.4'})) == '203.0.113.5'          # a direct caller cannot pose as someone else


def test_forwarded_for_takes_the_rightmost_address_that_is_not_a_proxy(header):
    header('X-Forwarded-For')
    # the client wrote a fake first hop; the proxies appended the real one
    assert clientinfo.client_ip(req('10.0.0.2', {'X-Forwarded-For': '6.6.6.6, 198.51.100.7, 10.0.0.9'})) == '198.51.100.7'


def test_cloudflare_header_only_through_the_proxy(header):
    header('CF-Connecting-IP')
    assert clientinfo.client_ip(req('172.19.0.14', {'CF-Connecting-IP': '198.51.100.7'})) == '198.51.100.7'
    assert clientinfo.client_ip(req('203.0.113.5', {'CF-Connecting-IP': '6.6.6.6'})) == '203.0.113.5'
    assert clientinfo.client_ip(req('172.19.0.14', {'CF-Connecting-IP': 'not-an-ip'})) == '172.19.0.14'


def test_https_is_known_from_a_trusted_proxy_only():
    assert clientinfo.is_https(req('172.19.0.14', {'X-Forwarded-Proto': 'https'})) is True
    assert clientinfo.is_https(req('203.0.113.5', {'X-Forwarded-Proto': 'https'})) is False


def test_token_bucket_allows_a_burst_then_refills():
    lim = ratelimit.Limiter(rate_per_s=1000, burst=3)
    assert [lim.allow('a')[0] for _ in range(4)] == [True, True, True, False]
    assert lim.allow('b')[0] is True                                   # buckets are per client
    lim.buckets['a'].at -= 1                                            # a second later
    assert lim.allow('a')[0] is True


def test_static_files_and_health_are_not_limited(monkeypatch):
    monkeypatch.setattr(ratelimit, 'API', ratelimit.Limiter(0.0001, 1))
    assert ratelimit.check('/api/status', 'GET', '9.9.9.9')[0] is True
    assert ratelimit.check('/api/status', 'GET', '9.9.9.9')[0] is False
    assert ratelimit.check('/assets/index.js', 'GET', '9.9.9.9')[0] is True and ratelimit.check('/api/health', 'GET', '9.9.9.9')[0] is True
