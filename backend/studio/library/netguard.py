"""Fetching web addresses safely. Only public http(s) addresses are allowed (anything that resolves to a private, loopback, link-local, reserved or
metadata address is refused) unless an administrator lists a host or network as allowed. The address is resolved ONCE and the connection is made to
that exact IP, with the original host name kept for the Host header and the TLS name, so a name that answers differently the second time (DNS
rebinding) cannot lead the request somewhere else. Every redirect is checked again. This file imports nothing from the app, so the browser
sidecar can use the same code."""
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx

MAX_REDIRECTS = 5
USER_AGENT = 'Mozilla/5.0 (compatible; StudioLibraryBot/1.0)'


class Refused(ValueError):
    """The address may not be fetched; the message is shown to the person who asked."""


def parse_allowed(text):
    """'intranet.example.com, 10.1.0.0/16' -> [('host', 'intranet.example.com'), ('net', IPv4Network)]"""
    out = []
    for item in (text or '').replace('\n', ',').split(','):
        item = item.strip().lower()
        if not item:
            continue
        try:
            out.append(('net', ipaddress.ip_network(item, strict=False)))
        except ValueError:
            out.append(('host', item))
    return out


def _is_allowed(host, ip, allowed):
    for kind, v in allowed:
        if kind == 'host' and host == v:
            return True
        if kind == 'net' and ip.version == v.version and ip in v:
            return True
    return False


def resolve(url, allowed=()):
    """-> (scheme, host, port, ip) for a URL that may be fetched. Raises Refused."""
    try:
        parts = urlsplit(url.strip())
        host, port = parts.hostname, parts.port
    except ValueError:
        raise Refused('that is not a valid web address')
    if parts.scheme not in ('http', 'https'):
        raise Refused('only http:// and https:// addresses can be fetched')
    if not host:
        raise Refused('the address has no host name')
    if parts.username or parts.password:
        raise Refused('addresses with a user name or password in them are not accepted')
    host = host.lower().rstrip('.')
    port = port or (443 if parts.scheme == 'https' else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise Refused(f'the host name {host} could not be found')
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if getattr(ip, 'ipv4_mapped', None):
            ip = ip.ipv4_mapped
        ips.append(ip)
    if not ips:
        raise Refused(f'the host name {host} could not be found')
    for ip in ips:                                                            # every address the name has must be acceptable, not just the one we pick
        if not ip.is_global and not _is_allowed(host, ip, allowed):
            raise Refused('that address points to a private or internal network, which is not allowed (an administrator can allow specific hosts in Settings, Library)')
    return parts.scheme, host, port, ips[0]


def _pinned(url, scheme, host, port, ip):
    """The same request, aimed at the validated IP; Host and TLS name stay the original host."""
    parts = urlsplit(url)
    netloc = f'[{ip}]' if ip.version == 6 else str(ip)
    default = 443 if scheme == 'https' else 80
    if port != default:
        netloc += f':{port}'
    target = parts._replace(netloc=netloc).geturl()
    hostport = host if port == default else f'{host}:{port}'
    return target, {'Host': hostport, 'User-Agent': USER_AGENT, 'Accept': 'text/html,application/xhtml+xml,application/pdf,*/*;q=0.5'}, {'sni_hostname': host}


def get(url, allowed=(), max_bytes=25 * 1024 * 1024, timeout=30, transport=None, headers=None):
    """GET with every hop checked. -> (final_url, status, response headers, body bytes). Raises Refused or httpx.HTTPError; a body over max_bytes raises Refused."""
    current = url.strip()
    with httpx.Client(transport=transport, timeout=timeout, follow_redirects=False, verify=True) as client:
        for _ in range(MAX_REDIRECTS + 1):
            scheme, host, port, ip = resolve(current, allowed)
            target, hdrs, ext = _pinned(current, scheme, host, port, ip)
            hdrs.update(headers or {})
            with client.stream('GET', target, headers=hdrs, extensions=ext) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get('location'):
                    current = urljoin(current, r.headers['location'])
                    continue
                body, size = [], 0
                if r.headers.get('content-length', '').isdigit() and int(r.headers['content-length']) > max_bytes:
                    raise Refused(f'the page is larger than {max_bytes // (1024 * 1024)} MB')
                for block in r.iter_bytes():
                    size += len(block)
                    if size > max_bytes:
                        raise Refused(f'the page is larger than {max_bytes // (1024 * 1024)} MB')
                    body.append(block)
                return current, r.status_code, r.headers, b''.join(body)
    raise Refused('too many redirects')
