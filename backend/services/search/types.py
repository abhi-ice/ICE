"""The contract every search chunker follows.

A *chunker* turns one source document into zero or more `Chunk`s. It never
calls Voyage and never writes to the database: the worker
(services.search.worker) does that, compares content hashes, and embeds only
pieces whose text or filter metadata changed.

How writes reach the index: the change-stream listener
(services.search.listener) sees every insert / update / replace / delete on
an indexed collection and queues "<collection>:<_id>". So chunkers never need
hooks in routers. A chunker may also ask for *other* records to be re-queued
(`requeue`), e.g. a daily_presence row asks for its parent report's manpower
roll-up.

Rules for chunk text (enforced by tests/test_search_registry.py):
  * never include emails, phone numbers, pay or cost rates, money amounts
    from cost fields, GPS coordinates, storage keys / URLs, tokens, password
    hashes or attendance person names;
  * the header is short and plain: "<project> | <what> <date> | ..." so the
    vector itself carries project, date and activity;
  * deleted / soft-deleted / dev-stub / orphan rows return no chunks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Optional

Decision = Literal["index", "query_only", "exclude"]


@dataclass
class Chunk:
    kind: str                        # e.g. "daily_report", "rfi", "spec"
    header: str                      # short context line, part of the embedded text
    body: str                        # the record's own words
    date: Optional[str] = None       # ISO "YYYY-MM-DD" (report date, record date)
    activity_code: list[str] = field(default_factory=list)
    trade_code: list[str] = field(default_factory=list)
    status: Optional[str] = None
    parent_id: Optional[str] = None  # e.g. the daily report id for an activity row
    private: bool = False            # only owner_user_id may see it
    owner_user_id: Optional[str] = None
    allowed_roles: Optional[list[str]] = None  # None = every role that can see the project
    title: Optional[str] = None      # short label for citations ("RFI #12 - Stair rail")
    url: Optional[str] = None        # app path that opens the record, e.g. "/rfis?openId=..."

    @property
    def text(self) -> str:
        return f"{self.header}\n{self.body}".strip()


@dataclass
class ChunkResult:
    tenant_id: Optional[str]
    project_id: Optional[str]        # None = company-level record
    source_id: Optional[str]         # the app-level id (doc["id"]) when there is one
    chunks: list[Chunk] = field(default_factory=list)
    # Other records to re-queue: (collection, filter) e.g. ("daily_reports", {"id": rid})
    requeue: list[tuple[str, dict]] = field(default_factory=list)


class ChunkContext:
    """Per-batch helper with small caches so chunkers don't repeat lookups."""

    def __init__(self, db: Any):
        self.db = db
        self._cache: dict[tuple, Any] = {}

    async def one(self, collection: str, filt: dict, projection: Optional[dict] = None) -> Optional[dict]:
        key = (collection, tuple(sorted((k, str(v)) for k, v in filt.items())),
               tuple(sorted((projection or {}).items())))
        if key not in self._cache:
            proj = dict(projection or {})
            if proj:
                proj.setdefault("_id", 0)
            self._cache[key] = await self.db[collection].find_one(filt, proj or None)
        return self._cache[key]

    async def project_name(self, project_id: Optional[str]) -> str:
        if not project_id:
            return ""
        p = await self.one("projects", {"id": project_id}, {"name": 1, "number": 1})
        if not p:
            return ""
        return (p.get("name") or p.get("number") or "").strip()

    async def user_name(self, user_id: Optional[str]) -> str:
        if not user_id:
            return ""
        u = await self.one("users", {"id": user_id}, {"name": 1, "full_name": 1})
        if not u:
            return ""
        return (u.get("name") or u.get("full_name") or "").strip()


Chunker = Callable[[ChunkContext, dict], Awaitable[Optional[ChunkResult]]]


@dataclass
class Entry:
    collection: str
    decision: Decision
    reason: str
    chunker: Optional[Chunker] = None


def clean(*parts: Any, sep: str = " ") -> str:
    """Join non-empty string parts, collapsing whitespace."""
    out = []
    for p in parts:
        if p is None:
            continue
        s = " ".join(str(p).split())
        if s:
            out.append(s)
    return sep.join(out)


def iso_date(v: Any) -> Optional[str]:
    """Best-effort "YYYY-MM-DD" from a date / datetime / ISO string."""
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()[:10]
    s = str(v).strip()
    return s[:10] if len(s) >= 10 and s[4] == "-" and s[7] == "-" else None
