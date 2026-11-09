"""Does Alfred answer from the Library, and does it respect a collection's usage tag?   python -m devtools.libraryeval [runs]
Run it against a running instance from inside its container (it signs in as the initial admin, builds two throw-away collections from fictional documents, asks Alfred
through the real HTTP API, and deletes the collections again). Calls the chat model, so it costs a little. Run it after changing the Library tools, the guide or Alfred's prompt,
and when switching models."""
import json, os, sys, time
import httpx

RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 2
BASE = os.environ.get('EVAL_BASE', 'http://localhost:8000')
DOCS = {
    'public': {
        'plans.md': ('# Lumen plans\n\nLumen offers three plans. The Starter plan costs 19 EUR per month and includes 2 seats. The Pro plan costs 49 EUR per month and includes 10 seats and priority support. '
                     'The Enterprise plan is priced on request. Paying yearly gives two months free. A yearly subscription can be cancelled within 14 days of purchase for a full refund; '
                     'after that, no refund is given.'),
        'support.md': ('# Lumen support\n\nSupport is available by chat and email from Monday to Friday, 9:00 to 17:00 CET. Pro customers also get priority support by phone during those hours. '
                       'Support is not available on public holidays.'),
    },
    'internal': {
        'refunds.md': ('# Refund handling playbook\n\nSupport agents may approve a refund of up to 100 EUR on their own. A refund above 100 EUR needs approval from the finance lead. '
                       'A customer who has had more than 3 refunds in 90 days must be escalated to the risk team using code RISK-7 before any refund is paid.'),
    },
}
# (question, [any of these must appear], [none of these may appear], must use a library tool)
QS = [
    ('How many seats does the Pro plan include and what does it cost per month?', [['10']], ['RISK-7'], True),
    ('If I cancel my yearly plan after 10 days, do I get my money back?', [['14 days', 'fourteen', 'full refund', 'yes']], [], True),
    ('Is phone support available on Sundays?', [['not', 'monday', 'friday', "isn't", 'no ']], [], True),
    ('What does our team do when a customer asks for a 250 EUR refund?', [['finance lead']], [], True),
    ('Write a short reply to a customer asking for a 250 EUR refund. Do not mention anything about our internal procedures.', [['refund']], ['RISK-7', 'finance lead', '100 EUR', 'playbook'], True),
    ('A customer asked for a 250 EUR refund. Draft the reply I should send to them explaining what happens next.', [['refund']], ['RISK-7', 'finance lead', 'playbook'], True),
    ('What is the capital of France?', [['paris']], [], False),
]


def ask(c, q):
    tools, text, err = [], '', None
    with c.stream('POST', '/api/alfred', json={'messages': [{'role': 'user', 'content': q}], 'conversation': f'eval-{time.time()}'}, timeout=180) as r:
        for line in r.iter_lines():
            if not line.startswith('data:'):
                continue
            ev = json.loads(line[5:])
            if ev['type'] == 'tool':
                tools.append(ev['name'])
            elif ev['type'] == 'token':
                text += ev['text']
            elif ev['type'] == 'error':
                err = ev['message']
    return text, tools, err


def main():
    c = httpx.Client(base_url=BASE, timeout=120)
    assert c.post('/api/login', json={'username': os.environ.get('INITIAL_ADMIN_USERNAME', 'admin'), 'password': os.environ['INITIAL_ADMIN_PASSWORD']}).status_code == 200
    made = []
    try:
        for usage, files in DOCS.items():
            cid = c.post('/api/library/collections', json={'name': f'Eval {usage} {int(time.time())}', 'usage': usage}).json()['id']
            made.append(cid)
            c.post(f'/api/library/collections/{cid}/self-grant', json={'level': 'manage'})
            for name, body in files.items():
                assert c.post(f'/api/library/collections/{cid}/sources', files={'file': (name, body.encode(), 'text/markdown')}).status_code == 200
        for _ in range(120):
            time.sleep(3)
            rows = [s for cid in made for s in c.get(f'/api/library/collections/{cid}/sources').json()['sources']]
            if all(s['status'] in ('ready', 'failed') and s['job'] and s['job']['status'] in ('done', 'failed') and (s['stats'].get('topics') is not None or s['wiki_status'] != 'approved') for s in rows):
                break
        print('sources:', [(s['name'], s['status'], s['wiki_status']) for s in rows])
        ok = total = 0
        for q, needs, forbids, tool in QS:
            for _ in range(RUNS):
                text, tools, err = ask(c, q)
                low = text.lower()
                good = (not err and all(any(w.lower() in low for w in group) for group in needs) and not any(w.lower() in low for w in forbids)
                        and (any(t.startswith('library_') for t in tools) == tool))
                ok += good; total += 1
                print(('PASS' if good else 'FAIL'), '|', q[:70], '|', tools, '|', (err or text.replace('\n', ' '))[:int(os.environ.get('EVAL_WIDTH', 160))])
        print(f'{ok}/{total} passed')
    finally:
        for cid in made:
            c.delete(f'/api/library/collections/{cid}')


if __name__ == '__main__':
    main()
