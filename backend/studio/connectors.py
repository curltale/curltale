"""Connectors: where the data lives. Stored in the app database (password encrypted), tested and introspected with short-lived read-only connections.

What differs between database kinds (connecting, running, reading the structure, dialect) lives in drivers.py; this module is the storage and the safety rules.
"""
import base64
import hashlib
import ipaddress
import socket
import time
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken
from . import config, drivers
from .db import J, conn
from .drivers import DEFAULT_OPTIONS, DriverError



class ConnectorError(ValueError):
    pass


# ---------------------------------------------------------------- secrets
def _fernet():
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(('connector:' + config.SECRET_KEY).encode()).digest()))


def encrypt(s):
    return _fernet().encrypt(s.encode()).decode()


def decrypt(token):
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise ConnectorError('the stored password cannot be decrypted (SECRET_KEY changed?): enter it again') from e


# ---------------------------------------------------------------- storage
def public(r):
    """A connector as the UI may see it: everything except the secret."""
    return {'id': r['id'], 'name': r['name'], 'kind': r['kind'], 'params': r['params'], 'options': {**DEFAULT_OPTIONS, **(r['options'] or {})}, 'active': r['active'],
            'has_password': bool(r['secret']), 'status': r['status'], 'catalog_at': r['catalog_at'], 'tables_known': len((r['catalog'] or {}).get('tables', [])),
            'created_at': r['created_at'], 'label': _driver_cls(r['kind']).label, 'dialect': _driver_cls(r['kind']).dialect}


def _driver_cls(kind):
    cls = drivers.KINDS.get(kind)
    if cls is None:
        raise ConnectorError(f'unknown connector kind "{kind}" (available: {", ".join(drivers.KINDS)})')
    return cls


def kinds():
    """What the UI offers: each kind with the form fields it needs."""
    return [{'kind': k, 'label': c.label, 'default_port': c.default_port, 'fields': list(c.fields), 'file_based': c.file_based, 'dialect': c.dialect} for k, c in drivers.KINDS.items()]


def _clean_params(kind, p):
    try:
        return _driver_cls(kind).clean_params(p)
    except ValueError as e:
        raise ConnectorError(str(e)) from e


def _clean_options(o):
    o = {**DEFAULT_OPTIONS, **(o or {})}
    o['statement_timeout_ms'] = max(1000, min(int(o['statement_timeout_ms']), 120000))
    o['row_limit'] = max(1, min(int(o['row_limit']), 5000))
    o['agents_may_read_rows'] = bool(o['agents_may_read_rows'])
    return o


def list_all():
    with conn() as c:
        return [public(r) for r in c.execute('SELECT * FROM connector ORDER BY id').fetchall()]


def get(cid):
    with conn() as c:
        r = c.execute('SELECT * FROM connector WHERE id=%s', (cid,)).fetchone()
    if not r:
        raise ConnectorError('no such connector')
    return r


def active():
    with conn() as c:
        return c.execute('SELECT * FROM connector WHERE active').fetchone()


def create(name, params, password, options=None, kind='postgres'):
    if not name or not name.strip():
        raise ConnectorError('a name is required')
    p = _clean_params(kind, params)
    if not _driver_cls(kind).file_based:
        guard(p)
    with conn() as c:
        if c.execute('SELECT 1 FROM connector WHERE name=%s', (name.strip(),)).fetchone():
            raise ConnectorError(f'a connector named "{name}" already exists')
        r = c.execute('INSERT INTO connector (name,kind,params,secret,options) VALUES (%s,%s,%s,%s,%s) RETURNING *',
                      (name.strip(), kind, J(p), encrypt(password) if password else None, J(_clean_options(options)))).fetchone()
    return public(r)


