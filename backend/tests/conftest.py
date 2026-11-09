import json, os, sys
from pathlib import Path
import psycopg, pytest

REPO = Path(__file__).resolve().parents[2]                        # the repository root (holds .env on a development machine)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tempfile
FILES = tempfile.mkdtemp(prefix='ss-files-')
os.environ['CONNECTOR_FILES_DIR'] = FILES
os.environ['DATA_DIR'] = FILES                                     # the Library keeps its files in FILES/library
os.environ['SERVICE_ROLE'] = 'api'                                  # tests run jobs themselves, no background workers
for _k, _v in (('RATE_PER_S', '100000'), ('RATE_BURST', '100000'), ('LOGIN_PER_MIN', '100000'), ('ALFRED_PER_MIN', '100000'), ('INGEST_PER_MIN', '100000')):
    os.environ.setdefault(_k, _v)                                 # the suite is one fast client: the limits get their own tests
os.environ['INITIAL_ADMIN_USERNAME'] = 'admin'                    # the suite's own first admin, whatever a local .env says
os.environ['INITIAL_ADMIN_PASSWORD'] = 'test-admin-password-12345'
os.environ.setdefault('EMBEDDING_PROVIDER', 'openrouter')        # no embedding model is downloaded: tests that need vectors fake them
os.environ.setdefault('EMBEDDING_DIM', '768')
os.environ['TRUSTED_PROXIES'] = '127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16'   # the product default, whatever this server's .env says            # file databases (SQLite, DuckDB) may only live here, as in the app


def _admin_url():
    """A PostgreSQL superuser URL on a server with pgvector: TEST_DATABASE_URL, or the dev database of .env on 127.0.0.1:5433.
    The suite creates and drops its own throwaway databases there; it never touches the database the URL names."""
    if os.environ.get('TEST_DATABASE_URL'):
        return os.environ['TEST_DATABASE_URL']
    env = REPO / '.env'
    for l in (env.read_text().splitlines() if env.exists() else []):
        if l.startswith('DB_PASSWORD='):
            return f"postgresql://studio:{l.split('=', 1)[1]}@127.0.0.1:5433/studio"
    raise RuntimeError('set TEST_DATABASE_URL to a PostgreSQL superuser URL on a server with pgvector (see README, Development)')


# Point everything at a throwaway database BEFORE any studio module is imported (test modules import studio at collection time).
_ADMIN = _admin_url()
_TEST_URL = _ADMIN.rsplit('/', 1)[0] + '/studio_test'
with psycopg.connect(_ADMIN, autocommit=True) as _c:
    _c.execute('DROP DATABASE IF EXISTS studio_test WITH (FORCE)')
    _c.execute('CREATE DATABASE studio_test')
os.environ['DATABASE_URL'] = _TEST_URL
os.environ['DATA_DB_NAME'] = 'semantic_data_test'   # never touch the real synthetic database
os.environ['OPENROUTER_API_KEY'] = ''   # tests never call the network (keyword search only)


def fixture_model():
    """The fictional test model (fixtures/sample_model.py), read through the product's own Excel format as an upload would be."""
    from studio import excel_io
    sys.path.insert(0, str(Path(__file__).resolve().parent / 'fixtures'))
    from sample_model import MODEL
    model, rep = excel_io.read_workbook(excel_io.build_workbook(MODEL))
    assert not rep.errors, rep.errors[:3]
    return model


@pytest.fixture(scope='session')
def dbmod():
    """Throwaway database with the fixture model published, as a user would after uploading it."""
    from studio import config, db, ontology, indexer
    assert config.DATABASE_URL == _TEST_URL, 'tests must never touch the dev database'
    db.init_schema()
    vid = ontology.import_model(fixture_model(), '1.0', publish=True)
    indexer.reindex(vid, embed=False)
    yield db
    db.pool().close()
    with psycopg.connect(_ADMIN, autocommit=True) as c:
        c.execute('DROP DATABASE IF EXISTS studio_test WITH (FORCE)')


@pytest.fixture(scope='session')
def datadb(dbmod):
    """A small synthetic database in its own throwaway database, built from the test ontology."""
    from studio import config, datastore, ontology
    from studio.synth import load
    assert config.DATA_DB == 'semantic_data_test'
    onto = ontology.assemble(ontology.published_id())
    res = load.build(onto, seed=3, scale='small')
    datastore.reset_pools()
    yield {'onto': onto, 'result': res, 'cat': datastore.Catalog(onto)}
    datastore.reset_pools()
    with psycopg.connect(_ADMIN, autocommit=True) as c:
        c.execute('DROP DATABASE IF EXISTS semantic_data_test WITH (FORCE)')


@pytest.fixture(scope='session')
def client(dbmod):
    """One app (and one MCP session manager, which can only start once per process) for the whole run."""
    from fastapi.testclient import TestClient
    from studio.api import app
    with TestClient(app) as c:
        yield c
