"""Application-level enforcement of what a caller may read, for every query that goes to the data (agents, Alfred, the console).

The query is parsed (sqlglot), every column is traced to its real table through aliases, CTEs and subqueries, and the statement that finally runs is the one
generated from the checked tree, never the text the caller sent. Rules:
  * one read-only statement; only tables the model describes; only functions that compute from their arguments (the standard ones the parser knows,
    minus those that reach the server, plus a short allow list: a function that runs SQL of its own, like query_to_xml, would step around every check);
  * every column is named: a whole row (`SELECT t FROM table t`, `to_json(t)`, `t.*` inside a function) is refused, it would carry columns nobody checked;
  * restricted columns (agent_access none) are never readable without the restricted entitlement;
  * a confidential / personal column needs its entitlement, unless it is masked (agent_access masked): then the caller without the entitlement gets a stable hash;
  * a hashed column may be selected (outermost query only), counted, grouped, ordered (by its hash) and joined to another hashed column; anywhere else
    (filters, functions, MIN/MAX, subquery outputs, a join to a readable column or to values the caller wrote) it is refused, because comparing or
    transforming it would let a caller recover the value.
The database account is the second line (a read-only account; on the built-in demo database also column grants).
"""
import hashlib
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import ScopeType, traverse_scope

from . import config
from .drivers import PostgresDriver, mask_salt

_PG = []
_ROW = getattr(exp, 'TableColumn', None)                 # how the parser marks a table alias used as a value (a whole row)


def _default_driver():
    if not _PG:
        _PG.append(PostgresDriver({'params': {}, 'options': {}, 'secret': None}))
    return _PG[0]


class Refused(ValueError):
    """The query is not allowed. The message says why and what to do instead."""


def decide(a, ent):
    """'raw' | 'hash' | 'deny' for one column (a: the field as in the model)."""
    sens = a.get('sensitivity')
    if a.get('agent_access') == 'none' or sens == 'restricted':
        return 'raw' if ent.get('restricted') else 'deny'
    if a.get('agent_access') == 'masked':
        return 'raw' if sens in ('confidential', 'personal') and ent.get(sens) else 'hash'
    if sens in ('confidential', 'personal') and not ent.get(sens):
        return 'deny'
    return 'raw'


class Schema:
    """What the model says the database holds, for the parser: {schema: {table: {column: type}}} and the field behind each column."""

    def __init__(self, ents):
        self.tables, self.fields, self.mapping = {}, {}, {}
        for k, e in ents.items():
            s, t = e['semantic_table'].split('.', 1)
            self.mapping.setdefault(s, {})[t] = {c: 'text' for c in e['attributes']}
            self.fields[(s, t)] = e['attributes']


def _project_root(col):
    p = col.parent
    if isinstance(p, exp.Alias):
        p = p.parent
    return isinstance(p, exp.Select) and (col in p.expressions or col.parent in p.expressions)


def _in_count(col):
    p = col.parent
    if isinstance(p, exp.Distinct):
        p = p.parent
    return isinstance(p, exp.Count)


def _plain_use(col):
    """GROUP BY / PARTITION BY on the bare column."""
    p = col.parent
    if isinstance(p, exp.Group):
        return True
    return isinstance(p, exp.Window) and col in (p.args.get('partition_by') or [])


def _join_partner(col):
    """The other column of `a.x = b.y` in a join condition, or None when the column is not used that way."""
    p = col.parent
    if isinstance(p, exp.EQ) and isinstance(p.left, exp.Column) and isinstance(p.right, exp.Column):
        n = p.parent
        while isinstance(n, (exp.And, exp.Paren)):
            n = n.parent
        if isinstance(n, exp.Join):
            return p.right if p.left is col else p.left
    return None


def _is_hashed(col, scope, schema, ent):
    """Is this column, in this scope, a column of a model table that the caller only gets as a hash?"""
    src = scope.sources.get(col.table)
    if not isinstance(src, exp.Table):
        return False
    a = schema.fields.get((src.db, src.name), {}).get(col.name)
    return a is not None and decide(a, ent) == 'hash'


def _is_root(scope):
    """The outermost query, or a branch of a top-level UNION: what the caller receives."""
    s = scope
    while s.scope_type == ScopeType.SET_OPERATION and s.parent is not None:
        s = s.parent
    return s.scope_type == ScopeType.ROOT


