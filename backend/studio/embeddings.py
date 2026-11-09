"""Embeddings: a local ONNX model (default) or OpenRouter's OpenAI-compatible /embeddings."""
import threading
import time
import httpx
from . import config, llm, settings

_local = None
_lock = threading.Lock()
_http = httpx.Client(timeout=120)               # one connection pool: no TLS handshake per call


def available():
    return config.EMBEDDING_PROVIDER == 'local' or llm.available()


def _embed_local(texts):
    global _local
    with _lock:
        if _local is None:
            from fastembed import TextEmbedding
            _local = TextEmbedding(config.EMBEDDING_MODEL, cache_dir=config.MODEL_CACHE)
        model = _local
    vecs = [v.tolist() for v in model.embed(texts, batch_size=8)]           # small batches keep memory flat
    if any(len(v) != config.EMBEDDING_DIM for v in vecs):
        raise RuntimeError(f'unexpected embedding shape (expected dim {config.EMBEDDING_DIM}, got {len(vecs[0]) if vecs else 0})')
    return vecs


def embed(texts, retries=3):
    """texts -> list of vectors. Raises RuntimeError without an API key."""
    if config.EMBEDDING_PROVIDER == 'local':
        return _embed_local(texts)
    if not available():
        raise RuntimeError('the AI endpoint is not configured (Settings, Alfred and AI)')
    headers = llm.headers()
    last = None
    for attempt in range(retries):
        try:
            r = _http.post(f'{llm.base_url()}/embeddings', headers=headers,
                           json={'model': config.EMBEDDING_MODEL, 'input': texts})
            r.raise_for_status()
            data = sorted(r.json()['data'], key=lambda d: d['index'])
            vecs = [d['embedding'] for d in data]
            if len(vecs) != len(texts) or any(len(v) != config.EMBEDDING_DIM for v in vecs):
                raise RuntimeError(f'unexpected embedding shape (expected dim {config.EMBEDDING_DIM}, got {len(vecs[0]) if vecs else 0})')
            return vecs
        except (httpx.HTTPError, KeyError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f'embedding request failed: {last}')


def embed_query(q):
    """One search question -> vector (with the model's query instruction, if it has one)."""
    return embed([config.EMBEDDING_QUERY_PREFIX + q])[0]


def vec_literal(v):
    return '[' + ','.join(f'{x:.7f}' for x in v) + ']'