def update(cid, name=None, params=None, password=None, options=None):
    old = get(cid)
    p = _clean_params(old['kind'], {**old['params'], **(params or {})})
    if not _driver_cls(old['kind']).file_based:
        guard(p)
    with conn() as c:
        r = c.execute('UPDATE connector SET name=%s, params=%s, secret=%s, options=%s, status=NULL WHERE id=%s RETURNING *',
                      ((name or old['name']).strip(), J(p), encrypt(password) if password else old['secret'], J(_clean_options({**(old['options'] or {}), **(options or {})})), cid)).fetchone()
    return public(r)


def delete(cid):
    with conn() as c:
        r = c.execute('DELETE FROM connector WHERE id=%s RETURNING id', (cid,)).fetchone()
    if not r:
        raise ConnectorError('no such connector')


# ---------------------------------------------------------------- safety
def guard(p):
    """A connector must not reach this app's own database or a cloud metadata address: an admin of the UI is not an admin of the server."""
    own = urlparse(config.DATABASE_URL)
    if (p['host'], p['port'], p.get('database')) == (own.hostname, own.port or 5432, (own.path or '/').lstrip('/')):
        raise ConnectorError("this is the app's own database; connect to the database you want to describe")
    try:
        for fam, _t, _p, _c, sa in socket.getaddrinfo(p['host'], p['port'], proto=socket.IPPROTO_TCP):
            ip = ipaddress.ip_address(sa[0])
            if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
                raise ConnectorError(f'{p["host"]} resolves to {ip}, which is not a database address')
    except socket.gaierror as e:
        raise ConnectorError(f'cannot resolve host {p["host"]}') from e


def driver(row):
    """The driver of a connector row (rechecks the host: DNS can change)."""
    if not drivers.KINDS[row['kind']].file_based:
        guard(row['params'])
    return drivers.driver_for(row)


# ---------------------------------------------------------------- test and structure
def test(cid):
    row = get(cid)
    out = {'ok': False, 'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    try:
        out.update(driver(row).test(), ok=True)
    except (DriverError, ConnectorError) as e:
        out['error'] = str(e)
    with conn() as c:
        c.execute('UPDATE connector SET status=%s WHERE id=%s', (J(out), cid))
    return out


def introspect(cid, schemas=None):
    """Read tables, columns, keys and small value sets. Stores the result on the connector and returns it."""
    row = get(cid)
    try:
        cat = driver(row).introspect(schemas)
    except DriverError as e:
        raise ConnectorError(str(e)) from e
    with conn() as c:
        c.execute('UPDATE connector SET catalog=%s, catalog_at=now() WHERE id=%s', (J(cat), cid))
    return cat


# ---------------------------------------------------------------- does the database hold what the model describes?
_cov = {}


def coverage(row, vid=None):
    """How many of the published model's tables exist in the connector's last read structure: {total, found, missing: [...], percent} or None (structure not read yet)."""
    from .ontology import assemble, published_id
    cat = row.get('catalog')
    if not cat:
        return None
    vid = vid or published_id()
    key = (row['id'], str(row.get('catalog_at')), vid)
    if key not in _cov:
        _cov.clear()
        have = {f"{t['schema']}.{t['name']}".lower() for t in cat['tables']}
        want = [e['semantic_table'] for e in assemble(vid)['entities'].values()]
        missing = [t for t in want if t.lower() not in have]
        _cov[key] = {'total': len(want), 'found': len(want) - len(missing), 'missing': missing[:12],
                     'percent': round(100 * (len(want) - len(missing)) / len(want)) if want else 100}
    return _cov[key]


def mismatch_hint(row):
    """One sentence for a failing query, when the database clearly is not the one the model describes (else None)."""
    try:
        cv = coverage(row)
    except Exception:
        return None
    if not cv or cv['percent'] >= 50:
        return None
    return (f"This database holds {cv['found']} of the model's {cv['total']} tables (missing e.g. {', '.join(cv['missing'][:3])}): it is not the database the published model describes. "
            "Generate a model from it (Connectors page) or switch to a database that matches the model.")
