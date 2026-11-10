"""How much a request may send, decided before the app reads a byte of it.

A web framework reads the body of a request before it runs the code that checks who is asking, so without a limit anybody could make the server hold
gigabytes in memory (a JSON body) or on disk (a file upload). Here every request to /api and /mcp is capped, and anything bigger than a sign-in form
is accepted only from a caller who is already signed in (or, for /mcp, holds a valid token):

  * a body that declares its size is refused at once when it is too large, or too large for a stranger;
  * a body that does not declare its size (chunked) is counted as it arrives and cut off at the limit.

Limits: MAX_BODY_MB for ordinary requests, the upload limit of the Library for its upload endpoint, 20 MB for an Excel model.
"""
import json
import re

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from . import auth, config, settings

MB = 1024 * 1024
ANONYMOUS = 64 * 1024                                              # what a caller who is not signed in may send: enough for a sign-in form
UPLOAD = re.compile(r'^/api/library/collections/[^/]+/sources/?$')
EXCEL = '/api/admin/import/excel'


class TooLarge(HTTPException):
    """Raised while the body is being read. It is an HTTPException so that the framework answers 413 instead of 'error parsing the body'."""

    def __init__(self, cap):
        super().__init__(413, f'the request is larger than {_size(cap)}')


def _size(n):
    return f'{n // MB} MB' if n >= MB else f'{n // 1024} KB'


def limit_for(path):
    if UPLOAD.match(path):
        return settings.get('LIBRARY_MAX_UPLOAD_MB') * MB + MB    # the file plus the multipart envelope
    if path == EXCEL:
        return 21 * MB
    if path == '/api/login':
        return ANONYMOUS
    return config.MAX_BODY_MB * MB


def _known(scope):
    """Is this caller signed in (a browser session), or, on /mcp, the holder of a valid token?"""
    request = Request(scope)
    if scope['path'].rstrip('/') == '/mcp':
        return auth.api_token(request.headers.get('authorization', '')[7:], touch=False) is not None
    return auth.user_from_token(request.cookies.get('studio_session')) is not None


async def _reject(send, status, detail):
    body = json.dumps({'detail': detail}).encode()
    await send({'type': 'http.response.start', 'status': status,
                'headers': [(b'content-type', b'application/json'), (b'content-length', str(len(body)).encode()), (b'connection', b'close'), (b'cache-control', b'no-store')]})
    await send({'type': 'http.response.body', 'body': body})


class BodyLimit:
    """ASGI middleware (not a BaseHTTPMiddleware: it has to sit in front of the body, not behind it)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        if scope['type'] != 'http' or not (path.startswith('/api/') or path.rstrip('/') == '/mcp'):
            return await self.app(scope, receive, send)
        cap = limit_for(path)
        declared = dict(scope['headers']).get(b'content-length', b'').decode('latin-1')
        declared = int(declared) if declared.isdigit() else None
        if declared is not None and declared > cap:
            return await _reject(send, 413, f'the request is larger than {_size(cap)}')
        if (declared is None and scope['method'] not in ('GET', 'HEAD', 'OPTIONS')) or (declared or 0) > ANONYMOUS:
            if not await run_in_threadpool(_known, scope):          # more than a sign-in form: only from someone we know
                if declared is not None:
                    return await _reject(send, 401, 'sign in before sending this much data')
                cap = min(cap, ANONYMOUS)
        seen, started = 0, False

        async def counted():
            nonlocal seen
            message = await receive()
            if message['type'] == 'http.request':
                seen += len(message.get('body') or b'')
                if seen > cap:
                    raise TooLarge(cap)
            return message

        async def sending(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
            await send(message)

        try:
            await self.app(scope, counted, sending)
        except TooLarge as e:
            if started:
                raise
            await _reject(send, 413, e.detail)
