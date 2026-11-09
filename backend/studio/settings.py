"""Settings an admin can change from the Settings page, applied on the next request (no restart).

Each value comes from, in order: the Settings page (stored in app_setting), the environment (.env), the built-in default. Only settings that are
read when used are here; what the app needs before it can reach its database, what describes the deployment (proxies, ports) and what would
need a rebuild (the embedding model) stays in the environment. SETTINGS_LOCKED=true makes the page read-only and ignores stored values, for
teams that keep configuration in files only. The API key is stored encrypted and never sent back.
"""
import os
import threading
import time

from . import config
from .db import J, conn

LOCKED = os.environ.get('SETTINGS_LOCKED', '').lower() in ('1', 'true', 'yes')
TTL = 5                                                            # seconds a read value is reused (other app processes pick changes up within this)

# name: (group, label, type, min, max, help). The default is the config value of the same name (env, else built-in).
SPECS = {
    'LLM_BASE_URL': ('AI', 'AI endpoint address', 'str', None, None, 'Any OpenAI-compatible server: https://openrouter.ai/api/v1, a LiteLLM gateway, Ollama (http://host:11434/v1), vLLM...'),
    'OPENROUTER_API_KEY': ('AI', 'API key', 'secret', None, None, 'Key for the endpoint above (stored encrypted). Optional for servers that need none, such as a local Ollama. Used by Alfred, the demo-data builder and remote embeddings.'),
    'CHAT_MODEL': ('AI', 'Assistant model', 'str', None, None, 'Model id as the endpoint knows it, e.g. openai/gpt-4.1-mini or llama3.1. Alfred and the other AI features use it unless a feature has its own.'),
    'CHAT_REASONING': ('AI', 'Model reasoning', 'bool', None, None, 'Let the model reason before answering: better on hard questions, slower and dearer.'),
    'ALFRED_TEMPERATURE': ('AI', 'Temperature', 'float', 0, 1, 'Lower is more predictable.'),
    'ALFRED_MAX_LOOKUPS': ('AI', 'Lookups per question', 'int', 3, 20, 'Tool rounds Alfred may use before it must answer with what it found.'),
    'ALFRED_TOOL_RESULT_CHARS': ('AI', 'Tool result passed to the model (characters)', 'int', 2000, 64000, 'Longer results are cut before the model reads them.'),
    'AGENT_MAX_ROWS': ('Ontology & data', 'Rows returned to agents', 'int', 10, 1000, 'Most rows query_data returns to Alfred and MCP clients. A longer result says how many rows there are in all and suggests counting, grouping or filtering.'),
    'CONSOLE_MAX_ROWS': ('Ontology & data', 'Rows in the SQL console', 'int', 10, 5000, 'A connector can set a lower limit of its own.'),
    'RATE_PER_S': ('Limits', 'Requests per second (per client)', 'float', 1, 1000, 'Every API and MCP request, per client address.'),
    'RATE_BURST': ('Limits', 'Burst (per client)', 'int', 1, 10000, 'Requests a client may make at once before the rate applies.'),
    'LOGIN_PER_MIN': ('Limits', 'Sign-in attempts per minute', 'int', 1, 100, 'Per client address.'),
    'ALFRED_PER_MIN': ('Limits', 'Alfred questions per minute', 'int', 1, 600, 'Per user (each question costs model calls).'),
    'INGEST_PER_MIN': ('Limits', 'Heavy actions per minute', 'int', 1, 600, 'Per user: uploads, web addresses, refreshes, page regeneration, topic revisions, link suggestions and interview drafts. Each starts background work and may cost model calls.'),
    'SESSION_HOURS': ('General', 'Sign-in lasts (hours)', 'int', 1, 720, 'Applies to new sign-ins.'),
    'ACTIVITY_DAYS': ('General', 'Keep the activity log (days)', 'int', 1, 3650, 'Steps older than this are deleted (checked every hour).'),
    'ACTIVITY_RESULT_CHARS': ('General', 'Tool response kept in the log (characters)', 'int', 0, 100000, '0 keeps only the size and the summary.'),
    'LIBRARY_ENABLED': ('Library', 'Library (documents and collections)', 'bool', None, None, 'Turn the document Library on or off. Off hides it from the menu and the API; nothing is deleted.'),
    'MODEL_WRITER': ('AI', 'Model that writes source pages', 'str', None, None, 'Empty uses the assistant model.'),
    'MODEL_VERIFIER': ('AI', 'Model that checks source pages against the document', 'str', None, None, 'Must support tool calling. Empty uses the assistant model. A different model from the writer catches more.'),
    'MODEL_ANALYZER': ('AI', 'Model that decides which topics a source changes', 'str', None, None, 'Must support tool calling. Empty uses the assistant model.'),
    'MODEL_TOPIC_WRITER': ('AI', 'Model that writes topic pages', 'str', None, None, 'Must support tool calling. Empty uses the assistant model.'),
    'MODEL_INTERVIEW': ('AI', 'Model that conducts interviews', 'str', None, None, 'The model that talks with a person to capture what they know, and extracts the facts. Empty uses the assistant model.'),
    'MODEL_OCR': ('AI', 'Model that reads images and scanned pages (OCR)', 'str', None, None, 'A model that accepts images. Empty uses the assistant model, which must then accept images too. Without a working one, scanned pages and pictures cannot be read.'),
    'MAX_AMENDMENT_ROUNDS': ('Library', 'Correction rounds per source page', 'int', 0, 8, 'After a check finds problems the page is corrected and checked again, up to this many times; then it goes to a person for review.'),
    'WRITER_SECTION_TOKENS': ('Library', 'Longest text written in one go (tokens)', 'int', 1000, 100000, 'Longer documents are written section by section and joined. Keep it well below what the writer model accepts.'),
    'OCR_MAX_PAGES': ('Library', 'Most scanned pages read per document', 'int', 1, 1000, 'More are spread evenly over the document. Each page is one model call.'),
    'LIBRARY_MIN_SIMILARITY': ('Library', 'Smallest similarity for a passage to count as a match', 'float', 0, 1, 'Cosine similarity between a question and a passage. 0 returns the nearest passages whatever their similarity (every question gets an answer, however weak). Models differ: BGE scores unrelated text about 0.4-0.6, so try 0.6-0.7 there.'),
    'FETCH_ALLOWED_HOSTS': ('Web capture', 'Internal hosts web capture may reach', 'str', None, None, 'Web capture refuses private and internal addresses. List here the hosts or networks it may use anyway, separated by commas: intranet.example.com, 10.20.0.0/16. Empty allows none.'),
    'FETCH_MAX_MB': ('Web capture', 'Largest web page or file captured (MB)', 'int', 1, 200, ''),
    'FETCH_RENDER': ('Web capture', 'Render pages with the browser service', 'enum', ('never', 'auto', 'always'), None, 'auto: only when a page has little text without JavaScript. Needs the browser service address below.'),
    'FETCHER_URL': ('Web capture', 'Browser service address', 'str', None, None, 'The address of the optional browser container, e.g. http://fetcher:8001. Empty: pages are fetched without running JavaScript.'),
    'FETCHER_TOKEN': ('Web capture', 'Browser service token', 'secret', None, None, 'The shared secret set as FETCHER_TOKEN on the browser container.'),
    'DUPLICATE_SIMILARITY': ('Library', 'Similarity at which a new topic is held as a probable duplicate', 'float', 0.5, 1, 'A new topic whose title, aliases and summary are at least this similar to an existing topic waits for a person to decide. Models differ: BGE rates related topics 0.7-0.85 and the same subject above 0.9.'),
    'RERANK_URL': ('AI', 'Reranker address (optional)', 'str', None, None, 'A Cohere-style /rerank endpoint (e.g. https://api.cohere.com/v2/rerank, or a self-hosted one). Used by Library search for agents to put the best topics first. Empty: not used.'),
    'RERANK_MODEL': ('AI', 'Reranker model', 'str', None, None, 'Model name sent to the reranker.'),
    'RERANK_API_KEY': ('AI', 'Reranker key', 'secret', None, None, 'Stored encrypted. Empty for a reranker that needs none.'),
    'LIBRARY_WORKERS': ('Library', 'Background workers in this process', 'int', 0, 8, 'How many documents are processed at once by the web process itself. 0 when a separate worker container does the work. Applies after a restart.'),
    'LIBRARY_MAX_UPLOAD_MB': ('Library', 'Largest upload (MB)', 'int', 1, 2000, 'Larger files are refused.'),
    'STORAGE_DRIVER': ('Storage', 'Where original files are kept', 'enum', ('local', 's3'), None, 'local: the data folder mounted into the app. s3: any S3-compatible bucket (AWS, MinIO, Ceph...).'),
    'S3_ENDPOINT': ('Storage', 'S3 endpoint', 'str', None, None, 'Leave empty for AWS. For MinIO or another server: its address, e.g. https://s3.example.com.'),
    'S3_BUCKET': ('Storage', 'S3 bucket', 'str', None, None, 'The bucket must exist. Turn on versioning if you want to keep old versions: deleting a source removes every version.'),
    'S3_REGION': ('Storage', 'S3 region', 'str', None, None, 'Optional.'),
    'S3_ACCESS_KEY': ('Storage', 'S3 access key', 'str', None, None, ''),
    'S3_SECRET_KEY': ('Storage', 'S3 secret key', 'secret', None, None, 'Stored encrypted.'),
    'S3_PATH_STYLE': ('Storage', 'Path-style addressing', 'bool', None, None, 'On for MinIO and most self-hosted servers.'),
    'S3_PREFIX': ('Storage', 'S3 key prefix', 'str', None, None, 'Optional folder inside the bucket.'),
    'FOUR_EYES': ('General', 'Four-eyes rule', 'bool', None, None, 'Whoever changed a draft cannot publish it; another publisher (or admin) must.'),
}
STORE_KEY = {'FOUR_EYES': 'four_eyes'}                                # stored before this page existed: kept under its old key
DEFAULTS = {'MODEL_INTERVIEW': '', 'RERANK_URL': '', 'RERANK_MODEL': '', 'RERANK_API_KEY': '', 'DUPLICATE_SIMILARITY': 0.92, 'MODEL_ANALYZER': '', 'MODEL_TOPIC_WRITER': '', 'FETCH_ALLOWED_HOSTS': '', 'FETCH_MAX_MB': 25, 'FETCH_RENDER': 'auto', 'FETCHER_URL': '', 'FETCHER_TOKEN': '', 'MODEL_WRITER': '', 'MODEL_VERIFIER': '', 'MAX_AMENDMENT_ROUNDS': 3, 'WRITER_SECTION_TOKENS': 8000, 'LIBRARY_MIN_SIMILARITY': 0.0, 'MODEL_OCR': '', 'OCR_MAX_PAGES': 100, 'FOUR_EYES': False, 'LIBRARY_ENABLED': True, 'LIBRARY_WORKERS': 2, 'LIBRARY_MAX_UPLOAD_MB': 100, 'STORAGE_DRIVER': 'local', 'S3_ENDPOINT': '', 'S3_BUCKET': '',
            'S3_REGION': '', 'S3_ACCESS_KEY': '', 'S3_SECRET_KEY': '', 'S3_PATH_STYLE': False, 'S3_PREFIX': ''}
