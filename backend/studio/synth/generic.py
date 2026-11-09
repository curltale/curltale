"""Synthetic demo data for ANY model: rows for every entity, built only from what the uploaded model says.

  * keys: unique, following the field's example or format when given (an example "AB12345" gives "AB" + 5 digits), else <ENTITY>-000001
  * links: every foreign key points at a real row of the entity it references; parents are generated before children, cycles are closed afterwards.
    Every parent gets a child before any gets two, then a long tail (a few busy parents, many quiet ones). Required links (many-to-one) are never
    empty, optional ones sometimes are. Chains agree: an order line's order and its subscription belong to the same customer (an ancestor reached
    through two links is the same row), adding a parent row when none fits. Self-links form shallow trees (up to 4 levels). No AI here: the seed decides everything.
  * values: the field's allowed values when listed (the first ones more often), otherwise a value of the right type guessed from the column name
    (email, phone, person and company names, cities, amounts, rates, counts, dates in order: created before closed...)
  * optional AI step: one call per entity to the chat model, which proposes realistic values for its free-text columns; without a key, or when
    switched off, the guesses above are used. Everything is fictional; nothing comes from real data.
Calculated fields are not columns: nothing is generated for them.
"""
import json
import random
import re
import string
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from . import ddl

FIRST = ['Alex', 'Sam', 'Maria', 'John', 'Aisha', 'Wei', 'Lucas', 'Emma', 'Omar', 'Sofia', 'Daniel', 'Noor', 'Liam', 'Chiara', 'Ravi', 'Hannah', 'Mateo', 'Yuki', 'Fatima', 'Paul']
LAST = ['Smith', 'Rossi', 'Khan', 'Garcia', 'Chen', 'Muller', 'Silva', 'Haddad', 'Novak', 'Kim', 'Brown', 'Martin', 'Okafor', 'Ivanova', 'Dubois', 'Patel', 'Larsen', 'Costa']
COMPANY = ['Northwind', 'Bluebird', 'Summit', 'Harbor', 'Evergreen', 'Atlas', 'Pioneer', 'Crescent', 'Meridian', 'Oakridge', 'Silverline', 'Brightpath', 'Ironwood', 'Lakeside']
SUFFIX = ['Ltd', 'LLC', 'Group', 'Holdings', 'Partners', 'Trading', 'Services', 'Systems']
CITY = ['Lisbon', 'Milan', 'Oslo', 'Singapore', 'Toronto', 'Austin', 'Lyon', 'Osaka', 'Nairobi', 'Leeds', 'Porto', 'Valencia', 'Munich', 'Cork']
COUNTRY = ['Portugal', 'Italy', 'Singapore', 'Canada', 'United States', 'France', 'Japan', 'Kenya', 'United Kingdom', 'Spain', 'Germany', 'Ireland']
STREET = ['Main Street', 'Harbour Road', 'Park Avenue', 'Station Lane', 'River Walk', 'Market Square', 'Hill Road', 'Garden Close']
WORDS = ['standard', 'premium', 'basic', 'priority', 'regular', 'special', 'annual', 'monthly', 'online', 'retail', 'corporate', 'partner', 'internal', 'external']
SCALE = {'small': 0.35, 'medium': 1.0, 'large': 2.5}
KIND_ROWS = {'reference': 0.15, 'master': 1.0, 'supertype': 1.0, 'agreement': 1.2, 'transaction': 3.0, 'fact': 4.0, 'bridge': 1.5, 'relationship': 1.5, 'derived': 1.0, 'technical': 0.5}
BASE = 120
LATE = re.compile(r'(end|closed|close|resolved|expir|cancel|terminat|finish|complet|until|valid_to|deactivat|last_)')
EARLY = re.compile(r'(start|created|creation|open|begin|from|issued|registered|activation|first_|signed)')


