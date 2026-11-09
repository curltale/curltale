"""Does the agent get multi-entity data questions right? python -m devtools.agenteval [runs], from backend/ with the test model (tests/fixtures/sample_model.py) published   (calls the chat model: costs a little, needs OPENROUTER_API_KEY)

Each question has an independently written SQL as ground truth; Alfred answers through its tools and the SQL it ran is compared with the truth.
Run it after any change to the planner, the tool descriptions or the prompt, and when switching the chat model."""
import sys
import asyncio, time, statistics, sys
from studio import alfred, access, datastore
RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
ent = access.normalize_user({})
QS = {
 'A smb unpaid+complaints': ("For each SMB customer, show the number of active subscriptions, open complaints and the total unpaid invoice amount. Give me the top 5 by unpaid amount.",
   """SELECT c.customer_name, coalesce(s.n,0) AS subscriptions, coalesce(o.n,0) AS complaints, coalesce(u.amt,0) AS unpaid FROM crm.customer c
      LEFT JOIN (SELECT customer_id, count(*) n FROM sales.subscription WHERE subscription_status='Active' GROUP BY 1) s ON s.customer_id=c.id
      LEFT JOIN (SELECT customer_id, count(*) n FROM support.complaint WHERE closed_at IS NULL AND resolved_at IS NULL GROUP BY 1) o ON o.customer_id=c.id
      LEFT JOIN (SELECT b.customer_id, sum(i.amount_due) amt FROM billing.invoice i JOIN billing.billing_account b ON b.id=i.billing_account_id WHERE i.amount_due>0 GROUP BY 1) u ON u.customer_id=c.id
      WHERE c.segment='SMB' ORDER BY unpaid DESC LIMIT 5"""),
 'B billing accounts subs+unpaid': ("Which billing accounts have more than 1 active subscription and at least one unpaid invoice? Show customer name, billing account id, active subscriptions and unpaid invoices.",
   """SELECT c.customer_name, b.id AS billing_account, s.n AS subscriptions, u.n AS unpaid FROM billing.billing_account b JOIN crm.customer c ON c.id=b.customer_id
      JOIN (SELECT billing_account_id, count(*) n FROM sales.subscription WHERE subscription_status='Active' GROUP BY 1) s ON s.billing_account_id=b.id
      JOIN (SELECT billing_account_id, count(*) n FROM billing.invoice WHERE amount_due>0 GROUP BY 1) u ON u.billing_account_id=b.id
      WHERE s.n>1 AND u.n>=1"""),
 'C monthly fee + open incidents': ("For each SMB customer show the total monthly fee of its active subscriptions and the number of open incidents. Top 5 by monthly fee.",
   """SELECT c.customer_name, coalesce(f.m,0) AS monthly_fee, coalesce(i.n,0) AS incidents FROM crm.customer c
      LEFT JOIN (SELECT s.customer_id, sum(p.monthly_fee) m FROM sales.subscription_price p JOIN sales.subscription s ON s.id=p.subscription_id WHERE s.subscription_status='Active' AND (p.valid_to IS NULL OR p.valid_to>now()) GROUP BY 1) f ON f.customer_id=c.id
      LEFT JOIN (SELECT customer_id, count(*) n FROM support.incident WHERE closed_at IS NULL AND resolved_at IS NULL GROUP BY 1) i ON i.customer_id=c.id
      WHERE c.segment='SMB' ORDER BY monthly_fee DESC LIMIT 5"""),
}
num = lambda v: isinstance(v, (int, float)) or hasattr(v, 'quantize')
def matches(got, truth):
    """every row of the truth appears in the answer (all its values in one row), same number of rows"""
    if len(got) != len(truth): return False
    for t in truth:
        tv = [round(float(v), 0) if num(v) else v for v in t.values()]
        if not any(all(v in [round(float(x), 0) if num(x) else x for x in g.values()] for v in tv) for g in got): return False
    return True
async def run(q):
    t = time.time(); calls = []; sql = None
    async for ev in alfred.chat([{'role': 'user', 'content': q}], None, 'bench', ent):
        if ev['type'] == 'tool':
            calls.append(ev['name']); sql = ev['args'].get('sql') if ev['name'] == 'query_data' else sql
    return time.time() - t, calls, sql
async def main():
    total = good = 0
    for name, (q, truth_sql) in QS.items():
        truth = datastore.console(truth_sql, ent)['rows']; ok = 0; times = []; nq = []; ncalls = []
        for i in range(RUNS):
            dt, calls, sql = await run(q); correct = False
            if sql:
                try: correct = matches(datastore.console(sql, ent)['rows'], truth)
                except Exception: pass
            ok += correct; times.append(dt); nq.append(calls.count('query_data')); ncalls.append(len(calls))
            print(f'  {name[:24]:24} run {i+1}: {dt:3.0f}s {len(calls)} calls, {calls.count("query_data")} query_data -> {"CORRECT" if correct else "WRONG"}', flush=True)
        total += RUNS; good += ok
        print(f'{name}: {ok}/{RUNS} correct | avg {statistics.mean(times):.0f}s | avg tool calls {statistics.mean(ncalls):.1f} | avg query_data {statistics.mean(nq):.1f}\n')
    print(f'OVERALL {good}/{total} correct')
if __name__ == "__main__":
    asyncio.run(main())
