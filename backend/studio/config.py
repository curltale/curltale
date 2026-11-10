import os
from pathlib import Path


def _load_dotenv():
    # local development: read app/.env if present (real environment variables win)
    f = Path(__file__).resolve().parents[2] / '.env'
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())
    pw = os.environ.get('DB_PASSWORD')
    if pw:
        os.environ.setdefault('DATABASE_URL', f'postgresql://studio:{pw}@127.0.0.1:5433/studio')


_load_dotenv()


def env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, '') else default


DATABASE_URL = env('DATABASE_URL', 'postgresql://studio:studio@127.0.0.1:5433/studio')
SECRET_KEY = env('SECRET_KEY', 'dev-secret')
OPENROUTER_API_KEY = env('OPENROUTER_API_KEY')
OPENROUTER_BASE = env('OPENROUTER_BASE', 'https://openrouter.ai/api/v1')
LLM_BASE_URL = env('LLM_BASE_URL', OPENROUTER_BASE)         # any OpenAI-compatible server; changed on the Settings page
# 'local': the model runs inside this container (about 30 ms per query, no network); 'openrouter': remote model (about 300-600 ms, needs the key)
EMBEDDING_PROVIDER = env('EMBEDDING_PROVIDER', 'local')
EMBEDDING_MODEL = env('EMBEDDING_MODEL', 'BAAI/bge-base-en-v1.5')
EMBEDDING_DIM = int(env('EMBEDDING_DIM', '768'))
EMBEDDING_MAX_TOKENS = int(env('EMBEDDING_MAX_TOKENS', '512'))     # what the model reads of one passage; the Library cuts documents to fit it
# BGE models retrieve better when the question (not the stored text) carries this instruction; empty for models that do not need it
EMBEDDING_QUERY_PREFIX = env('EMBEDDING_QUERY_PREFIX', 'Represent this sentence for searching relevant passages: ')
DATA_DIR = env('DATA_DIR', '/app/data')                        # the mounted data folder (backups, file databases, Library files)
SERVICE_ROLE = env('SERVICE_ROLE', 'all')                    # all: web + job workers in one process; api: web only; worker: python -m studio.worker
MODEL_CACHE = env('MODEL_CACHE', '/app/models')
# Qwen3.x and similar models 'think' before answering (slow, 20-60 s): off by default, set CHAT_REASONING=true to allow it
CHAT_REASONING = env('CHAT_REASONING', 'false').lower() in ('1', 'true', 'yes')
CHAT_MODEL = env('CHAT_MODEL', 'openai/gpt-4.1-mini')
DATA_DB = env('DATA_DB_NAME', 'semantic_data')
ADMIN_USER = env('INITIAL_ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = env('INITIAL_ADMIN_PASSWORD')
SESSION_HOURS = int(env('SESSION_HOURS', '12'))
ACTIVITY_DAYS = int(env('ACTIVITY_DAYS', '30'))           # how long the activity log keeps sessions
ACTIVITY_RESULT_CHARS = int(env('ACTIVITY_RESULT_CHARS', '8000'))   # tool responses kept in the activity log, cut to this size (0: not kept)
# behind a proxy: who may tell us the client address (see clientinfo.py)
TRUSTED_PROXIES = env('TRUSTED_PROXIES', '127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16')
CLIENT_IP_HEADER = env('CLIENT_IP_HEADER', 'X-Forwarded-For')
# rate limits enforced by the app itself (ratelimit.py), whatever proxy is in front
RATE_PER_S = float(env('RATE_PER_S', '20'))
RATE_BURST = int(env('RATE_BURST', '60'))
LOGIN_PER_MIN = int(env('LOGIN_PER_MIN', '10'))
ALFRED_PER_MIN = int(env('ALFRED_PER_MIN', '20'))
INGEST_PER_MIN = int(env('INGEST_PER_MIN', '20'))                  # documents one user may add per minute (uploads, web addresses)
MAX_BODY_MB = int(env('MAX_BODY_MB', '4'))                         # largest ordinary request (JSON); uploads and Excel models have their own limits (bodylimit.py)

# Alfred and data access (also changeable on the Settings page, see settings.py)
ALFRED_TEMPERATURE = float(env('ALFRED_TEMPERATURE', '0.2'))
ALFRED_MAX_LOOKUPS = int(env('ALFRED_MAX_LOOKUPS', '10'))       # tool rounds before Alfred must answer with what it found
ALFRED_TOOL_RESULT_CHARS = int(env('ALFRED_TOOL_RESULT_CHARS', '48000'))   # agents receive whole topic pages; this is a safety cap, not a budget
AGENT_MAX_ROWS = int(env('AGENT_MAX_ROWS', '100'))
CONSOLE_MAX_ROWS = int(env('CONSOLE_MAX_ROWS', '500'))


# ---------------------------------------------------------------- secrets a deployment must set itself
PLACEHOLDERS = ('change-me', 'changeme', 'dev-secret', 'secret', 'password', 'example')
MIN_SECRET = 32


def secret_problems():
    """What is wrong with the secrets this process was started with ([] when nothing is). SECRET_KEY signs sessions and derives the keys that
    encrypt stored passwords, salt masked values and protect the demo database: a missing, short or copied value makes all of that guessable."""
    from urllib.parse import urlparse
    out = []
    key = os.environ.get('SECRET_KEY') or ''
    if not key:
        out.append('SECRET_KEY is not set')
    elif key.lower().startswith(PLACEHOLDERS) or len(key) < MIN_SECRET:
        out.append(f'SECRET_KEY is a placeholder or shorter than {MIN_SECRET} characters')
    pw = urlparse(DATABASE_URL).password or ''
    if pw.lower().startswith(PLACEHOLDERS[:2]):
        out.append('the database password (APP_DB_PASSWORD) is still the placeholder of .env.example')
    if (ADMIN_PASSWORD or '').lower().startswith(PLACEHOLDERS[:2]):
        out.append('INITIAL_ADMIN_PASSWORD is still the placeholder of .env.example')
    return out


def require_secrets():
    """Refuse to start with secrets anybody can guess. Called by the web app and the worker when they start, not on import (tools and tests import freely)."""
    problems = secret_problems()
    if problems:
        raise RuntimeError('Curltale AI will not start with these settings: ' + '; '.join(problems) + '. Set them in .env (generate values with: openssl rand -hex 32).')
