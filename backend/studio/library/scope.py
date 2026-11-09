"""Who may touch which collection. Every Library function takes a Scope (who is asking) and opens a collection through `open_collection`;
there is no function that reads document data without one. A collection the caller has no grant on does not exist for them (404), and a
grant that is too low is a 403. Administrators see that collections exist (to grant themselves access) but not what is in them."""
from dataclasses import dataclass, field
from fastapi import HTTPException
from .. import auth
from ..db import conn

LEVELS = ('read', 'write', 'manage')
RANK = {l: i + 1 for i, l in enumerate(LEVELS)}


@dataclass(frozen=True)
class Scope:
    kind: str                                  # 'user' or 'token'
    name: str                                  # username or token name
    roles: tuple = ()
    grants: dict = field(default_factory=dict)  # collection id (str) -> level

    @property
    def admin(self):
        return self.kind == 'user' and 'admin' in self.roles

    def level(self, cid):
        """The level this caller really has. Writing needs the editor role as well as the grant; tokens only read."""
        lv = self.grants.get(str(cid))
        if lv and RANK[lv] > 1 and (self.kind == 'token' or 'editor' not in self.roles):
            return 'read'
        return lv


def for_user(u):
    with conn() as c:
        rows = c.execute('SELECT collection_id, level FROM library.grant_ WHERE username=%s', (u['username'],)).fetchall()
    return Scope('user', u['username'], tuple(u.get('roles') or auth.roles_of(u)), {str(r['collection_id']): r['level'] for r in rows})


def for_token(token_id, name):
    with conn() as c:
        rows = c.execute("SELECT collection_id FROM library.grant_ WHERE token_id=%s", (token_id,)).fetchall()
    return Scope('token', name, (), {str(r['collection_id']): 'read' for r in rows})


@dataclass(frozen=True)
class Collection:
    """A collection the caller has been checked against. Content functions take this, not a bare id."""
    id: str
    slug: str
    name: str
    description: str
    usage: str
    level: str | None
    allow_ai: bool = True
    agents_may_read: bool = True


def open_collection(scope, ref, need='read', metadata_only=False):
    """The collection by id or slug, if the caller holds at least `need` on it. 404 when they hold nothing (it does not exist for them), 403 when too low.
    metadata_only lets an administrator look at the collection's name and settings without a grant (never its content)."""
    with conn() as c:
        row = c.execute('SELECT id,slug,name,description,usage,allow_ai,agents_may_read FROM library.collection WHERE id::text=%s OR slug=%s', (str(ref), str(ref))).fetchone()
    if not row:
        raise HTTPException(404, 'no such collection')
    cid = str(row['id'])
    lv = scope.level(cid)
    if lv is None:
        if scope.admin and metadata_only:
            return Collection(cid, row['slug'], row['name'], row['description'], row['usage'], None, row['allow_ai'], row['agents_may_read'])
        if scope.admin:
            raise HTTPException(403, 'administrators grant themselves access first (Collections page); that is logged')
        raise HTTPException(404, 'no such collection')
    if RANK[lv] < RANK[need]:
        raise HTTPException(403, f'this needs {need} access to the collection' + (' and the editor role' if lv == 'read' and need != 'read' and scope.kind == 'user' and 'editor' not in scope.roles else ''))
    return Collection(cid, row['slug'], row['name'], row['description'], row['usage'], lv, row['allow_ai'], row['agents_may_read'])


def trusted(cid):
    """A handle for background jobs. Jobs are only created after an access check, so the job itself holds no user: it works on the one collection it was made for."""
    with conn() as c:
        row = c.execute('SELECT id,slug,name,description,usage,allow_ai,agents_may_read FROM library.collection WHERE id=%s', (cid,)).fetchone()
    if not row:
        raise LookupError('the collection no longer exists')
    return Collection(str(row['id']), row['slug'], row['name'], row['description'], row['usage'], 'manage', row['allow_ai'], row['agents_may_read'])


def visible(scope):
    """Collections shown to the caller: those they hold a grant on, and for an administrator all of them (without access to content)."""
    with conn() as c:
        rows = c.execute('SELECT id,slug,name,description,usage,allow_ai,agents_may_read,created_at FROM library.collection ORDER BY lower(name)').fetchall()
    out = []
    for r in rows:
        lv = scope.level(str(r['id']))
        if lv or scope.admin:
            out.append({**r, 'id': str(r['id']), 'level': lv})
    return out


# One wording, used by every answer that carries a collection's passages (API, MCP, Alfred): the tag is advice to the agent, not an access rule.
USAGE_NOTE = {
    'public': 'This collection is public: its content may be shared with customers.',
    'internal': "This collection is internal: use it for your own understanding (processes, rules). Do not quote it or refer to it when talking with customers.",
}
