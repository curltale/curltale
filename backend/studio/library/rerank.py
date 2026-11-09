"""Optional reranking of search candidates by a Cohere-style /rerank endpoint (Settings > Library). Used only for the Library's search for agents."""
import httpx
from .. import settings


def available():
    return bool(settings.get('RERANK_URL'))


def rerank(query, docs, top_n=None):
    """-> indexes of docs, best first. Any failure returns None, and the caller keeps its own order."""
    if not available() or len(docs) < 2:
        return None
    key = settings.get('RERANK_API_KEY')
    try:
        r = httpx.post(settings.get('RERANK_URL'), timeout=20, headers={**({'Authorization': f'Bearer {key}'} if key else {}), 'Content-Type': 'application/json'},
                       json={'model': settings.get('RERANK_MODEL') or None, 'query': query, 'documents': docs, 'top_n': top_n or len(docs)})
        r.raise_for_status()
        res = r.json().get('results') or r.json().get('data') or []
        order = [x['index'] for x in sorted(res, key=lambda x: -x.get('relevance_score', x.get('score', 0)))]
        return order if order else None
    except Exception:
        return None