OPTIONAL = {'MODEL_INTERVIEW', 'RERANK_URL', 'RERANK_MODEL', 'MODEL_ANALYZER', 'MODEL_TOPIC_WRITER', 'FETCH_ALLOWED_HOSTS', 'FETCHER_URL', 'MODEL_WRITER', 'MODEL_VERIFIER', 'MODEL_OCR', 'S3_ENDPOINT', 'S3_REGION', 'S3_ACCESS_KEY', 'S3_PREFIX'}              # may be saved empty

_cache = {'at': 0.0, 'values': {}}
_lock = threading.Lock()


class SettingError(ValueError):
    pass


def _key(name):
    return STORE_KEY.get(name, 'cfg.' + name)


def _stored():
    with _lock:
        if time.monotonic() - _cache['at'] < TTL:
            return _cache['values']
    keys = {_key(n): n for n in SPECS}
    try:
        with conn() as c:
            rows = c.execute('SELECT key, value FROM app_setting WHERE key = ANY(%s)', (list(keys),)).fetchall()
        values = {keys[r['key']]: r['value'] for r in rows}
    except Exception:                                              # no database yet (or briefly unreachable): the environment decides
        values = {}
    with _lock:
        _cache.update(at=time.monotonic(), values=values)
    return values


def forget():
    with _lock:
        _cache['at'] = 0.0


