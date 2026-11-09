"""Rate limits inside the app, so they hold whatever is (or is not) in front of it: token buckets per client address (see clientinfo) and per user.

One process keeps the buckets in memory (the app runs as one process); several app instances would need a shared store (Postgres or Redis).
A proxy's own limits (Traefik labels, nginx limit_req, Cloudflare) are a second layer, not a replacement.
"""
import threading
import time

from . import settings


class Bucket:
    __slots__ = ('tokens', 'at')

    def __init__(self, burst, at):
        self.tokens, self.at = float(burst), at


class Limiter:
    def __init__(self, rate_per_s=None, burst=None, limits=None):
        self.limits = limits or (lambda: (rate_per_s, burst))      # () -> (rate per second, burst): read again when the settings change
        self.rate, self.burst = self.limits()
        self.buckets = {}
        self.lock = threading.Lock()
        self.swept = time.monotonic()

    def allow(self, key):
        """(allowed, seconds to wait)"""
        now = time.monotonic()
        with self.lock:
            b = self.buckets.get(key)
            if b is None:
                b = self.buckets[key] = Bucket(self.burst, now)
            b.tokens = min(self.burst, b.tokens + (now - b.at) * self.rate)
            b.at = now
            if now - self.swept > 300:                       # forget clients idle for 10 minutes
                self.buckets = {k: v for k, v in self.buckets.items() if now - v.at < 600}
                self.swept = now
            if b.tokens >= 1:
                b.tokens -= 1
                return True, 0
            return False, max(1, int((1 - b.tokens) / self.rate) + 1)


    def reconfigure(self):
        with self.lock:
            self.rate, self.burst = self.limits()
            for b in self.buckets.values():
                b.tokens = min(b.tokens, float(self.burst))


API = Limiter(limits=lambda: (settings.get('RATE_PER_S'), settings.get('RATE_BURST')))                      # every API/MCP request, per client address
LOGIN = Limiter(limits=lambda: (settings.get('LOGIN_PER_MIN') / 60, settings.get('LOGIN_PER_MIN')))          # sign-in attempts, per client address
ALFRED = Limiter(limits=lambda: (settings.get('ALFRED_PER_MIN') / 60, settings.get('ALFRED_PER_MIN')))       # questions to Alfred (they cost money), per user

INGEST = Limiter(limits=lambda: (settings.get('INGEST_PER_MIN') / 60, settings.get('INGEST_PER_MIN')))      # files and web addresses added to the Library, per user (each starts background work)


def reconfigure():
    for lim in (API, LOGIN, ALFRED, INGEST):
        lim.reconfigure()


def check(path, method, ip):
    """(allowed, retry_after, what) for one request."""
    if path == '/api/health' or not (path.startswith('/api/') or path.rstrip('/') == '/mcp'):
        return True, 0, None                                            # static files and the health check are free
    ok, wait = API.allow(ip)
    if not ok:
        return False, wait, 'too many requests'
    if path == '/api/login' and method == 'POST':
        ok, wait = LOGIN.allow(ip)
        if not ok:
            return False, wait, 'too many sign-in attempts'
    return True, 0, None
