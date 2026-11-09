"""Retrieval check: python -m devtools.evalcli [--no-vectors] [--k 10] [--verbose], from backend/
Runs tests/retrieval_cases.yaml (written for the test model, tests/fixtures/sample_model.py) against the published version and reports hit@k and MRR."""
import argparse
import fnmatch
from pathlib import Path
import yaml
from studio import search
from studio.ontology import published_id


def run(k=10, vectors=True, verbose=False):
    cases = yaml.safe_load((Path(__file__).resolve().parents[1] / 'tests' / 'retrieval_cases.yaml').read_text())
    vid = published_id()
    hits, rr, misses = 0, 0.0, []
    for c in cases:
        ids = [r['id'] for r in search.search(vid, c['q'], k=k, expand=False, use_vectors=vectors)]
        rank = next((i + 1 for i, x in enumerate(ids) if any(fnmatch.fnmatchcase(x.lower(), e.lower()) for e in c['expect'])), None)
        if rank:
            hits += 1; rr += 1 / rank
        else:
            misses.append((c['q'], c['expect'], ids[:5]))
        if verbose:
            print(f"{'ok  ' if rank else 'MISS'} rank={rank}  {c['q']}")
    n = len(cases)
    print(f'hit@{k}: {hits}/{n} = {hits / n:.0%}   MRR: {rr / n:.2f}   (vectors: {vectors})')
    for q, exp, got in misses:
        print(f'  MISS {q!r}\n       wanted {exp}\n       got    {got}')
    return hits / n


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--k', type=int, default=10)
    ap.add_argument('--no-vectors', action='store_true')
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args()
    run(a.k, not a.no_vectors, a.verbose)