def check(sql, schema, ent, limit=None, drv=None):
    """-> (sql to run, {'hashed': [columns]}) or raises Refused. drv: the database driver (dialect, forbidden functions, hash); default PostgreSQL."""
    drv = drv or _default_driver()
    dialect = drv.dialect
    try:
        trees = sqlglot.parse(sql, read=dialect)
    except SqlglotError as e:
        raise Refused(f'cannot parse the SQL: {str(e).splitlines()[0][:200]}') from e
    trees = [t for t in trees if t is not None]
    if len(trees) != 1:
        raise Refused('one statement at a time' if len(trees) > 1 else 'empty query')
    tree = trees[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Subquery, exp.With)) and not (hasattr(exp, 'SetOperation') and isinstance(tree, exp.SetOperation)):
        raise Refused('read-only: start with SELECT or WITH')
    bad = [n for n in tree.walk() if isinstance(n, (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Command, exp.Copy, exp.Merge, exp.Into, exp.Lock, exp.Set))]
    if bad:
        raise Refused('read-only: this statement writes or changes something')
    for n in tree.walk():
        if isinstance(n, (exp.Parameter, exp.SessionParameter, exp.Placeholder)):
            raise Refused('variables and parameters are not available here')
        if isinstance(n, exp.Func):
            name = n.name if isinstance(n, exp.Anonymous) else (n.sql_name() if hasattr(n, 'sql_name') else type(n).__name__)
            if drv.forbidden.match(name or ''):
                raise Refused(f'function {name} is not available here')
            if isinstance(n, exp.Anonymous) and not drv.allowed.match(name or ''):       # not a function the parser knows: allowed only from the list
                raise Refused(f'function {name} is not available here (only standard functions that compute from their arguments can be called)')
        if isinstance(n, exp.Table) and isinstance(n.this, (exp.Func, exp.Anonymous)):
            raise Refused('table functions are not available here')
    try:
        q = qualify(tree, schema=schema.mapping, dialect=dialect, validate_qualify_columns=True, quote_identifiers=True, identify=True)
    except SqlglotError as e:
        msg = str(e).splitlines()[0]
        m = re.match(r"Column '([^']+)' could not be resolved", msg)
        if m:
            raise Refused(f'column "{m.group(1)}" does not exist in the tables of this query. Use only columns of the model (see describe), and qualify ambiguous columns') from e
        raise Refused(f'{msg}. Use only tables and columns of the model (see describe), and qualify ambiguous columns') from e
    for n in q.walk():
        if _ROW is not None and isinstance(n, _ROW):               # `SELECT t FROM table t`, `to_json(t)`: the table's alias used as a value is the whole row
            raise Refused(f'"{n.name}" is a table, not a column. List the columns you need: a whole row is not available')
    hashed, ordered, unresolved, resolved = [], [], [], set()
    for scope in traverse_scope(q):
        for name, src in scope.sources.items():
            if isinstance(src, exp.Table):
                key = (src.db, src.name)
                if key not in schema.fields:
                    raise Refused(f'relation "{(src.db + ".") if src.db else ""}{src.name}" does not exist (it is not a table of the model)')
        for col in scope.columns:
            src = scope.sources.get(col.table)
            if src is None:
                unresolved.append(col)                     # a column of an outer query (checked in that scope), or not a column at all: settled below
                continue
            resolved.add(id(col))
            if not isinstance(src, exp.Table):
                continue                                   # a column of a subquery or CTE: its own scope checked where it comes from
            a = schema.fields[(src.db, src.name)].get(col.name)
            if a is None:
                raise Refused(f'unknown column {src.db}.{src.name}.{col.name}')
            d = decide(a, ent)
            ref = f'{src.name}.{col.name}'
            if d == 'deny':
                raise Refused(f'column {ref} is not available to this credential (it needs the {a.get("sensitivity")} entitlement'
                              f'{" and is never available to agents" if a.get("agent_access") == "none" else ""}). Leave it out; SELECT * is refused when a table has such columns.')
            if d == 'hash':
                if _in_count(col) or _plain_use(col):
                    continue
                if isinstance(col.parent, exp.Ordered):   # ordering by the real value would give away its rank: order by the hash instead
                    ordered.append(col)
                    continue
                other = _join_partner(col)
                if other is not None:
                    if _is_hashed(other, scope, schema, ent):
                        continue                           # two hashed columns: equal values already show as equal hashes
                    raise Refused(f'column {ref} is masked: it can be joined only to another masked column. Joining it to a readable column or to values of your own '
                                  f'would reveal it')
                if _project_root(col) and _is_root(scope):
                    hashed.append(col)
                    continue
                raise Refused(f'column {ref} is masked: it can be selected (as a stable hash, in the outermost query), counted, grouped, ordered and joined on, but not filtered or '
                              f'transformed. To find a record by this value, ask the owner of the data')
    for n in q.walk():                                         # the stars were expanded into columns; one that is left (alias.* inside a function) would carry a whole row unchecked
        if isinstance(n, exp.Star) and not isinstance(n.parent, exp.Count):
            raise Refused('list the columns you need: a whole row (alias.* inside a function or expression) is not available')
    for col in unresolved:                                    # `SELECT t FROM table t`, `to_json(t)`, `schema.table` used as a value: a row, not a column
        if id(col) not in resolved:
            raise Refused(f'"{col.name}" is not a column of the tables in this query. List the columns you need: a whole row (a table or its alias used as a value) is not available')
    for col in ordered:
        col.replace(drv.hash_expr(col))
    names = []
    for col in hashed:
        alias = col.parent.alias if isinstance(col.parent, exp.Alias) else col.name
        names.append(f'{col.table}.{col.name}')
        h = drv.hash_expr(col)
        if isinstance(col.parent, exp.Alias):
            col.replace(h)
        else:
            col.replace(exp.alias_(h, alias, quoted=True))
    if limit and not q.args.get('limit') and isinstance(q, exp.Select):
        q = q.limit(limit)
    return q.sql(dialect=dialect), {'hashed': names}