class Generator:
    def __init__(self, onto, seed=7, scale='medium', suggest=None):
        """suggest: optional function(entity_key, entity, columns) -> {column: [values]} (the AI step); errors there fall back to guesses."""
        self.onto, self.ents = onto, onto['entities']
        self.rng = random.Random(seed)
        self.factor = SCALE.get(scale, 1.0)
        self.suggest = suggest
        self.fks = {}                                              # (entity, column) -> target entity
        for k, c, t in ddl.foreign_keys(onto):
            self.fks[(k, c)] = t
        self.optional, self.from_rels = {}, 0                      # (entity, column) -> True/False from the relationship's cardinality (None: unknown)
        self._links_from_relationships()
        self.rows, self.findings, self.suggested, self._fit = {}, [], {}, {}
        self.today = date.today()

    # ---- how many rows
    def _count(self, k):
        e = self.ents[k]
        parents = {t for (ek, c), t in self.fks.items() if ek == k and t != k}
        n = BASE * KIND_ROWS.get(e.get('kind'), 1.0) * self.factor
        if parents and e.get('kind') in (None, 'master', 'supertype'):
            n *= 1.5
        allowed = [a for a in e['attributes'].values() if a['role'] == 'primary_key' and a.get('allowed_values')]
        if allowed:                                                # a code table: as many rows as codes
            return len(allowed[0]['allowed_values'])
        return max(5, int(n))

    def _order(self):
        """Parents before children (self-links and cycles are filled in afterwards)."""
        deps = {k: {t for (ek, c), t in self.fks.items() if ek == k and t != k} for k in self.ents}
        done, order = set(), []
        while len(order) < len(self.ents):
            ready = [k for k in self.ents if k not in done and deps[k] <= done]
            if not ready:                                         # a cycle: take the entity with the fewest missing parents
                ready = [min((k for k in self.ents if k not in done), key=lambda k: len(deps[k] - done))]
            for k in ready:
                done.add(k)
                order.append(k)
        return order

    # ---- values
    def _id(self, k, a, i):
        ex = (a.get('example') or '').strip()
        if ex and len(ex) <= 40 and re.search(r'\d', ex):
            digits = sum(ch.isdigit() for ch in ex)
            num = str(i + 10 ** (digits - 1) if digits > 1 else i)[-digits:].rjust(digits, '0')
            out, j = [], 0
            for ch in ex:
                if ch.isdigit():
                    out.append(num[j]); j += 1
                else:
                    out.append(ch)
            return ''.join(out)
        if a['type'] == 'integer':
            return i
        return f"{re.sub(r'[^A-Z]', '', k.upper())[:4] or 'ID'}-{i:06d}"

    def _date(self, col, base=None):
        if base and LATE.search(col):
            return base + timedelta(days=self.rng.randint(1, 400))
        start = self.today - timedelta(days=3 * 365)
        return start + timedelta(days=self.rng.randint(0, 3 * 365 - 1))

    def _text(self, k, col, a, i):
        n = col.lower() + ' ' + (a.get('business_name') or '').lower()
        r = self.rng
        if 'email' in n or 'e_mail' in n:
            return f"{r.choice(FIRST).lower()}.{r.choice(LAST).lower()}{r.randint(1, 99)}@example.com"
        if re.search(r'phone|mobile|fax|tel\b', n):
            return f"+1 555 {r.randint(100, 999)} {r.randint(1000, 9999)}"
        if re.search(r'first[_ ]?name|given', n):
            return r.choice(FIRST)
        if re.search(r'last[_ ]?name|surname|family', n):
            return r.choice(LAST)
        if re.search(r'(full|contact|person|owner|manager|employee|user)[_ ]?name|^name\b', n):
            return f"{r.choice(FIRST)} {r.choice(LAST)}"
        if re.search(r'compan|organi|customer[_ ]?name|account[_ ]?name|vendor|supplier|partner[_ ]?name|business[_ ]?name|legal', n):
            return f"{r.choice(COMPANY)} {r.choice(SUFFIX)}"
        if 'city' in n:
            return r.choice(CITY)
        if 'country' in n:
            return r.choice(COUNTRY)
        if re.search(r'street|address', n):
            return f"{r.randint(1, 250)} {r.choice(STREET)}, {r.choice(CITY)}"
        if re.search(r'post(al)?[_ ]?code|zip', n):
            return f"{r.randint(10000, 99999)}"
        if re.search(r'url|website|link', n):
            return f"https://www.example.com/{k}/{i}"
        if re.search(r'currency', n):
            return r.choice(['EUR', 'USD', 'GBP'])
        if re.search(r'(^|_)(code|ref|reference|number|no|id)($|_| )', n):
            return f"{k[:3].upper()}{r.randint(100000, 999999)}"
        if re.search(r'descr|note|comment|remark|summary|text|reason|title|subject', n):
            return f"{(a.get('business_name') or col).capitalize()} {r.choice(WORDS)} {i}"
        if re.search(r'status|state|type|category|class|segment|tier|level|priority|channel|stage', n):
            return r.choice(['A', 'B', 'C']) + ' ' + (a.get('business_name') or col).split()[0].lower()
        return f"{(a.get('business_name') or col)} {i}"

    def _allowed(self, k, col, a):
        """The allowed values that fit the column's type (a model can list text values on a numeric column: then they are ignored, and said so)."""
        key = (k, col)
        if key not in self._fit:
            vals, t = a.get('allowed_values') or [], a.get('type') or 'text'
            conv = {'integer': int, 'boolean': lambda v: {'true': True, 'false': False, '1': True, '0': False}[str(v).lower()]}.get(t)
            if t.startswith('decimal'):
                conv = float
            if conv:
                ok = []
                for v in vals:
                    try:
                        ok.append(conv(v))
                    except (ValueError, KeyError, TypeError):
                        pass
                if vals and not ok:
                    self.findings.append(f'{k}.{col}: the allowed values are not {t} values: they are ignored and {t} values are generated')
                vals = ok
            self._fit[key] = vals
        return self._fit[key]

    def _value(self, k, col, a, i, row):
        r = self.rng
        vals = self._allowed(k, col, a) if a.get('allowed_values') else None
        if vals:
            return vals[min(int(r.expovariate(0.6)), len(vals) - 1)] if len(vals) > 2 else r.choice(vals)
        sug = self.suggested.get(k, {}).get(col)
        t = a['type']
        if sug and t in ('text', None):
            return r.choice(sug)
        if t == 'boolean':
            return r.random() < 0.5
        if t == 'integer':
            n = col.lower()
            if re.search(r'count|qty|quantity|number_of|items|seats|units', n):
                return r.randint(0, 40)
            if 'year' in n:
                return r.randint(self.today.year - 5, self.today.year)
            return r.randint(1, 1000)
        if t and t.startswith('decimal'):
            n = col.lower()
            if 'ratio' in t or re.search(r'ratio|rate|percent|pct|share|score', n):
                return round(r.random(), 4)
            if re.search(r'amount|price|cost|balance|revenue|fee|charge|total|value|salary|budget|limit|paid|due', n):
                return round(r.lognormvariate(5, 1.2), 2)
            return round(r.uniform(0, 1000), 2)
        if t in ('date', 'timestamp'):
            base = next((row[c] for c in row if row.get(c) is not None and EARLY.search(c) and isinstance(row[c], (date, datetime))), None)
            d = self._date(col.lower(), base.date() if isinstance(base, datetime) else base)
            if t == 'timestamp':
                return datetime.combine(d, datetime.min.time()) + timedelta(seconds=r.randint(0, 86399))
            return d
        if t == 'text[]':
            return r.sample(WORDS, r.randint(1, 3))
        return self._text(k, col, a, i)

    # ---- the AI step
    def _ask(self, progress):
        if not self.suggest:
            return
        todo = []
        for k, e in self.ents.items():
            cols = [{'column': c, 'meaning': a.get('business_name'), 'description': (a.get('description') or '')[:160], 'format': a.get('format'), 'sensitivity': a.get('sensitivity')}
                    for c, a in e['attributes'].items() if a.get('type') in ('text', None) and not a.get('allowed_values')
                    and a['role'] not in ('primary_key', 'foreign_key', 'lineage') and (k, c) not in self.fks]
            if cols:
                todo.append((k, e, cols[:25]))
        done = [0]

        def one(item):
            k, e, cols = item
            try:
                vals = self.suggest(k, e, cols) or {}
                self.suggested[k] = {c: [str(v) for v in vs if v not in (None, '')][:20] for c, vs in vals.items() if isinstance(vs, list) and vs}
            except Exception as ex:                                # one entity without suggestions is fine: the guesses take over
                self.findings.append(f'AI suggestions for {k} failed ({type(ex).__name__}): plain values used')
            done[0] += 1
            progress(f'asking the AI model for realistic values ({done[0]} of {len(todo)} entities)')
        progress(f'asking the AI model for realistic values (0 of {len(todo)} entities)')
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(one, todo))

    # ---- links: which column points where, and whether it may be empty
    def _links_from_relationships(self):
        """Cardinality for every foreign key, read from the relationship whose join uses it; a relationship whose join links a column to another
        entity's key, with no field marked as foreign key, becomes a link too (when the column types fit)."""
        ents = self.ents
        by_table = {}
        for k, e in ents.items():
            by_table.setdefault(e['semantic_table'], k)
            by_table.setdefault(ddl.table_of(e), k)
        for r in self.onto.get('relationships') or []:
            m = re.fullmatch(r'\s*([\w.]+)\.(\w+)\s*=\s*([\w.]+)\.(\w+)\s*', r.get('join') or '')
            if not m:
                continue
            sides = [(by_table.get(m.group(1)), m.group(2)), (by_table.get(m.group(3)), m.group(4))]
            if any(k is None or c not in ents[k]['attributes'] for k, c in sides):
                continue
            keys = [c == ddl.pk_col(ents[k]) and len(ddl.pk_cols(ents[k])) == 1 for k, c in sides]
            frm = ddl.resolve_ref(r.get('from') or '', ents)
            if keys[0] and keys[1]:                                # key to key (a subtype): the "from" side is the child
                child, parent = (sides[0], sides[1]) if sides[0][0] == frm else (sides[1], sides[0])
            elif keys[0] or keys[1]:
                child, parent = (sides[1], sides[0]) if keys[0] else (sides[0], sides[1])
            else:
                continue
            (ck, cc), (pk_, pc) = child, parent
            optional = 'zero' in (r.get('cardinality') or '') or 'optional' in (r.get('cardinality') or '') or r.get('status') == 'optional'
            if (ck, cc) not in self.fks:
                ca, pa = ents[ck]['attributes'][cc], ents[pk_]['attributes'][pc]
                if ca['role'] in ('primary_key', 'lineage') or (ca['type'] != pa['type'] and ca['type'] not in ('text', None)):
                    continue
                self.fks[(ck, cc)] = pk_
                self.from_rels += 1
            if self.fks[(ck, cc)] == pk_:
                self.optional[(ck, cc)] = optional and self.optional.get((ck, cc), True)

    def _plan(self, order):
        """From the model alone: the ancestors each entity inherits through its links, and the ones its rows must agree on.
        An ancestor reached through two links to different entities is checked (an order line's order and subscription must belong to the same
        customer). Not checked: shared code tables (kind reference), and an entity two columns point at directly (sales and service manager are
        two roles, not one chain), with everything above it."""
        pos = {k: i for i, k in enumerate(order)}
        self.fwd, self.later, self.S, self.enforced, self.anc_model = {}, [], {}, {}, {}
        for k in order:
            links = sorted((c, t) for (ek, c), t in self.fks.items() if ek == k)
            fwd = [(c, t) for c, t in links if t != k and pos[t] < pos[k]]
            self.later += [(k, c, t) for c, t in links if (c, t) not in fwd]
            for c, t in fwd:
                self.S[(k, c)] = ({t} | self.anc_model[t]) if self.ents[t].get('kind') != 'reference' else set()
            per_target = {}
            for c, t in fwd:
                per_target.setdefault(t, []).append(c)
            roles = set().union(*[self.S[(k, cs[0])] for t, cs in per_target.items() if len(cs) > 1])
            reached = {}
            for c, t in fwd:
                for A in self.S[(k, c)]:
                    reached.setdefault(A, set()).add(t)
            self.enforced[k] = {A for A, ts in reached.items() if len(ts) > 1} - roles
            self.anc_model[k] = set(reached) - roles
            # required links lead (they cannot give way), the most connected first; optional ones then follow the chain or stay empty
            self.fwd[k] = sorted(fwd, key=lambda ct: (self.optional.get((k, ct[0])) is not False, -len(self.S[(k, ct[0])] & self.enforced[k]),
                                                      -len(self.S[(k, ct[0])]), ct[0]))

    def _deck(self, k, c, t):
        d = self.decks.get((k, c))
        if d is None:
            d = self.decks[(k, c)] = _Deck(len(self.rows[t]), self.rng)
            self.decks_of.setdefault(t, []).append(d)
        return d

    def _match(self, t, cons):
        """Rows of t whose ancestors are the given ones (t itself may be one: then it is that very row)."""
        found = None
        for A, v in sorted(cons.items(), key=lambda x: str(x[0])):
            idx = [self.pkidx[t][v]] if A == t and v in self.pkidx[t] else [] if A == t else self.byanc.get((t, A), {}).get(v, [])
            found = list(idx) if found is None else [i for i in found if i in set(idx)]
            if not found:
                return []
        return found or []

    def _creatable(self, t):
        e = self.ents[t]
        return e.get('kind') != 'reference' and not any((t, c) in self.fks or e['attributes'][c].get('allowed_values') for c in ddl.pk_cols(e))

    def _row(self, k, fixed, depth=0):
        """One row of k; fixed: ancestors the row must have (when it is created for a child that needs it)."""
        e = self.ents[k]
        pks = ddl.pk_cols(e)
        self.counter[k] += 1
        i = self.counter[k]
        row, cons, anc = {}, dict(fixed), {}
        for col, t in self.fwd[k]:
            S = self.S[(k, col)]
            relevant = {A: cons[A] for A in S if A in cons and (A in self.enforced[k] or A in fixed)}
            opt = self.optional.get((k, col))
            rate = 0.15 if opt else 0.08 if opt is None else 0
            if col not in pks and not (S & set(fixed)) and self.rng.random() < rate:
                row[col] = None
                continue
            d = self._deck(k, col, t)
            j = None
            if relevant:
                cands = self._match(t, relevant)
                if not cands and depth < 6 and self._creatable(t):
                    made = self._create(t, {A: v for A, v in relevant.items() if A != t}, depth + 1)
                    cands = [made] if made is not None else []
                if cands:
                    j = d.least(cands)
                elif opt is not False and col not in pks and not (S & set(fixed)):   # optional: better empty than pointing at another chain
                    row[col] = None
                    continue
                else:
                    self.broken += 1
            if j is None:
                j = d.draw()
            d.used[j] += 1
            row[col] = self.rows[t][j][ddl.pk_col(self.ents[t])]
            if S:
                full = {**self.anc[t][j], t: row[col]}
                for A in sorted(S):
                    if A in full:
                        cons.setdefault(A, full[A])
                        if A in self.anc_model[k]:
                            anc.setdefault(A, full[A])
        for col, a in e['attributes'].items():
            if col in row:
                continue
            if (k, col) in self.fks:                               # filled once every row exists (self-links and cycles)
                row[col] = None
            elif col in pks and len(pks) == 1:
                allowed = a.get('allowed_values')
                row[col] = allowed[i - 1] if allowed and i <= len(allowed) else self._id(k, a, i)
            elif a['role'] == 'lineage':
                row[col] = (datetime.now() - timedelta(hours=self.rng.randint(1, 72))) if a['type'] == 'timestamp' else 'demo'
            else:
                row[col] = self._value(k, col, a, i, row)
        return row, anc

    def _add(self, k, row, anc):
        key = tuple(row.get(c) for c in ddl.pk_cols(self.ents[k]))
        if None in key or key in self.seen[k]:                    # a composite key that repeats: skip the row
            return None
        self.seen[k].add(key)
        j = len(self.rows[k])
        self.rows[k].append(row)
        self.anc[k].append(anc)
        self.pkidx[k].setdefault(row.get(ddl.pk_col(self.ents[k])), j)
        for A, v in anc.items():
            self.byanc.setdefault((k, A), {}).setdefault(v, []).append(j)
        for d in self.decks_of.get(k, []):
            d.grow()
        return j

    def _create(self, t, fixed, depth):
        """A parent row that a required link needs (e.g. a subscription for the customer of this order line)."""
        row, anc = self._row(t, fixed, depth)
        if any(anc.get(A) != v for A, v in fixed.items()):
            return None
        j = self._add(t, row, anc)
        if j is not None:
            self.created[t] = self.created.get(t, 0) + 1
        return j

    def _close_later(self):
        """Self-links make shallow trees: a row points only at rows before it (no loops), at most 4 levels deep, and many rows stay at the top
        whatever the cardinality says (someone has to be the root). Cycles point at any row."""
        for k, col, t in self.later:
            rows, pcol = self.rows[k], ddl.pk_col(self.ents[t])
            p = 1.0 if self.optional.get((k, col)) is False else 0.6
            if col in ddl.pk_cols(self.ents[k]):
                continue
            depth = []
            for j, row in enumerate(rows):
                depth.append(1)
                if t == k:
                    cands = [i for i in range(j) if depth[i] < 4]
                    if cands and self.rng.random() < 0.6:
                        i = self.rng.choice(cands)
                        row[col], depth[j] = rows[i][pcol], depth[i] + 1
                elif self.rows[t] and self.rng.random() < p:
                    d = self._deck(k, col, t)
                    i = d.draw()
                    d.used[i] += 1
                    row[col] = self.rows[t][i][pcol]

    def chain_conflicts(self):
        """Rows whose links reach the same checked ancestor through two routes and disagree on it (0 when the data is consistent)."""
        n = 0
        for k, rows in self.rows.items():
            if not self.enforced.get(k):
                continue
            for row in rows:
                seen = {}
                for col, t in self.fwd[k]:
                    v = row.get(col)
                    if v is None or v not in self.pkidx[t]:
                        continue
                    full = {**self.anc[t][self.pkidx[t][v]], t: v}
                    for A in self.S[(k, col)] & self.enforced[k]:
                        if A in full and seen.setdefault(A, full[A]) != full[A]:
                            n += 1
        return n

    def link_report(self):
        out = []
        for (k, col), t in sorted(self.fks.items()):
            if t == k or not self.rows[t]:
                continue
            pcol = ddl.pk_col(self.ents[t])
            used = {r.get(col) for r in self.rows[k]} - {None}
            out.append({'child': k, 'column': col, 'parent': t, 'parents': len(self.rows[t]), 'children': len(self.rows[k]),
                        'childless': sum(1 for r in self.rows[t] if r[pcol] not in used), 'required': self.optional.get((k, col)) is False,
                        'empty': sum(1 for r in self.rows[k] if r.get(col) is None)})
        return out

    # ---- build
    def build(self, progress=lambda m: None):
        self._ask(progress)
        progress('generating rows')
        order = self._order()
        self._plan(order)
        self.decks, self.decks_of, self.byanc, self.broken, self.created = {}, {}, {}, 0, {}
        self.anc, self.pkidx, self.seen = {k: [] for k in order}, {k: {} for k in order}, {k: set() for k in order}
        self.counter = {k: 0 for k in order}
        for k in order:
            self.rows[k] = []
            n = self._count(k)
            while self.counter[k] < n:
                row, anc = self._row(k, {})
                self._add(k, row, anc)
        self._close_later()
        self._report()
        return self.rows

    def _report(self):
        rep = self.link_report()
        full = [r for r in rep if r['children'] >= r['parents']]
        share = round(100 * sum(r['parents'] - r['childless'] for r in full) / max(1, sum(r['parents'] for r in full)))
        self.findings.append(f'Links: {len(rep)} foreign keys filled' + (f' ({self.from_rels} of them from the Relationships sheet only)' if self.from_rels else '')
                             + f'; where a link has at least as many children as parents, {share}% of the parents have at least one child.')
        req_empty = sum(r['empty'] for r in rep if r['required'])
        if req_empty:
            self.findings.append(f'{req_empty} required links were left empty.')
        if self.created:
            self.findings.append('Chains: rows added so that linked rows share the same ancestor: '
                                 + ', '.join(f'{n} {k}' for k, n in sorted(self.created.items())) + '.')
        if self.broken:
            self.findings.append(f'Chains: {self.broken} links could not follow the same ancestor and point elsewhere.')


