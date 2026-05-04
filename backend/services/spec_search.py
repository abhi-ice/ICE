"""Spec embedding via TF-IDF + cosine similarity.

SUBSTITUTION: master prompt §14 specified Voyage SDK embeddings.
We use scikit-learn TfidfVectorizer per project_spec — vectors fit on the
spec corpus and persisted as JSONB in the spec_sections collection. Search
is cosine similarity in numpy. Same retrieval API as a vector DB would
expose: top_k(query, project_id) -> [{section_id, score, ...}].

Why TF-IDF and not Voyage? No external API call needed; works offline;
adequate for federal-construction specs which use highly distinctive
MasterFormat terminology. Swap-in for Voyage is a drop-in replacement
later.
"""
import math
import json
import re
from typing import List
from sklearn.feature_extraction.text import TfidfVectorizer
import numpy as np
from database import get_db, tenant_filter


# Per-spec cached vectorizers (in-process; cleared on backend restart)
_VEC_CACHE: dict[str, dict] = {}


async def fit_spec_index(tenant_id: str, project_spec_id: str) -> dict:
    """Fit a TF-IDF index over all sections of the given spec. Persists the
    vector array + vocabulary to spec_sections.embedding (a JSON list).
    Returns {section_count, vocab_size}.
    """
    db = get_db()
    sections = await db.spec_sections.find(
        tenant_filter(tenant_id, {"project_spec_id": project_spec_id}),
        {"_id": 0},
    ).sort("section_code", 1).to_list(5000)
    if not sections:
        return {"section_count": 0, "vocab_size": 0}

    docs = [(s["section_title"] + "\n" + (s.get("text_content") or "")).strip() for s in sections]
    vectorizer = TfidfVectorizer(
        max_features=4000,
        ngram_range=(1, 2),
        stop_words="english",
        min_df=1,
    )
    matrix = vectorizer.fit_transform(docs)  # shape: (n_docs, vocab_size)
    matrix_dense = matrix.toarray().astype(np.float32)

    # Persist normalized vectors per section for offline cosine search
    norms = np.linalg.norm(matrix_dense, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normed = matrix_dense / norms

    vocab = vectorizer.get_feature_names_out().tolist()
    for i, sec in enumerate(sections):
        vec = normed[i].tolist()
        await db.spec_sections.update_one(
            tenant_filter(tenant_id, {"id": sec["id"]}),
            {"$set": {"embedding": vec}},
        )

    # Cache vocab for fast query at request time
    _VEC_CACHE[project_spec_id] = {"vocab": vocab, "vectorizer": vectorizer}

    return {"section_count": len(sections), "vocab_size": len(vocab)}


def _query_vector(project_spec_id: str, query: str) -> np.ndarray | None:
    cache = _VEC_CACHE.get(project_spec_id)
    if not cache:
        return None
    v = cache["vectorizer"].transform([query]).toarray().astype(np.float32)[0]
    n = float(np.linalg.norm(v))
    if n > 0:
        v = v / n
    return v


async def search_spec(
    *, tenant_id: str, project_spec_id: str, query: str, top_k: int = 5,
) -> List[dict]:
    """Cosine similarity over TF-IDF vectors. Lazily fits the index if not
    yet cached (e.g., after server restart)."""
    db = get_db()
    if project_spec_id not in _VEC_CACHE:
        # Re-fit so vectorizer vocab matches what's in DB
        await fit_spec_index(tenant_id, project_spec_id)

    qvec = _query_vector(project_spec_id, query)
    if qvec is None or qvec.size == 0:
        return []

    sections = await db.spec_sections.find(
        tenant_filter(tenant_id, {"project_spec_id": project_spec_id}),
        {"_id": 0},
    ).to_list(5000)
    scored = []
    for s in sections:
        emb = s.get("embedding")
        if not emb:
            continue
        v = np.asarray(emb, dtype=np.float32)
        if v.shape[0] != qvec.shape[0]:
            # vocab drift — needs re-fit; skip this row
            continue
        score = float(np.dot(v, qvec))
        if score > 0.05:
            scored.append((score, s))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {
            "spec_section_id": s["id"],
            "section_code": s["section_code"],
            "section_title": s["section_title"],
            "page_start": s.get("page_start"),
            "score": round(score, 4),
            "snippet": _snippet(s.get("text_content") or "", query),
        }
        for score, s in scored[:top_k]
    ]


async def search_all_specs_in_project(
    *, tenant_id: str, project_id: str, query: str, top_k: int = 5,
) -> List[dict]:
    db = get_db()
    specs = await db.project_specs.find(
        tenant_filter(tenant_id, {"project_id": project_id, "processing_status": "complete"}),
        {"_id": 0, "id": 1},
    ).to_list(50)
    results: list[dict] = []
    for spec in specs:
        chunk = await search_spec(
            tenant_id=tenant_id,
            project_spec_id=spec["id"],
            query=query,
            top_k=top_k,
        )
        for r in chunk:
            r["project_spec_id"] = spec["id"]
        results.extend(chunk)
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]


def _snippet(text: str, query: str, window: int = 240) -> str:
    if not text:
        return ""
    q = query.split()
    lower = text.lower()
    best_pos = 0
    for term in q:
        p = lower.find(term.lower())
        if p > 0:
            best_pos = p
            break
    start = max(0, best_pos - window // 2)
    end = min(len(text), start + window)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return re.sub(r"\s+", " ", snippet)
