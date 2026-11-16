"""Which key the AI features use.

Every AI feature (daily report helpers, photo review, schedule review,
copilot, and so on) asks this module for its key instead of reading an
environment variable itself, so there is one place that decides.

Order:
  1. ANTHROPIC_API_KEY  -- the owner's own Anthropic key. Used on Railway
                           and on Emergent today. Calls go straight to
                           api.anthropic.com through services.anthropic_compat.
  2. EMERGENT_LLM_KEY   -- Emergent's gateway key. Only used when the
                           Anthropic key is missing, so the app still runs
                           unchanged if this branch is deployed on Emergent.

The keys are read every time (not once at import), so a changed
environment variable is picked up after a restart without code changes.
"""
from __future__ import annotations

import os
from typing import Optional


def anthropic_api_key() -> Optional[str]:
    """The owner's own Anthropic key, or None when it is not set."""
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip() or None


def emergent_llm_key() -> Optional[str]:
    """Emergent's gateway key, or None when it is not set."""
    return (os.environ.get("EMERGENT_LLM_KEY") or "").strip() or None


def llm_api_key() -> Optional[str]:
    """The key the AI features should use: ANTHROPIC_API_KEY first,
    then EMERGENT_LLM_KEY, else None."""
    return anthropic_api_key() or emergent_llm_key()


def has_llm_key() -> bool:
    """True when at least one AI key is configured."""
    return llm_api_key() is not None


__all__ = ["anthropic_api_key", "emergent_llm_key", "llm_api_key", "has_llm_key"]