class _Deck:
    """Hands out the rows of a parent: every row once first (in shuffled order), then a long tail (a few parents get many children)."""

    def __init__(self, n, rng):
        self.rng, self.used, self.order = rng, [0] * n, list(range(n))
        rng.shuffle(self.order)
        self.first = self.order[::-1]
        self._cum = None

    def grow(self):
        self.used.append(0)
        self.order.append(len(self.used) - 1)
        self.first.append(len(self.used) - 1)
        self._cum = None

    def draw(self):
        while self.first:
            j = self.first.pop()
            if self.used[j] == 0:
                return j
        if self._cum is None:
            acc, self._cum = 0.0, []
            for r in range(len(self.order)):
                acc += 1 / (r + 1) ** 0.8
                self._cum.append(acc)
        return self.rng.choices(self.order, cum_weights=self._cum)[0]

    def least(self, cands):
        m = min(self.used[c] for c in cands)
        return self.rng.choice([c for c in cands if self.used[c] == m])


def ai_suggester(call_model):
    """The AI step, given call_model(system, user) -> text. One call per entity; the answer must be JSON {column: [values]}."""
    system = ('You invent realistic but FICTIONAL sample values for a demo database. Reply with JSON only: {"column": ["value", ...]} with 12 '
              'varied values per column, matching the column meaning and format. Never real people, real companies or real identifiers.')

    def suggest(key, entity, cols):
        user = json.dumps({'entity': entity.get('name'), 'definition': (entity.get('definition') or '')[:300], 'columns': cols}, ensure_ascii=False)
        text = call_model(system, user)
        m = re.search(r'\{.*\}', text, re.S)
        return json.loads(m.group(0)) if m else {}
    return suggest
