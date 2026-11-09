"""Who is the client, and did they come over HTTPS? Works with no proxy, one proxy, or a chain (Cloudflare -> Traefik, nginx, a load balancer).

Headers that name the client (X-Forwarded-For, CF-Connecting-IP, X-Real-IP) are only believed when the request arrives FROM a trusted proxy:
anyone else could write them. Configure per deployment:
  TRUSTED_PROXIES   addresses/networks of the proxies in front of the app (default: loopback and private networks, where a container proxy lives)
  CLIENT_IP_HEADER  X-Forwarded-For (default: any standard proxy; the client is the rightmost address that is not a trusted proxy)
                    CF-Connecting-IP (Cloudflare), X-Real-IP (nginx), or empty (no proxy: the socket address)
"""
import ipaddress

from . import config


def _nets():
    out = []
    for part in (config.TRUSTED_PROXIES or '').split(','):
        part = part.strip()
        if part:
            try:
                out.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                pass
    return out


_NETS = _nets()


def trusted(ip):
    try:
        a = ipaddress.ip_address(ip)
    except (ValueError, TypeError):
        return False
    return any(a in n for n in _NETS)


def _valid(ip):
    try:
        ipaddress.ip_address(ip)
        return True
    except (ValueError, TypeError):
        return False


def peer(request):
    c = request.scope.get('client')
    return c[0] if c else None


def client_ip(request):
    p = peer(request)
    header = (config.CLIENT_IP_HEADER or '').strip()
    if not header or not trusted(p):
        return p
    value = request.headers.get(header.lower())
    if not value:
        return p
    if header.lower() == 'x-forwarded-for':
        hops = [h.strip() for h in value.split(',') if h.strip()] + [p]
        for h in reversed(hops):                       # walk back from the app: the first hop that is not one of our proxies is the client
            if not trusted(h):
                return h if _valid(h) else p
        return hops[0] if _valid(hops[0]) else p
    first = value.split(',')[0].strip()
    return first if _valid(first) else p


def is_https(request):
    if trusted(peer(request)):
        proto = request.headers.get('x-forwarded-proto')
        if proto:
            return proto.split(',')[0].strip().lower() == 'https'
    return request.url.scheme == 'https'
