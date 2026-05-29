"""Optional neural embeddings (pt.77) — env-gated, with TF-IDF fallback.

The spec / KB RAG defaults to TF-IDF (services/spec_search.py), which needs no
external API and is solid for keyword-dense MasterFormat specs. When neural
embeddings are enabled AND an OpenAI-compatible embeddings endpoint is reachable,
fit_spec_index / search_spec switch to dense neural vectors for semantically
richer retrieval. ANY failure (disabled, no endpoint, transport error, shape
mismatch) returns None so the caller transparently falls back to TF-IDF — the
copilot's spec retrieval can never break because of this.

Enable (Railway env):
  SPEC_EMBEDDINGS=neural
  EMBED_MODEL=text-embedding-3-small         # default
  EMBED_API_BASE=<OpenAI-compatible base>    # optional (e.g. the gateway)
  EMBED_API_KEY=<key>                         # falls back to EMERGENT_LLM_KEY

litellm (already a dependency) is the transport; it speaks the OpenAI embeddings
protocol against whatever base/key are configured.
"""
import os
import logging

log = logging.getLogger(__name__)


def neural_enabled() -> bool:
    """True only when explicitly switched on via env. Default: TF-IDF."""
    return os.environ.get("SPEC_EMBEDDINGS", "").strip().lower() in (
        "neural", "openai", "1", "true", "on", "yes",
    )


def embed_model() -> str:
    return os.environ.get("EMBED_MODEL", "text-embedding-3-small")


async def embed_texts(texts):
    """Return list[list[float]] aligned to `texts`, or None when neural
    embeddings are disabled/unavailable. NEVER raises — callers fall back to
    TF-IDF on None."""
    if not neural_enabled():
        return None
    items = [str(t or "") for t in (texts or [])]
    if not items:
        return None
    try:
        import litellm
        kwargs = {"model": embed_model(), "input": items}
        base = os.environ.get("EMBED_API_BASE")
        key = os.environ.get("EMBED_API_KEY") or os.environ.get("EMERGENT_LLM_KEY")
        if base:
            kwargs["api_base"] = base
        if key:
            kwargs["api_key"] = key
        resp = await litellm.aembedding(**kwargs)
        data = resp["data"] if isinstance(resp, dict) else getattr(resp, "data", None)
        if not data:
            return None
        out = []
        for d in data:
            emb = d["embedding"] if isinstance(d, dict) else getattr(d, "embedding", None)
            if emb is None:
                return None
            out.append(list(emb))
        if len(out) != len(items):
            return None
        return out
    except Exception as e:  # disabled provider, no endpoint, network, etc.
        log.warning("neural embeddings unavailable (%s); using TF-IDF fallback", type(e).__name__)
        return None
