"""Users, browser sessions (cookie) and API tokens (for MCP clients). Passwords use scrypt."""
import hashlib, hmac, os, secrets
from datetime import datetime, timedelta, timezone
from fastapi import Cookie, Depends, HTTPException, Request
from . import access, config, settings
from .db import conn, J


def hash_password(pw, salt=None):
    salt = salt or os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return salt.hex() + '$' + h.hex()


def check_password(pw, stored):
    salt, h = stored.split('$')
    return hmac.compare_digest(hash_password(pw, bytes.fromhex(salt)).split('$')[1], h)


def _h(token):
    return hashlib.sha256((config.SECRET_KEY + token).encode()).hexdigest()


def ensure_admin():
    with conn() as c:
        if config.ADMIN_PASSWORD and not c.execute('SELECT 1 FROM app_user').fetchone():
            c.execute("INSERT INTO app_user (username,password_hash,role) VALUES (%s,%s,'admin')", (config.ADMIN_USER, hash_password(config.ADMIN_PASSWORD)))


MIN_PASSWORD = 12
LOCK_FAILS, IP_FAILS, LOCK_MINUTES = 5, 30, 15
ROLES = ('viewer', 'modeller', 'publisher', 'admin')                   # each role can do everything the ones before it can
RANK = {r: i for i, r in enumerate(ROLES)}
EXTRA_ROLES = ('editor',)                                              # roles outside that ladder: editor = may ingest, edit and approve documents in collections granted at write level
_DUMMY = hash_password('not-a-real-password')                          # an unknown username costs the same time as a wrong password


class LoginBlocked(Exception):
    pass


def password_problem(pw, username=None):
    if len(pw or '') < MIN_PASSWORD:
        return f'the password needs at least {MIN_PASSWORD} characters'
    if username and pw.lower() == username.lower():
        return 'the password cannot be the username'
    return None


def login(username, password, ip=None):
    """(token, user) or None. Raises LoginBlocked after too many failures: then even the right password is refused until the window passes."""
    with conn() as c:
        q = f"SELECT count(*) AS n FROM login_attempt WHERE NOT ok AND at > now() - interval '{LOCK_MINUTES} minutes' AND "
        if c.execute(q + 'username=%s', (username,)).fetchone()['n'] >= LOCK_FAILS or (ip and c.execute(q + 'ip=%s', (ip,)).fetchone()['n'] >= IP_FAILS):
            c.execute('INSERT INTO login_attempt (username,ip,ok) VALUES (%s,%s,false)', (username, ip))
            raise LoginBlocked(f'too many failed attempts: try again in {LOCK_MINUTES} minutes')
        u = c.execute('SELECT * FROM app_user WHERE username=%s', (username,)).fetchone()
        ok = check_password(password, u['password_hash'] if u else _DUMMY) and u is not None and not u['disabled']      # a disabled account fails like a wrong password
        c.execute('INSERT INTO login_attempt (username,ip,ok) VALUES (%s,%s,%s)', (username, ip, ok))
        c.execute("DELETE FROM login_attempt WHERE at < now() - interval '1 day'")
        if not ok:
            return None
        if ok and u['must_change'] is False and password_problem(password, username):      # an old password below today's rule: change it now
            c.execute('UPDATE app_user SET must_change=true WHERE username=%s', (username,))
            u['must_change'] = True
        token = secrets.token_urlsafe(32)
        c.execute('INSERT INTO session (token_hash,username,expires_at) VALUES (%s,%s,%s)',
                  (_h(token), username, datetime.now(timezone.utc) + timedelta(hours=settings.get('SESSION_HOURS'))))
        c.execute('DELETE FROM session WHERE expires_at < now()')
        return token, {'username': u['username'], 'role': u['role'], 'roles': roles_of(u), 'entitlements': access.normalize_user(u['entitlements']), 'must_change': u['must_change']}


def change_password(username, current, new, keep_token=None):
    """The user's own change: the current password is required, the new one must pass the rule, every other session of the user ends."""
    with conn() as c:
        u = c.execute('SELECT * FROM app_user WHERE username=%s', (username,)).fetchone()
        if not u or not check_password(current, u['password_hash']):
            raise ValueError('the current password is wrong')
        problem = password_problem(new, username) or ('the new password must differ from the current one' if new == current else None)
        if problem:
            raise ValueError(problem)
        c.execute('UPDATE app_user SET password_hash=%s, must_change=false, password_changed_at=now() WHERE username=%s', (hash_password(new), username))
        c.execute('DELETE FROM session WHERE username=%s AND token_hash <> %s', (username, _h(keep_token) if keep_token else ''))


class UserError(ValueError):
    """An administration of users that is not allowed; the message says why and what to do instead."""


def generate_password():
    """A strong temporary password: 20 characters from an alphabet without look-alikes, always passing the password rule."""
    alphabet = 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    while True:
        pw = ''.join(secrets.choice(alphabet) for _ in range(20))
        if any(ch.islower() for ch in pw) and any(ch.isupper() for ch in pw) and any(ch.isdigit() for ch in pw):
            return pw


def enabled_admins(c, excluding=None):
    """How many administrators can sign in (not disabled), optionally not counting one of them."""
    return c.execute("SELECT count(*) AS n FROM app_user WHERE role='admin' AND NOT disabled AND (%s::text IS NULL OR username <> %s)", (excluding, excluding)).fetchone()['n']


def _user_row(c, username):
    u = c.execute('SELECT username, role, disabled FROM app_user WHERE username=%s', (username,)).fetchone()
    if not u:
        raise UserError('no such user')
    return u


