"""Runs LAST (file name): it publishes a new version into the shared test database, which other tests must not see."""
import json, os
from studio import auth, ontology as O
from test_access import _rpc, mcp_call


def test_published_example_edits_reach_the_agents(client):
    """Agents read the published model: a modeller's edit to a query example must appear after publishing, and not before."""
    tok = auth.create_api_token('ctx-check4', {})
    assert client.post('/api/login', json={'username': 'admin', 'password': os.environ['INITIAL_ADMIN_PASSWORD']}).status_code == 200
    assert client.post('/api/versions/draft', json={'label': 'example-edit'}).status_code == 200
    ex = next(r for r in client.get('/api/examples?version=draft').json() if r['id'] == 'EX01')
    marker = 'ZZ-MARKER-FOR-AGENTS'
    assert client.put('/api/examples/EX01', json={'data': {**ex, 'body': ex['body'] + ' ' + marker}}).status_code == 200
    seen = lambda: json.dumps(mcp_call(client, tok, 'describe', {'name': 'customer'}))
    assert marker not in seen()                                       # drafts are invisible to agents
    assert client.post('/api/versions/publish').status_code == 200                    # starts the background publish
    import time
    for _ in range(300):
        job = client.get('/api/versions/publish/status').json()
        if job['state'] != 'running':
            break
        time.sleep(0.1)
    assert job['state'] == 'done', job
    assert marker in seen()                                           # ...and they see it as soon as it is published, without a restart
