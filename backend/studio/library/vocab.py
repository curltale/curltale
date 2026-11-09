"""The vocabulary of a collection: the kinds of topic and the kinds of relation its wiki uses. Every collection starts from the same neutral set and
then goes its own way; what one collection adds never reaches another."""
from ..db import conn

from .starter_ontology import TOPIC_TYPES as _BK_TOPIC_TYPES, RELATION_TYPES as _BK_RELATION_TYPES

# The starter ontology (types with descriptions and page templates, relations with inverses), plus the generic 'Topic' that the
# analyzer falls back to when a proposed type is rejected.
TOPIC_TYPES = [('Topic', 'A subject that does not fit a more specific type. Fallback only: prefer a specific type whenever one fits.', '')] + list(_BK_TOPIC_TYPES)   # (name, description, template)
RELATION_TYPES = [(n, d, inv) for n, inv, d in _BK_RELATION_TYPES]                                                                                      # (name, description, inverse)
PROTECTED = ('Topic', 'related_to')                # the fallbacks the analyzer uses when nothing fits: they always exist


def seed(cid):
    """Give a collection the starter vocabulary (does nothing for kinds it already has)."""
    with conn() as c:
        for name, desc, template in TOPIC_TYPES:
            c.execute('INSERT INTO library.topic_type (collection_id,name,description,template) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING', (cid, name, desc, template))
        for name, desc, inv in RELATION_TYPES:
            c.execute('INSERT INTO library.relation_type (collection_id,name,description,inverse_name) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING', (cid, name, desc, inv))


def seed_all():
    with conn() as c:
        ids = [r['id'] for r in c.execute('SELECT id FROM library.collection c WHERE NOT EXISTS (SELECT 1 FROM library.topic_type t WHERE t.collection_id=c.id)').fetchall()]
    for i in ids:
        seed(i)


def get(cid, include_rejected=False):
    with conn() as c:
        tt = c.execute("SELECT name, description, template, status FROM library.topic_type WHERE collection_id=%s AND (%s OR status<>'rejected') ORDER BY (name='Topic') DESC, name", (cid, include_rejected)).fetchall()
        rt = c.execute("SELECT name, description, inverse_name, status FROM library.relation_type WHERE collection_id=%s AND (%s OR status<>'rejected') ORDER BY (name='related_to') DESC, name", (cid, include_rejected)).fetchall()
    return {'topic_types': tt, 'relation_types': rt}
