"""Small async client for Voyage AI: embeddings and reranking.

Keys come from services.llm_keys (VOYAGE_API_KEY / VOYAGE_API_BASE). Keys
made in MongoDB Atlas use https://ai.mongodb.com/v1, keys made on
voyageai.com use https://api.voyageai.com/v1; the request shape is the same.

Only text we chose to index is ever sent (see services.search.registry).
The key is never logged.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional, Sequence

import httpx

from services.llm_keys import voyage_api_base, voyage_api_key

logger = logging.getLogger(__name__)

EMBED_MODEL = os.environ.get("VOYAGE_EMBED_MODEL", "voyage-4")
EMBED_DIMS = 1024
RERANK_MODEL = os.environ.get("VOYAGE_RERANK_MODEL", "rerank-3-lite")

# Voyage accepts up to 1,000 inputs per call; we stay well under it and under
# the per-call token limit by batching on both count and characters.
MAX_BATCH = 128
MAX_BATCH_CHARS = 200_000
_RETRY_STATUS = {429, 500, 502, 503, 504}


class VoyageUnavailable(RuntimeError):
    """No key configured, or Voyage kept failing after retries."""

    def __init__(self, message: str, status_code: "Optional[int]" = None):
        super().__init__(message)
        self.status_code = status_code


class VoyageRejected(VoyageUnavailable):
    """Voyage refused the request itself (HTTP 4xx other than 429): retrying
    the same input will not help."""


async def _post(path: str, payload: dict, *, timeout: float = 60.0,
                client: Optional[httpx.AsyncClient] = None, attempts: int = 6) -> dict:
    """attempts=6 for background indexing (patient); searches pass a short
    timeout and 2 attempts so a Voyage outage falls back to keyword search
    within about two seconds."""
    key = voyage_api_key()
    if not key:
        raise VoyageUnavailable("VOYAGE_API_KEY is not set")
    url = f"{voyage_api_base()}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    own = client is None
    c = client or httpx.AsyncClient(timeout=timeout)
    try:
        delay = 1.0
        last = max(1, attempts) - 1
        for attempt in range(last + 1):
            try:
                r = await c.post(url, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == last:
                    raise VoyageUnavailable(f"Voyage request failed: {type(exc).__name__}") from None
            else:
                if r.status_code == 200:
                    return r.json()
                if r.status_code not in _RETRY_STATUS:
                    cls = VoyageRejected if 400 <= r.status_code < 500 else VoyageUnavailable
                    raise cls(f"Voyage returned HTTP {r.status_code}", r.status_code)
                if attempt == last:
                    raise VoyageUnavailable(f"Voyage returned HTTP {r.status_code}", r.status_code)
                ra = r.headers.get("retry-after")
                if ra:
                    try:
                        delay = max(delay, float(ra))
                    except ValueError:
                        pass
            await asyncio.sleep(min(delay, 30.0))
            delay *= 2
    finally:
        if own:
            await c.aclose()
    raise VoyageUnavailable("unreachable")  # pragma: no cover


def _batches(texts: Sequence[str]):
    batch, chars = [], 0
    for i, t in enumerate(texts):
        if batch and (len(batch) >= MAX_BATCH or chars + len(t) > MAX_BATCH_CHARS):
            yield batch
            batch, chars = [], 0
        batch.append(i)
        chars += len(t)
    if batch:
        yield batch


# A search waits for at most one quick try, so an outage falls back to
# keyword search fast; background indexing keeps the patient retries.
FAST_EMBED_TIMEOUT = 2.5
FAST_RERANK_TIMEOUT = 3.0


async def embed(texts: Sequence[str], *, input_type: str = "document",
                client: Optional[httpx.AsyncClient] = None,
                fast: bool = False) -> tuple[list[list[float]], int]:
    """Embed texts in order. input_type is "document" for indexed pieces and
    "query" for search questions. Returns (vectors, total_tokens)."""
    if input_type not in ("document", "query"):
        raise ValueError("input_type must be 'document' or 'query'")
    out: list[Optional[list[float]]] = [None] * len(texts)
    tokens = 0
    for idx in _batches(texts):
        data = await _post("embeddings", {
            "input": [texts[i] for i in idx],
            "model": EMBED_MODEL,
            "input_type": input_type,
            "output_dimension": EMBED_DIMS,
        }, client=client, **({"timeout": FAST_EMBED_TIMEOUT, "attempts": 1} if fast else {}))
        rows = sorted(data.get("data") or [], key=lambda d: d.get("index", 0))
        if len(rows) != len(idx):
            raise VoyageUnavailable("Voyage returned the wrong number of vectors")
        for i, row in zip(idx, rows):
            out[i] = row["embedding"]
        tokens += int((data.get("usage") or {}).get("total_tokens") or 0)
    return [v for v in out], tokens  # type: ignore[misc]


async def rerank(query: str, documents: Sequence[str], *, top_k: Optional[int] = None,
                 client: Optional[httpx.AsyncClient] = None,
                 fast: bool = False) -> list[tuple[int, float]]:
    """Rerank documents for a query. Returns [(input_index, score)] best first."""
    if not documents:
        return []
    payload: dict = {"query": query, "documents": list(documents), "model": RERANK_MODEL, "truncation": True}
    if top_k:
        payload["top_k"] = int(top_k)
    data = await _post("rerank", payload, client=client,
                       **({"timeout": FAST_RERANK_TIMEOUT, "attempts": 1} if fast else {"timeout": 30.0}))
    rows = data.get("data") or data.get("results") or []
    res = [(int(r["index"]), float(r["relevance_score"])) for r in rows]
    res.sort(key=lambda x: -x[1])
    return res