def _decrypt(v):
    from . import connectors
    try:
        return connectors.decrypt(v['enc'])
    except Exception:
        return None


def _from_page(name):
    """Stored values count unless the settings are locked; governance switches (no environment variable) always live on the page."""
    return name in _stored() and (not LOCKED or name in STORE_KEY)


def source(name):
    if _from_page(name):
        return 'page'
    return 'env' if name not in STORE_KEY and os.environ.get(name) not in (None, '') else 'default'


def editable(name):
    return not LOCKED or name in STORE_KEY


def get(name):
    """The value in force now."""
    stored = _stored()
    if _from_page(name):
        v = stored[name]
        if SPECS[name][2] == 'secret':
            v = _decrypt(v) if isinstance(v, dict) else None
            if v:
                return v
        else:
            return v
    if name in DEFAULTS:
        e = _from_env(name)
        return DEFAULTS[name] if e is None else e
    return getattr(config, name)


def _from_env(name):
    """The environment's value for a setting that has a built-in default, read the way the Settings page would (None when unset or not valid)."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == '' or name in STORE_KEY:
        return None
    kind = SPECS[name][2]
    try:
        if kind == 'bool':
            low = raw.strip().lower()
            if low in ('1', 'true', 'yes', 'on'):
                return True
            if low in ('0', 'false', 'no', 'off'):
                return False
            return None
        return _coerce(name, int(raw) if kind == 'int' else float(raw) if kind == 'float' else raw)
    except (SettingError, ValueError):
        return None


def _coerce(name, value):
    group, label, kind, lo, hi, _ = SPECS[name]
    try:
        if kind == 'bool':
            if not isinstance(value, bool):
                raise ValueError
            v = value
        elif kind == 'int':
            if isinstance(value, bool):
                raise ValueError
            v = int(value)
            if float(value) != v:
                raise ValueError
        elif kind == 'float':
            if isinstance(value, bool):
                raise ValueError
            v = float(value)
        elif kind == 'enum':
            v = str(value).strip()
        else:
            v = str(value).strip()
            if not v and name in OPTIONAL:
                return ''
            if not v:
                raise ValueError
            if len(v) > 300:
                raise SettingError(f'{label}: too long')
    except (TypeError, ValueError):
        raise SettingError(f'{label}: expected {"on/off" if kind == "bool" else "a whole number" if kind == "int" else "a number" if kind == "float" else "a value"}')
    if kind == 'enum' and v not in lo:
        raise SettingError(f'{label}: one of {", ".join(lo)}')
    if lo is not None and kind in ('int', 'float') and not lo <= v <= hi:
        raise SettingError(f'{label}: between {lo} and {hi}')
    return v


def _shown(name, v):
    return ('set' if v else 'not set') if SPECS[name][2] == 'secret' else v


def listing():
    out = []
    for name, (group, label, kind, lo, hi, help_) in SPECS.items():
        v = get(name)
        env = os.environ.get(name)
        out.append({'name': name, 'group': group, 'label': label, 'type': kind, 'min': None if kind == 'enum' else lo, 'max': hi, 'options': list(lo) if kind == 'enum' else None, 'help': help_,
                    'value': None if kind == 'secret' else v, 'is_set': bool(v) if kind == 'secret' else None, 'source': source(name), 'editable': editable(name),
                    'env': None if env in (None, '') or name in STORE_KEY else ('set' if kind == 'secret' else env)})
    return {'locked': LOCKED, 'settings': out}


def put(name, value, user):
    if name not in SPECS:
        raise SettingError(f'unknown setting {name}')
    if not editable(name):
        raise SettingError('settings are locked (SETTINGS_LOCKED): change them in the environment')
    v = _coerce(name, value)
    before = _shown(name, get(name))
    from . import connectors
    stored = {'enc': connectors.encrypt(v)} if SPECS[name][2] == 'secret' else v
    with conn() as c:
        c.execute('INSERT INTO app_setting (key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value', (_key(name), J(stored)))
        c.execute("INSERT INTO audit_log (username,action,kind,key,before,after) VALUES (%s,'setting','setting',%s,%s,%s)",
                  (user, name, J(before), J(_shown(name, v))))
    forget()
    _applied(name)


def reset(name, user):
    """Back to the environment value (or the default)."""
    if name not in SPECS:
        raise SettingError(f'unknown setting {name}')
    if not editable(name):
        raise SettingError('settings are locked (SETTINGS_LOCKED): change them in the environment')
    before = _shown(name, get(name))
    with conn() as c:
        c.execute('DELETE FROM app_setting WHERE key=%s', (_key(name),))
        c.execute("INSERT INTO audit_log (username,action,kind,key,before,after) VALUES (%s,'setting','setting',%s,%s,%s)", (user, name, J(before), J('reset')))
    forget()
    _applied(name)


def _applied(name):
    if name in ('RATE_PER_S', 'RATE_BURST', 'LOGIN_PER_MIN', 'ALFRED_PER_MIN', 'INGEST_PER_MIN'):
        from . import ratelimit
        ratelimit.reconfigure()
