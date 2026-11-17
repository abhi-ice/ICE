"""Search everything: the query side.

search_everything() answers one search for one signed-in person:

  1. Build the permission filter from the request context (never from the
     question): own company always; the person's projects when per-project
     access applies, plus company-level records; roles and private records;
     recovery reports only on projects the person may open.
  2. Turn the question into a vector with Voyage (input_type "query"),
     unless the outside-provider gate says no (a CUI project, a company or
     project with AI switched off) or Voyage is unavailable. Then only the
     keyword search runs, and the answer says so in plain words.
  3. Run the vector search ($vectorSearch) and the keyword search ($search)
     at the same time and merge the two lists with reciprocal rank fusion
     (k = 60). The cluster is MongoDB 8.0, which has no $rankFusion stage,
     so the merge happens here in Python.
  4. Rerank the top 30 with Voyage rerank. If that fails, keep the merged
     order.
  5. Collapse several pieces of the same record into one result, then load
     each source record again and drop anything that is gone (missing,
     deleted, archived, made-up dev test results) or that the person could
     not open today. A stale index entry can therefore never show more than
     the record's own page would.

The two database searches go through _run_vector_stage / _run_text_stage so
tests can swap them for stubs (a plain mongod cannot run either stage).

Behind services.feature_flags.search_everything_enabled() (default OFF).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Awaitable, Callable, Optional

from services.search.store import SEARCH_CHUNKS, TEXT_INDEX_NAME, VECTOR_INDEX_NAME

logger = logging.getLogger(__name__)

RRF_K = 60
VECTOR_LIMIT = 40
VECTOR_NUM_CANDIDATES = 400
TEXT_LIMIT = 40
RERANK_TOP = 30
EXCERPT_CHARS = 400
MAX_LIMIT = 25
EMBED_TIMEOUT_S = 5.0
RERANK_TIMEOUT_S = 5.0
MAX_QUERY_CHARS = 500
RERANK_DOC_CHARS = 4000
LABEL_CHARS = 160

RECOVERY_COLLECTION = "recovery_reports"

NOTICE_OFF = "Search is turned off."
NOTICE_EMPTY = "Type something to search for."
NOTICE_NO_PROJECT = "Project not found"
NOTICE_GATED = ("Records for this project are not sent to the outside search "
                "service, so only exact word matches were searched.")
NOTICE_NO_VECTOR = ("Meaning-based search is not available right now, so only "
                    "exact word matches were searched.")
NOTICE_UNAVAILABLE = "Search is not available right now. Try again later."

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KIND_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-\.]*")

# Fields read back from search_chunks (never the vector itself).
_CHUNK_FIELDS = {
    "_id": 1, "tenant_id": 1, "project_id": 1, "kind": 1, "source_collection": 1,
    "source_oid": 1, "source_id": 1, "chunk_no": 1, "parent_id": 1, "date": 1,
    "activity_code": 1, "status": 1, "private": 1, "owner_user_id": 1,
    "allowed_roles": 1, "title": 1, "url": 1, "header": 1, "body": 1, "text": 1,
    "embedded": 1,
}

Embedder = Callable[..., Awaitable[tuple[list, int]]]
Reranker = Callable[..., Awaitable[list]]


# ---------------------------------------------------------------- who is asking

def _role(ctx: dict) -> str:
    return (((ctx or {}).get("user") or {}).get("role") or (ctx or {}).get("role") or "").strip().lower()


def _user_id(ctx: dict) -> Optional[str]:
    return ((ctx or {}).get("user") or {}).get("id")


class Scope:
    """Everything the permission filter needs, worked out once per search."""

    def __init__(self, *, tenant_id: str, role: str, user_id: Optional[str],
                 full_admin: bool, project_ids: Optional[list],
                 include_company_level: bool, recovery_project_ids: Optional[list],
                 kinds: Optional[list] = None, date_from: Optional[str] = None,
                 date_to: Optional[str] = None):
        self.tenant_id = tenant_id
        self.role = role
        self.user_id = user_id
        self.full_admin = full_admin
        # None = no project limit (every project in the company).
        self.project_ids = project_ids
        self.include_company_level = include_company_level
        # None = every project; otherwise recovery reports only on these.
        self.recovery_project_ids = recovery_project_ids
        self.kinds = kinds or None
        self.date_from = date_from
        self.date_to = date_to

    def as_dict(self) -> dict:
        return dict(self.__dict__)


async def build_scope(ctx: dict, *, project_id: Optional[str] = None,
                      kinds: Optional[list] = None, date_from: Optional[str] = None,
                      date_to: Optional[str] = None, explicit: bool = True) -> Scope:
    """Work out who may see what (design section 4.3). The company always
    comes from the context. An explicit project is checked first with
    check_project_access (404 HTTPException when refused); the caller
    decides what to do with that.

    explicit=False means the project is only the one the user happens to be
    working in (the assistant's session project), not one the question
    named. The search is still limited to that project, but company-wide
    records that belong to no project (vendors, crews, knowledge base,
    equipment) stay in, so "who is our electrical sub?" can be answered.
    Those records are still limited to the caller's company, role and
    private flags like everything else."""
    from services.project_access import (
        allowed_project_ids, check_project_access, is_enforced, is_full_admin,
    )
    tenant_id = (ctx or {}).get("tenant_id")
    if not tenant_id:
        raise ValueError("search needs a company (tenant_id) in the request context")
    role = _role(ctx)
    user_id = _user_id(ctx)
    full = is_full_admin(ctx)

    project_ids: Optional[list] = None
    include_company = True
    if project_id:
        await check_project_access(ctx, project_id, write=False, route="search")
        project_ids = [project_id]
        include_company = not explicit
    elif is_enforced(ctx):
        allowed = await allowed_project_ids(ctx)
        if allowed is not None:
            project_ids = sorted(allowed)

    # Recovery reports check project membership whatever FF_PROJECT_ACCESS
    # says, unless the caller is an all-projects admin
    # (routers/recovery_reports_router.py:45-83, has_project_access).
    recovery_ids: Optional[list] = None
    if not full:
        from auth import get_accessible_project_ids
        try:
            acc = await get_accessible_project_ids(
                tenant_id=tenant_id, user_id=user_id or "", user_role=role)
        except Exception as exc:  # a hiccup hides recovery reports, never shows them
            logger.warning("search: could not read recovery report access (%s)", type(exc).__name__)
            acc = []
        recovery_ids = None if acc is None else sorted(set(acc))

    return Scope(tenant_id=str(tenant_id), role=role, user_id=user_id, full_admin=full,
                 project_ids=project_ids, include_company_level=include_company,
                 recovery_project_ids=recovery_ids, kinds=kinds,
                 date_from=date_from, date_to=date_to)


# ---------------------------------------------------------------- filters

# A tenant id no company has: the filter for "nothing may be seen".
_NO_TENANT = "__no_company__"
_MATCH_NOTHING = {"tenant_id": {"$eq": _NO_TENANT}}


def vector_filter(scope: Scope) -> dict:
    """The permission filter in the form $vectorSearch accepts (every path
    used here is a filter field of the vector index)."""
    clauses: list[dict] = [{"tenant_id": {"$eq": scope.tenant_id}}]
    # Atlas refuses an empty $in list, so empty lists are written out.
    if scope.project_ids is not None:
        in_projects = ({"project_id": {"$in": list(scope.project_ids)}}
                       if scope.project_ids else None)
        company = {"project_id": {"$eq": None}}
        if scope.include_company_level:
            clauses.append({"$or": [in_projects, company]} if in_projects else company)
        else:
            clauses.append(in_projects or _MATCH_NOTHING)
    if scope.role != "admin":
        clauses.append({"$or": [{"allowed_roles": {"$eq": None}},
                                {"allowed_roles": {"$eq": scope.role or "-"}}]})
    if not scope.full_admin:
        priv: list[dict] = [{"private": {"$eq": False}}]
        if scope.user_id:
            priv.append({"owner_user_id": {"$eq": scope.user_id}})
        clauses.append({"$or": priv} if len(priv) > 1 else priv[0])
    if scope.recovery_project_ids is not None:
        not_recovery = {"source_collection": {"$ne": RECOVERY_COLLECTION}}
        clauses.append({"$or": [not_recovery,
                                {"project_id": {"$in": list(scope.recovery_project_ids)}}]}
                       if scope.recovery_project_ids else not_recovery)
    if scope.kinds:
        clauses.append({"kind": {"$in": list(scope.kinds)}})
    if scope.date_from or scope.date_to:
        rng: dict = {}
        if scope.date_from:
            rng["$gte"] = scope.date_from
        if scope.date_to:
            rng["$lte"] = scope.date_to
        clauses.append({"date": rng})
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def _missing(path: str) -> dict:
    """Null or absent. Atlas Search indexes a stored null (equals null
    matches it), while a field that is not there at all only fails
    "exists"; both count as "no value"."""
    return {"compound": {"should": [
        {"equals": {"path": path, "value": None}},
        {"compound": {"mustNot": [{"exists": {"path": path}}]}},
    ], "minimumShouldMatch": 1}}


def _any_of(*clauses: Optional[dict]) -> dict:
    cs = [c for c in clauses if c]
    return {"compound": {"should": cs, "minimumShouldMatch": 1}}


def text_filter_clauses(scope: Scope) -> list[dict]:
    """The same permission filter as compound.filter clauses for $search."""
    out: list[dict] = [{"equals": {"path": "tenant_id", "value": scope.tenant_id}}]
    if scope.project_ids is not None:
        in_projects = ({"in": {"path": "project_id", "value": list(scope.project_ids)}}
                       if scope.project_ids else None)
        if scope.include_company_level:
            out.append(_any_of(in_projects, _missing("project_id")))
        elif in_projects:
            out.append(in_projects)
        else:  # no projects and no company-level records: match nothing
            out.append({"equals": {"path": "tenant_id", "value": "__no_company__"}})
    if scope.role != "admin":
        out.append(_any_of(_missing("allowed_roles"),
                           {"equals": {"path": "allowed_roles", "value": scope.role or "-"}}))
    if not scope.full_admin:
        out.append(_any_of(
            {"equals": {"path": "private", "value": False}},
            {"equals": {"path": "owner_user_id", "value": scope.user_id}} if scope.user_id else None,
        ))
    if scope.recovery_project_ids is not None:
        out.append(_any_of(
            {"compound": {"mustNot": [{"equals": {"path": "source_collection",
                                                  "value": RECOVERY_COLLECTION}}]}},
            {"in": {"path": "project_id", "value": list(scope.recovery_project_ids)}}
            if scope.recovery_project_ids else None,
        ))
    if scope.kinds:
        out.append({"in": {"path": "kind", "value": list(scope.kinds)}})
    if scope.date_from or scope.date_to:
        rng: dict = {"path": "date"}
        if scope.date_from:
            rng["gte"] = scope.date_from
        if scope.date_to:
            rng["lte"] = scope.date_to
        out.append({"range": rng})
    return out


def chunk_allowed(scope: Scope, c: dict) -> bool:
    """Python copy of the permission filter, applied again to every hit."""
    if c.get("tenant_id") != scope.tenant_id:
        return False
    pid = c.get("project_id")
    if scope.project_ids is not None:
        if pid is None:
            if not scope.include_company_level:
                return False
        elif pid not in scope.project_ids:
            return False
    roles = c.get("allowed_roles")
    if scope.role != "admin" and roles is not None and scope.role not in roles:
        return False
    # A private piece needs the person asking to be its known owner (no user
    # id means this check cannot tell, so the piece is hidden).
    if not scope.full_admin and c.get("private") and (
            not scope.user_id or c.get("owner_user_id") != scope.user_id):
        return False
    if (scope.recovery_project_ids is not None
            and c.get("source_collection") == RECOVERY_COLLECTION
            and pid not in scope.recovery_project_ids):
        return False
    if scope.kinds and c.get("kind") not in scope.kinds:
        return False
    d = c.get("date")
    if scope.date_from and (not d or d < scope.date_from):
        return False
    if scope.date_to and (not d or d > scope.date_to):
        return False
    return True


# ---------------------------------------------------------------- the two searches

def _words(q: str) -> list[str]:
    return [w.strip(".-") for w in _WORD_RE.findall(q or "") if w.strip(".-")]


def text_search_stage(q: str, scope: Scope) -> dict:
    words = _words(q)
    long_words = [w for w in words if len(w) > 4]
    match: list[dict] = [{"text": {"query": q, "path": ["text", "title", "header"]}}]
    if long_words:
        match.append({"text": {"query": " ".join(long_words), "path": ["text", "title", "header"],
                               "fuzzy": {"maxEdits": 1}}})
    should: list[dict] = []
    if len(words) > 1:
        should.append({"phrase": {"query": q, "path": ["text", "title", "header"],
                                  "score": {"boost": {"value": 2}}}})
    codes = sorted({w for w in words if any(ch.isdigit() for ch in w)}
                   | {w.upper() for w in words if any(ch.isdigit() for ch in w)})
    for code in codes[:10]:
        should.append({"equals": {"path": "activity_code", "value": code,
                                  "score": {"boost": {"value": 3}}}})
    compound: dict = {
        "must": [{"compound": {"should": match, "minimumShouldMatch": 1}}],
        "filter": text_filter_clauses(scope),
    }
    if should:
        compound["should"] = should
    return {"$search": {"index": TEXT_INDEX_NAME, "compound": compound}}


def vector_pipeline(vector: list, scope: Scope) -> list[dict]:
    return [
        {"$vectorSearch": {"index": VECTOR_INDEX_NAME, "path": "embedding",
                           "queryVector": vector, "numCandidates": VECTOR_NUM_CANDIDATES,
                           "limit": VECTOR_LIMIT, "filter": vector_filter(scope)}},
        {"$project": {**_CHUNK_FIELDS, "score": {"$meta": "vectorSearchScore"}}},
    ]


def text_pipeline(q: str, scope: Scope) -> list[dict]:
    return [
        text_search_stage(q, scope),
        {"$limit": TEXT_LIMIT},
        {"$project": {**_CHUNK_FIELDS, "score": {"$meta": "searchScore"}}},
    ]


async def _run_vector_stage(db, pipeline: list[dict]) -> list[dict]:
    return await db[SEARCH_CHUNKS].aggregate(pipeline).to_list(length=VECTOR_LIMIT)


async def _run_text_stage(db, pipeline: list[dict]) -> list[dict]:
    return await db[SEARCH_CHUNKS].aggregate(pipeline).to_list(length=TEXT_LIMIT)


# ---------------------------------------------------------------- merging

def rrf_fuse(*ranked_lists: list, k: int = RRF_K) -> list[tuple[Any, float]]:
    """Reciprocal rank fusion: each list adds 1 / (k + rank) for every id it
    holds (rank starts at 1). Returns [(id, score)] best first; ties keep
    the order in which ids were first seen."""
    scores: dict[Any, float] = {}
    order: dict[Any, int] = {}
    for lst in ranked_lists:
        for rank, item_id in enumerate(lst, start=1):
            if item_id not in order:
                order[item_id] = len(order)
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], order[kv[0]]))


def collapse(ranked: list[dict]) -> list[dict]:
    """Keep the best-ranked piece of each source record (input is best
    first)."""
    seen: set = set()
    out = []
    for c in ranked:
        key = (c.get("source_collection"), c.get("source_oid"))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


ProjectGate = Callable[[Optional[str]], Awaitable[bool]]


async def rerank_top(q: str, top: list[tuple[dict, float]], reranker: Reranker,
                     allows: ProjectGate) -> list[tuple[dict, float]]:
    """Reorder the merged top list with the reranker.

    A piece's text is sent only when both hold:
      * it was allowed to go to the outside service when it was indexed
        (embedded = True), and
      * its project still passes the outside-provider gate now (`allows`,
        asked once per distinct project, all at the same time). A project
        marked CUI, or with AI switched off, after indexing keeps its old
        vectors until the worker re-queues them; this stops its text going
        out in the meantime. A piece with no project is checked against the
        company switch only.
    Every other piece keeps its merged position; the sendable ones are
    reordered among the remaining slots. Raises when the reranker fails
    (the caller keeps the merged order)."""
    candidates = [j for j, (d, _) in enumerate(top) if d.get("embedded") is True]
    if len(candidates) < 2:
        return top
    pids = list(dict.fromkeys(top[j][0].get("project_id") or None for j in candidates))

    async def _safe(pid):
        try:
            return bool(await allows(pid))
        except Exception:  # cannot tell: do not send
            return False
    verdicts = dict(zip(pids, await asyncio.gather(*(_safe(p) for p in pids))))
    slots = [j for j in candidates if verdicts.get(top[j][0].get("project_id") or None)]
    if len(slots) < 2:
        return top
    texts = [(top[j][0].get("text") or top[j][0].get("body") or "")[:RERANK_DOC_CHARS] for j in slots]
    order = await asyncio.wait_for(reranker(q, texts, top_k=None), RERANK_TIMEOUT_S)
    seen: set = set()
    reordered: list[tuple[dict, float]] = []
    for idx, score in order or []:
        i = int(idx)
        if 0 <= i < len(slots) and i not in seen:
            seen.add(i)
            reordered.append((top[slots[i]][0], float(score)))
    # Anything the reranker left out keeps its merged order, after.
    reordered += [(top[slots[i]][0], 0.0) for i in range(len(slots)) if i not in seen]
    out = list(top)
    for slot, item in zip(slots, reordered):
        out[slot] = item
    return out


# ---------------------------------------------------------------- excerpts

def best_excerpt(text: str, q: str, size: int = EXCERPT_CHARS) -> str:
    """About `size` characters of `text`, around the stretch where the
    question's words appear most often."""
    s = " ".join(str(text or "").split())
    if len(s) <= size:
        return s
    low = s.lower()
    terms = {w.lower() for w in _words(q) if len(w) >= 3}
    hits: list[int] = []
    for t in terms:
        start = 0
        while True:
            i = low.find(t, start)
            if i < 0:
                break
            hits.append(i)
            start = i + 1
    hits.sort()
    best_start = 0
    if hits:
        best_count = -1
        for h in hits:
            st = max(0, min(h - size // 4, len(s) - size))
            cnt = sum(1 for x in hits if st <= x < st + size)
            if cnt > best_count:
                best_count, best_start = cnt, st
    # Leave room for the two "..." marks, then snap to word boundaries.
    start = best_start
    end = min(len(s), start + size - 2)
    if start > 0:
        sp = s.find(" ", start, start + 30)
        if sp >= 0:
            start = sp + 1
    if end < len(s):
        sp = s.rfind(" ", start, end)
        if sp > end - 30:
            end = sp
    return ("…" if start > 0 else "") + s[start:end].strip() + ("…" if end < len(s) else "")


# ---------------------------------------------------------------- rehydrate

_GONE_STATUSES = {"deleted", "archived"}


def _row_gone(row: dict) -> bool:
    if str(row.get("status") or "").strip().lower() in _GONE_STATUSES:
        return True
    if row.get("deleted_at") or row.get("archived_at") or row.get("is_deleted") is True \
            or row.get("deleted") is True or row.get("_placeholder"):
        return True
    try:
        from services.photo_ai import is_dev_stub_result
        if is_dev_stub_result(row):
            return True
    except Exception:  # photo_ai unavailable: no dev-stub rows to hide
        pass
    return False


async def rehydrate(db, ctx: dict, scope: Scope, hits: list[dict]) -> list[dict]:
    """Load the source record of every hit (one query per collection) and
    keep only hits whose record still exists in this company, is not gone,
    and passes the same project rule as the record's own list page."""
    from services.project_access import filter_rows
    from services.search.queue import oid_value

    by_coll: dict[str, list] = {}
    for h in hits:
        by_coll.setdefault(h.get("source_collection") or "", []).append(h.get("source_oid"))
    async def _load(coll: str, oids: list) -> list[dict]:
        ids = [oid_value(o) for o in oids if o]
        try:
            return await db[coll].find(
                {"_id": {"$in": ids},
                 "$or": [{"tenant_id": scope.tenant_id}, {"tenant_id": None},
                         {"tenant_id": {"$exists": False}}]}).to_list(length=None)
        except Exception as exc:  # that collection's hits are dropped
            logger.warning("search: could not load %s rows (%s)", coll, type(exc).__name__)
            return []

    # Every collection is loaded at the same time.
    colls = [c for c in by_coll if c]
    loaded = await asyncio.gather(*(_load(c, by_coll[c]) for c in colls))
    rows: dict[tuple, dict] = {}
    for coll, found in zip(colls, loaded):
        for row in found:
            rows[(coll, str(row["_id"]))] = row
    kept = []
    for h in hits:
        row = rows.get((h.get("source_collection"), str(h.get("source_oid"))))
        if row is None or _row_gone(row):
            continue
        row_tid = row.get("tenant_id")
        if row_tid is not None and row_tid != scope.tenant_id:
            continue
        pid = row.get("project_id") if isinstance(row.get("project_id"), str) and row.get("project_id") \
            else h.get("project_id")
        ok = await filter_rows(ctx, [{"project_id": pid}], include_company_level=pid is None)
        if not ok:
            continue
        if (scope.recovery_project_ids is not None
                and h.get("source_collection") == RECOVERY_COLLECTION
                and pid not in scope.recovery_project_ids):
            continue
        kept.append({**h, "project_id": pid})
    return kept


async def project_names(db, tenant_id: str, ids: list) -> dict:
    ids = sorted({i for i in ids if i})
    if not ids:
        return {}
    out = {}
    async for p in db.projects.find({"tenant_id": tenant_id, "id": {"$in": ids}},
                                    {"_id": 0, "id": 1, "name": 1, "number": 1}):
        out[p["id"]] = (p.get("name") or p.get("number") or "").strip()
    return out


# ---------------------------------------------------------------- main entry

def _empty(mode: str, notice: Optional[str]) -> dict:
    return {"count": 0, "mode": mode, "results": [], "notice": notice}


def _label(h: dict) -> str:
    """The record's title (or the first part of its header), on one line and
    at most LABEL_CHARS long, so eight results stay well inside the
    assistant's tool-result limit."""
    raw = (h.get("title") or "").strip() or (h.get("header") or "").split("|")[0].strip() \
        or (h.get("kind") or "record").replace("_", " ")
    s = " ".join(str(raw).split())
    if len(s) <= LABEL_CHARS:
        return s
    cut = s[:LABEL_CHARS - 1]
    sp = cut.rfind(" ")
    if sp > LABEL_CHARS - 30:
        cut = cut[:sp]
    return cut.rstrip() + "…"


# Every kind a chunker writes (services/search/chunkers/*).
KNOWN_KINDS = frozenset({
    "daily_report", "dr_activity", "manpower", "photo", "photo_review", "delivery",
    "safety_event", "qc_issue", "task", "schedule_import", "mitigation",
    "recovery_report", "recovery_action", "delay", "delay_ai_report", "rfi",
    "submittal", "coordination", "work_item", "change_order", "kb", "master_doc",
    "project", "vendor", "crew", "equipment", "location", "spec",
})

# Everyday names (and the names the assistant tends to use) -> real kinds.
# "Reports" means everything written in a daily report, not just its notes.
KIND_ALIASES = {
    "daily_report": ["daily_report", "dr_activity", "manpower", "photo", "photo_review"],
    "daily_reports": ["daily_report", "dr_activity", "manpower", "photo", "photo_review"],
    "report": ["daily_report", "dr_activity", "manpower", "photo", "photo_review"],
    "reports": ["daily_report", "dr_activity", "manpower", "photo", "photo_review"],
    "dr": ["daily_report", "dr_activity", "manpower", "photo", "photo_review"],
    "activity": ["dr_activity", "task"],
    "activities": ["dr_activity", "task"],
    "schedule": ["task", "schedule_import"],
    "schedule_activity": ["task"],
    "tasks": ["task"],
    "photos": ["photo", "photo_review"],
    "photo": ["photo", "photo_review"],
    "delays": ["delay", "delay_ai_report", "mitigation"],
    "delay": ["delay", "delay_ai_report", "mitigation"],
    "rfis": ["rfi"],
    "submittals": ["submittal"],
    "recovery": ["recovery_report", "recovery_action"],
    "specs": ["spec"], "specification": ["spec"], "specifications": ["spec"],
    "subcontractor": ["vendor"], "subcontractors": ["vendor"], "supplier": ["vendor"],
    "suppliers": ["vendor"], "vendors": ["vendor"],
    "people": ["manpower", "crew"], "crews": ["crew"], "workers": ["manpower", "crew"],
    "safety": ["safety_event"], "quality": ["qc_issue"], "qc": ["qc_issue"],
    "deliveries": ["delivery"], "change_orders": ["change_order"],
    "work_items": ["work_item"], "lessons": ["kb"], "knowledge": ["kb"],
}


def _clean_kinds(kinds) -> Optional[list]:
    """Turn the requested record types into real kinds. Everyday names are
    expanded (see KIND_ALIASES); names that match nothing are dropped. If
    nothing usable is left, search every type rather than return nothing."""
    if not kinds:
        return None
    if isinstance(kinds, str):
        kinds = kinds.split(",")
    out: list = []
    for k in kinds:
        k = str(k).strip().lower().replace(" ", "_").replace("-", "_")
        if not k or not _KIND_RE.match(k):
            continue
        for real in KIND_ALIASES.get(k, [k] if k in KNOWN_KINDS else []):
            if real not in out:
                out.append(real)
    return out or None


def _clean_date(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    v = str(v).strip()[:10]
    return v if _DATE_RE.match(v) else None


async def _default_embedder(texts, input_type="query"):
    from services.search import voyage_client
    return await voyage_client.embed(texts, input_type=input_type, fast=True)


async def _default_reranker(query, documents, top_k=None):
    from services.search import voyage_client
    return await voyage_client.rerank(query, documents, top_k=top_k, fast=True)


def _ms(t0: float) -> int:
    return int(round((time.perf_counter() - t0) * 1000))


async def search_everything(db, ctx: dict, q: str, *, project_id: Optional[str] = None,
                            kinds: Optional[list] = None, date_from: Optional[str] = None,
                            date_to: Optional[str] = None, limit: int = 8,
                            embedder: Optional[Embedder] = None,
                            reranker: Optional[Reranker] = None,
                            explicit: bool = True) -> dict:
    """Search every indexed record the caller may see. See the module notes.

    explicit=False: project_id is the session's project rather than one the
    question named, so company-wide records are searched too (see
    build_scope).

    Returns {"count", "mode": "hybrid"|"keyword"|"unavailable", "results",
    "notice"} (timings go to the log only); never raises for a refused or
    unknown project (the result is empty with notice "Project not found")."""
    from fastapi import HTTPException
    from services import feature_flags
    from services.search import gate
    from services.search.types import ChunkContext

    t_all = time.perf_counter()
    timings: dict[str, int] = {}
    if not feature_flags.search_everything_enabled():
        return _empty("unavailable", NOTICE_OFF)
    q = " ".join(str(q or "").split())[:MAX_QUERY_CHARS]
    if not q:
        return _empty("keyword", NOTICE_EMPTY)
    tenant_id = (ctx or {}).get("tenant_id")
    if not tenant_id:
        raise ValueError("search needs a company (tenant_id) in the request context")
    limit = max(1, min(int(limit or 8), MAX_LIMIT))
    kinds = _clean_kinds(kinds)
    date_from, date_to = _clean_date(date_from), _clean_date(date_to)
    project_id = project_id if isinstance(project_id, str) and project_id.strip() else None
    gate_ctx = ChunkContext(db)  # small lookup cache, shared with the rerank gate below

    # 1. Three things at the same time, so the slow part (the question's
    #    vector) is not kept waiting by the permission lookups:
    #      * who may see what (build_scope, including the project check),
    #      * whether an explicit project exists in this company at all
    #        (check_project_access stays silent about that for all-projects
    #        admins and while project limits are off),
    #      * the outside-provider gate, then the vector if the gate says yes.
    #    If the project turns out to be unknown or refused, the vector is
    #    thrown away.
    t0 = time.perf_counter()

    async def _scope():
        try:
            return await build_scope(ctx, project_id=project_id, kinds=kinds,
                                     date_from=date_from, date_to=date_to,
                                     explicit=explicit)
        except HTTPException:
            return None

    async def _exists():
        if not project_id:
            return True
        found = await db.projects.find_one({"tenant_id": tenant_id, "id": project_id},
                                           {"_id": 0, "id": 1})
        return bool(found)

    async def _gate_and_embed():
        try:
            ok = await gate.project_allows_embedding(gate_ctx, str(tenant_id), project_id)
        except Exception as exc:  # cannot tell: nothing goes out
            logger.warning("search: gate check failed (%s); keyword only", type(exc).__name__)
            ok = False
        if not ok:
            return False, None
        try:
            vecs, _tokens = await asyncio.wait_for(
                (embedder or _default_embedder)([q], input_type="query"), EMBED_TIMEOUT_S)
            return True, (list(vecs[0]) if vecs and vecs[0] else None)
        except Exception as exc:
            logger.info("search: query embedding unavailable (%s); keyword only", type(exc).__name__)
            return True, None

    scope, exists, (allowed, vector) = await asyncio.gather(_scope(), _exists(), _gate_and_embed())
    timings["embed_ms"] = _ms(t0)
    if scope is None or not exists:
        return _empty("unavailable", NOTICE_NO_PROJECT)
    notice: Optional[str] = None
    if not allowed:
        notice = NOTICE_GATED
    elif vector is None:
        notice = NOTICE_NO_VECTOR

    # 2. Both searches at once.
    t0 = time.perf_counter()

    async def _vec():
        if vector is None:
            return None
        try:
            return await _run_vector_stage(db, vector_pipeline(vector, scope))
        except Exception as exc:
            logger.warning("search: vector search failed (%s)", type(exc).__name__)
            return None

    async def _txt():
        try:
            return await _run_text_stage(db, text_pipeline(q, scope))
        except Exception as exc:
            logger.warning("search: keyword search failed (%s)", type(exc).__name__)
            return None

    vec_hits, txt_hits = await asyncio.gather(_vec(), _txt())
    timings["search_ms"] = _ms(t0)
    if vec_hits is None and txt_hits is None:
        timings["total_ms"] = _ms(t_all)
        logger.info("search: unavailable timings=%s", timings)
        return _empty("unavailable", NOTICE_UNAVAILABLE)
    mode = "hybrid" if vec_hits is not None else "keyword"
    if vec_hits is None and vector is not None:
        notice = NOTICE_NO_VECTOR

    # Defence in depth: the same permission rules again on every hit.
    docs: dict[Any, dict] = {}
    lists = []
    for hits in (vec_hits, txt_hits):
        ids = []
        for h in hits or []:
            if not chunk_allowed(scope, h):
                continue
            docs.setdefault(h["_id"], h)
            ids.append(h["_id"])
        lists.append(ids)
    fused = rrf_fuse(*lists)

    # 3. Rerank the top of the merged list.
    t0 = time.perf_counter()
    top = [(docs[i], s) for i, s in fused[:RERANK_TOP]]
    ranked: list[tuple[dict, float]] = top
    if vector is not None and len(top) > 1:
        try:
            ranked = await rerank_top(
                q, top, reranker or _default_reranker,
                lambda pid: gate.project_allows_embedding(gate_ctx, scope.tenant_id, pid))
        except Exception as exc:
            logger.info("search: rerank unavailable (%s); keeping merged order", type(exc).__name__)
            ranked = top
    timings["rerank_ms"] = _ms(t0)
    ranked += [(docs[i], s) for i, s in fused[RERANK_TOP:]]

    # 4. One result per record, then re-check each record itself.
    t0 = time.perf_counter()
    scored = []
    for d, s in ranked:
        scored.append({**d, "_score": s})
    candidates = collapse(scored)
    kept: list[dict] = []
    pos = 0
    while len(kept) < limit and pos < len(candidates):
        batch = candidates[pos:pos + max(limit * 2, 8)]
        pos += len(batch)
        kept += await rehydrate(db, ctx, scope, batch)
    kept = kept[:limit]
    names = await project_names(db, scope.tenant_id, [h.get("project_id") for h in kept])
    timings["rehydrate_ms"] = _ms(t0)

    from auth import redact_secrets
    from services.search.chunkers.docs_company import scrub

    results = []
    for n, h in enumerate(kept, start=1):
        label = _label(h)
        excerpt = best_excerpt(scrub(h.get("body") or h.get("text") or ""), q)
        results.append(redact_secrets({
            "cite": f"S{n}",
            "kind": h.get("kind"),
            "source_collection": h.get("source_collection"),
            "source_id": h.get("source_id") or h.get("source_oid"),
            "project_id": h.get("project_id"),
            "project_name": names.get(h.get("project_id")) if h.get("project_id") else None,
            "label": label,
            "date": h.get("date"),
            "activity_code": list(h.get("activity_code") or []),
            "excerpt": excerpt,
            "score": round(float(h.get("_score") or 0.0), 4),
            "url": h.get("url"),
        }))
    timings["total_ms"] = _ms(t_all)
    logger.info("search: mode=%s results=%d candidates=%d timings=%s",
                mode, len(results), len(candidates), timings)
    return {"count": len(results), "mode": mode, "results": results, "notice": notice}
