"""Tenant-scoped TF-IDF index for the Knowledge Base. Master prompt §6.

When the tenant's KB is mutated we mark the index dirty, then rebuild on
next read. Lock-free — concurrent rebuilds just race to a deterministic
result.
"""
from __future__ import annotations
from threading import Lock
from typing import Dict
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np

from database import get_db, tenant_filter

_caches: Dict[str, dict] = {}
_lock = Lock()


def invalidate_index(tenant_id: str) -> None:
    with _lock:
        _caches.pop(tenant_id, None)


async def _rebuild(tenant_id: str) -> dict:
    db = get_db()
    rows = await db.kb_entries.find(
        tenant_filter(tenant_id), {"_id": 0},
    ).to_list(2000)
    if not rows:
        return {"vectorizer": None, "matrix": None, "rows": []}
    corpus = [(r.get("title", "") + " \n" + r.get("body", "")) for r in rows]
    vect = TfidfVectorizer(stop_words="english", ngram_range=(1, 2),
                           max_features=8000, min_df=1)
    matrix = vect.fit_transform(corpus)
    return {"vectorizer": vect, "matrix": matrix, "rows": rows}


async def search_kb(tenant_id: str, query: str, limit: int = 10) -> list[dict]:
    cache = _caches.get(tenant_id)
    if cache is None:
        cache = await _rebuild(tenant_id)
        with _lock:
            _caches[tenant_id] = cache
    vect = cache["vectorizer"]
    if vect is None or not query.strip():
        return []
    qv = vect.transform([query])
    sims = cosine_similarity(qv, cache["matrix"]).ravel()
    if not sims.size:
        return []
    idx = np.argsort(-sims)[:limit]
    out: list[dict] = []
    for i in idx:
        if sims[i] <= 0.01:
            continue
        r = cache["rows"][int(i)]
        out.append({**r, "_score": round(float(sims[i]), 4)})
    return out
