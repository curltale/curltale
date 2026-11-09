"""Application-level enforcement of what a caller may read, for every query that goes to the data (agents, Alfred, the console).

The query is parsed (sqlglot), every column is traced to its real table through aliases, CTEs and subqueries, and the statement that finally runs is the one
generated from the checked tree, never the text the caller sent. Rules:
  * one read-only statement; only tables the model describes; no system functions;
  * restricted columns (agent_access none) are never readable without the restricted entitlement;
  * a confidential / personal column needs its entitlement, unless it is masked (agent_access masked): then the caller without the entitlement gets a stable hash;
  * a hashed column may be selected (outermost query only), counted, grouped, ordered and joined on; anywhere else (filters, functions, MIN/MAX, subquery outputs) it is refused,
    because comparing or transforming it would let a caller recover the value.
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
    """GROUP BY / ORDER BY / PARTITION BY on the bare column, or an equality between two bare columns in a join condition."""
    p = col.parent
    if isinstance(p, (exp.Group, exp.Ordered)):
        return True
    if isinstance(p, exp.Window) and col in (p.args.get('partition_by') or []):
        return True
    if isinstance(p, exp.EQ) and isinstance(p.left, exp.Column) and isinstance(p.right, exp.Column):
        n = p.parent
        while isinstance(n, (exp.And, exp.Paren)):
            n = n.parent
        return isinstance(n, exp.Join)
    return False


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
        if isinstance(n, (exp.Func, exp.Table)) and not isinstance(n, exp.Table):
            name = n.name if isinstance(n, exp.Anonymous) else (n.sql_name() if hasattr(n, 'sql_name') else type(n).__name__)
            if drv.forbidden.match(name or ''):
                raise Refused(f'function {name} is not available here')
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
    hashed = []
    for scope in traverse_scope(q):
        for name, src in scope.sources.items():
            if isinstance(src, exp.Table):
                key = (src.db, src.name)
                if key not in schema.fields:
                    raise Refused(f'relation "{(src.db + ".") if src.db else ""}{src.name}" does not exist (it is not a table of the model)')
        for col in scope.columns:
            src = scope.sources.get(col.table)
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
                if _project_root(col) and _is_root(scope):
                    hashed.append(col)
                    continue
                raise Refused(f'column {ref} is masked: it can be selected (as a stable hash, in the outermost query), counted, grouped, ordered and joined on, but not filtered or '
                              f'transformed. To find a record by this value, ask the owner of the data')
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