def reset_password(username, password=None, actor=None):
    """An administrator sets a new temporary password (the given one, or a generated one) and returns it: the only time it is ever shown.
    The user must choose their own at the next sign-in, every session of theirs ends, and a sign-in lockout on their name is lifted."""
    if actor and actor == username:
        raise UserError('use Change password for your own account')
    password = password or generate_password()
    problem = password_problem(password, username)
    if problem:
        raise UserError(problem)
    with conn() as c:
        _user_row(c, username)
        c.execute('UPDATE app_user SET password_hash=%s, must_change=true, password_changed_at=now() WHERE username=%s', (hash_password(password), username))
        c.execute('DELETE FROM session WHERE username=%s', (username,))
        c.execute('DELETE FROM login_attempt WHERE username=%s', (username,))
    return password


def set_disabled(username, disabled, actor=None):
    """Disable (or enable again) an account. A disabled user cannot sign in and their sessions end; grants, roles and history stay."""
    with conn() as c:
        u = _user_row(c, username)
        if disabled:
            if actor and actor == username:
                raise UserError('you cannot disable your own account')
            if u['role'] == 'admin' and not u['disabled'] and enabled_admins(c, excluding=username) < 1:
                raise UserError('this is the only administrator who can sign in: make someone else an administrator first')
        c.execute('UPDATE app_user SET disabled=%s WHERE username=%s', (bool(disabled), username))
        if disabled:
            c.execute('DELETE FROM session WHERE username=%s', (username,))
    return {'disabled': bool(disabled)}


def delete_user(username, actor=None):
    """Remove a disabled account for good: their sessions and their collection grants go with it. Refused while they are the only manager of a collection.
    The audit log keeps their name."""
    with conn() as c:
        u = _user_row(c, username)
        if actor and actor == username:
            raise UserError('you cannot delete your own account')
        if not u['disabled']:
            raise UserError('disable the account first: a user is deleted only after it has been disabled')
        if u['role'] == 'admin' and enabled_admins(c) < 1:
            raise UserError('there would be no administrator left who can sign in')
        sole = c.execute("SELECT col.name FROM library.grant_ g JOIN library.collection col ON col.id=g.collection_id WHERE g.username=%s AND g.level='manage' "
                         "AND (SELECT count(*) FROM library.grant_ m WHERE m.collection_id=g.collection_id AND m.level='manage') < 2 ORDER BY col.name", (username,)).fetchall()
        if sole:
            raise UserError('this user is the only manager of: ' + ', '.join(r['name'] for r in sole) + '. Appoint another manager first')
        c.execute('DELETE FROM app_user WHERE username=%s', (username,))             # sessions and grants cascade
    return {'deleted': username}


def logout(token):
    with conn() as c:
        c.execute('DELETE FROM session WHERE token_hash=%s', (_h(token),))


def user_from_token(token):
    if not token:
        return None
    with conn() as c:
        r = c.execute('SELECT u.username,u.role,u.extra_roles,u.entitlements,u.must_change FROM session s JOIN app_user u USING (username) WHERE s.token_hash=%s AND s.expires_at>now() AND NOT u.disabled', (_h(token),)).fetchone()
    return {**r, 'roles': roles_of(r)} if r else None


def roles_of(u):
    """All roles a user holds: the rung of the ladder and every rung below it, plus the extra roles. An admin holds every role."""
    rung = RANK.get(u.get('role'), 0)
    roles = set(ROLES[:rung + 1]) | {x for x in (u.get('extra_roles') or []) if x in EXTRA_ROLES}
    if u.get('role') == 'admin':
        roles |= set(EXTRA_ROLES)
    return sorted(roles, key=lambda r: (ROLES + EXTRA_ROLES).index(r))


def has_role(role):
    """Dependency: the user holds this role (any of the ladder, or an extra one such as editor)."""
    if role in RANK:
        return at_least(role)

    def dep(u=Depends(current_user)):
        if role not in u['roles']:
            raise HTTPException(403, f'this needs the {role} role (you hold: {", ".join(u["roles"])})')
        return u
    return dep


PASSWORD_PAGES = ('/api/me', '/api/me/password', '/api/logout')


def current_user(request: Request, studio_session: str | None = Cookie(default=None)):
    u = user_from_token(studio_session)
    if not u:
        raise HTTPException(401, 'not logged in')
    if u.get('must_change') and request.url.path not in PASSWORD_PAGES:          # nothing else works until the password is changed
        raise HTTPException(403, 'password change required')
    return u


def at_least(role):
    """Dependency: the user's role must be this one or above (viewer < modeller < publisher < admin)."""
    def dep(u=Depends(current_user)):
        if RANK.get(u['role'], 0) < RANK[role]:
            raise HTTPException(403, f'this needs the {role} role (you are {u["role"]})')
        return u
    return dep


modeller_user = at_least('modeller')
publisher_user = at_least('publisher')
admin_user = at_least('admin')
editor_user = has_role('editor')


def create_api_token(name, entitlements=None):
    token = 'ss_' + secrets.token_urlsafe(32)
    with conn() as c:
        c.execute('INSERT INTO api_token (name,token_hash,entitlements) VALUES (%s,%s,%s)', (name, _h(token), J(access.normalize(entitlements))))
    return token


def api_token(token, touch=True):
    """Token -> {'id', 'name', 'entitlements'} or None."""
    if not token:
        return None
    with conn() as c:
        q = 'UPDATE api_token SET last_used=now() WHERE token_hash=%s RETURNING id,name,entitlements' if touch else 'SELECT id,name,entitlements FROM api_token WHERE token_hash=%s'
        r = c.execute(q, (_h(token),)).fetchone()
    return {'id': r['id'], 'name': r['name'], 'entitlements': access.normalize(r['entitlements'])} if r else None


def api_token_ok(token):
    return api_token(token) is not None
