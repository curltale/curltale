"""How many records a lookup filter really selected: information for query_data results, never a refusal.

A query often finds records by a descriptive text value and then uses their key: a CTE or subquery that selects an entity's key filtered on a text
column, or a filter on a text column of a table joined through its key. When such a filter matches several distinct records, the result says so
(filter_matches): how many, with a few examples. Whether that is expected is for the caller to judge; the rows are returned unchanged.

Nothing here names a field: what counts as a key, a code list or a text column comes from the model (role, allowed values, type), and a column the
caller cannot read raw is never probed (counting matches on it would reveal its values).
"""
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from . import enforce

MAX_PROBES = 3
EXAMPLES = 5
TEXT_TYPES = ('text', None)


def _conjuncts(where):
    """The AND-ed parts of a WHERE (a filter inside an OR is not a lookup on its own)."""
    out, todo = [], [where.this]
    while todo:
        n = todo.pop()
        if isinstance(n, exp.Paren):
            todo.append(n.this)
        elif isinstance(n, exp.And):
            todo += [n.left, n.right]
        else:
            out.append(n)
    return out


def _lookup_column(pred):
    """The column a positive text filter compares to string literals (=, LIKE, ILIKE, IN), else None."""
    if isinstance(pred, (exp.EQ, exp.Like, exp.ILike)):
        l, r = pred.left, pred.right
        if isinstance(l, exp.Column) and isinstance(r, exp.Literal) and r.is_string:
            return l
        if isinstance(r, exp.Column) and isinstance(l, exp.Literal) and l.is_string:
            return r
    if isinstance(pred, exp.In) and isinstance(pred.this, exp.Column):
        vals = pred.args.get('expressions') or []
        if vals and all(isinstance(v, exp.Literal) and v.is_string for v in vals):
            return pred.this
    return None


def _key_used(scope, alias, keys):
    """The filtered table's key leaves this scope (a CTE or subquery selecting it) or joins it to another table."""
    sel = scope.expression
    if not enforce._is_root(scope):
        for p in sel.expressions:
            c = p.this if isinstance(p, exp.Alias) else p
            if isinstance(c, exp.Column) and c.table == alias and c.name in keys:
                return True
    for j in sel.args.get('joins') or []:
        on = j.args.get('on')
        if on is None:
            continue
        for eq in on.find_all(exp.EQ):
            if any(isinstance(s, exp.Column) and s.table == alias and s.name in keys for s in (eq.left, eq.right)):
                return True
    where = sel.args.get('where')                                  # old-style joins: a.key = b.col in the WHERE
    if where is not None:
        for eq in where.find_all(exp.EQ):
            cols = [s for s in (eq.left, eq.right) if isinstance(s, exp.Column)]
            if len(cols) == 2 and any(c.table == alias and c.name in keys for c in cols) and any(c.table != alias for c in cols):
                return True
    return False


def lookups(sql, cat, ent, dialect):
    """[(entity key, field, schema, table, key column, predicate)] for the lookup filters of a query that already passed enforce.check."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
        q = qualify(tree, schema=cat.schema.mapping, dialect=dialect, validate_qualify_columns=False, quote_identifiers=True, identify=True)
    except Exception:
        return []
    out, seen = [], set()
    for scope in traverse_scope(q):
        sel = scope.expression
        if not isinstance(sel, exp.Select) or sel.args.get('where') is None:
            continue
        for pred in _conjuncts(sel.args['where']):
            col = _lookup_column(pred)
            src = scope.sources.get(col.table) if col is not None else None
            if not isinstance(src, exp.Table):
                continue
            k = cat.by_table.get(f'{src.db}.{src.name}')
            if k is None:
                continue
            e = cat.ents[k]
            a = e['attributes'].get(col.name)
            keys = [c for c, x in e['attributes'].items() if x['role'] == 'primary_key']
            if (a is None or len(keys) != 1 or a['role'] in ('primary_key', 'foreign_key') or a.get('allowed_values') or a.get('type') not in TEXT_TYPES
                    or enforce.decide(a, ent) != 'raw' or not _key_used(scope, col.table, keys)):
                continue
            p = pred.copy()
            for c in p.find_all(exp.Column):
                c.set('table', exp.to_identifier('t'))
            sig = (k, col.name, p.sql(dialect=dialect))
            if sig in seen:
                continue
            seen.add(sig)
            out.append((k, col.name, src.db, src.name, keys[0], p))
            if len(out) >= MAX_PROBES:
                return out
    return out


def report(sql, cat, ent, dialect, run):
    """filter_matches for a query: run(sql) executes a probe with the caller's own rules and returns its rows."""
    out = []
    for k, col, db, table, key, p in lookups(sql, cat, ent, dialect):
        t = exp.Table(this=exp.to_identifier(table, quoted=True), db=exp.to_identifier(db, quoted=True), alias=exp.TableAlias(this=exp.to_identifier('t')))
        count = exp.select(exp.Count(this=exp.Distinct(expressions=[exp.column(key, 't')])).as_('n')).from_(t).where(p.copy())
        try:
            n = int(run(count.sql(dialect=dialect))[0]['n'])
        except Exception:
            continue                                               # the probe is information only: if it cannot run, say nothing
        if n <= 1:
            continue
        name = cat.ents[k].get('name') or k
        item = {'entity': k, 'column': col, 'filter': _plain(p, dialect), 'distinct_records': n}
        try:
            ex = exp.select(exp.column(key, 't'), exp.column(col, 't')).distinct().from_(t.copy()).where(p.copy()).order_by(exp.column(key, 't')).limit(EXAMPLES)
            item['examples'] = run(ex.sql(dialect=dialect))
        except Exception:
            pass
        item['note'] = f'This filter on {col} matches {n} different {name} records (distinct {key}); the rows returned cover all of them.'
        out.append(item)
    return out


def _plain(p, dialect):
    """The predicate as the caller would read it (without the probe's table alias)."""
    q = p.copy()
    for c in q.find_all(exp.Column):
        c.set('table', None)
    return q.sql(dialect=dialect)
