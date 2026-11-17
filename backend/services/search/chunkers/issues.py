"""Search chunkers for the "issues" group: delays, requests for information
(RFIs), submittals, coordination events, field issues (work items), change
orders, the knowledge base and the per-project master document.

Each chunker turns ONE source row into zero or more chunks (see
services/search/types.py for the contract). The change-stream listener
queues every write; the worker loads the row and calls the chunker here.

Visibility: none of these collections restrict reads by role today, so
allowed_roles is left as None (every role that can see the project):
  * delays   - GET /projects/{pid}/delays uses only tenant_filter
               (routers/field_events_router.py:562-578)
  * rfis     - GET /projects/{pid}/rfis (routers/docs_router.py:371-391)
  * submittals - GET /projects/{pid}/submittals (routers/iter4_router.py:459-465);
               the timing routes allow every role (routers/submittal_sla_router.py:50)
  * coordination_events - routers/coordination_router.py:118-215
  * work_items - routers/docs_router.py:651-658
  * change_orders - routers/docs_router.py:607-612
  * kb_entries - routers/iter7_router.py:491-506 (company-level rows shared)
  * master_docs - routers/iter7_router.py:621-630
Project membership is handled by the search query (design section 4.3).

Links: the app has detail pages for delays (/delays/:id) and coordination
events (/coordination/:id) (frontend/src/App.js:97,100). Other records use
the "<list page>?openId=<id>" deep link already used by
frontend/src/pages/Approvals.jsx:185-195.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from services.search.types import (
    Chunk, ChunkContext, ChunkResult, Entry, clean, iso_date,
)

# About 1,500 tokens. Longer bodies are split into several chunks that
# share the same header.
MAX_BODY_CHARS = 6000

# --------------------------------------------------------------------------
# Scrubbing: words people typed can still contain contact details or links.
# --------------------------------------------------------------------------
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Phone numbers with or without separators ("212-555-1234", "2125551234").
_PHONE = re.compile(r"(?<![\w.])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
# Storage keys and paths: "s3://bucket/key", "tenants/abc/obj-1.pdf".
_S3 = re.compile(r"\bs3://\S+", re.IGNORECASE)
_STORAGE = re.compile(
    r"\b[\w.-]+(?:/[\w.-]+){2,}\.(?:jpe?g|png|gif|heic|webp|pdf|mp3|m4a|wav|webm|ogg|xlsx?|mpp|docx?)\b",
    re.IGNORECASE,
)
# Dollar figures people type ("$18,750.50", "US$ 2.5m", "$40k").
_MONEY = re.compile(
    r"(?:US)?\$\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:k|m|mm|million|thousand)\b)?",
    re.IGNORECASE,
)
# Map coordinates ("40.712345, -73.987654").
_GPS = re.compile(r"-?\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}")
# The knowledge-base import bakes the field issue's cost into the body
# (routers/iter7_router.py:~553); drop that whole line.
_IMPACT_LINE = re.compile(r"^\s*IMPACT:\s*cost=.*$", re.IGNORECASE | re.MULTILINE)


def scrub(text: str) -> str:
    """Mask contact details, links, storage paths, money and map
    coordinates in free text, and drop knowledge-base cost lines.
    Same rules as the field chunker's scrubber (chunkers/field.py)."""
    if not text:
        return ""
    text = _IMPACT_LINE.sub("", text)
    text = _S3.sub("[file]", text)
    text = _URL.sub("[link]", text)
    text = _EMAIL.sub("[email]", text)
    text = _STORAGE.sub("[file]", text)
    text = _GPS.sub("[location]", text)
    text = _MONEY.sub("[amount]", text)
    text = _PHONE.sub("[phone]", text)
    return text


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _is_deleted(doc: dict) -> bool:
    return (doc.get("status") == "deleted") or bool(doc.get("deleted_at"))


def _empty(doc: dict, tenant_id: Optional[str] = None,
           project_id: Optional[str] = None) -> ChunkResult:
    """No chunks: the worker then removes any chunks this row had."""
    return ChunkResult(
        tenant_id=tenant_id or doc.get("tenant_id"),
        project_id=project_id if project_id is not None else doc.get("project_id"),
        source_id=doc.get("id"),
        chunks=[],
    )


def _label(v: Any) -> str:
    """'building_coordination' -> 'building coordination'."""
    if v is None:
        return ""
    return clean(str(v).replace("_", " "))


def _paras(text: Any) -> list[str]:
    """Split free text into tidy paragraphs (one per non-empty line)."""
    if not text:
        return []
    if isinstance(text, (list, tuple)):
        text = "\n".join(str(t) for t in text if t)
    out = []
    for line in str(text).splitlines():
        line = clean(line)
        if line:
            out.append(line)
    return out


def _labelled(label: str, text: Any) -> list[str]:
    """Paragraphs of `text` with `label` in front of the first one."""
    ps = _paras(text)
    if ps:
        ps[0] = f"{label}: {ps[0]}"
    return ps


def _split(paras: list[str], limit: int = MAX_BODY_CHARS) -> list[str]:
    """Group paragraphs into bodies no longer than `limit` characters."""
    pieces: list[str] = []
    cur: list[str] = []
    size = 0
    for p in paras:
        while len(p) > limit:  # one huge paragraph: cut at a word boundary
            cut = p.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if cur:
                pieces.append("\n".join(cur))
                cur, size = [], 0
            pieces.append(p[:cut].strip())
            p = p[cut:].strip()
        if cur and size + len(p) + 1 > limit:
            pieces.append("\n".join(cur))
            cur, size = [], 0
        if p:
            cur.append(p)
            size += len(p) + 1
    if cur:
        pieces.append("\n".join(cur))
    return [x for x in pieces if x.strip()]


def _uniq(values: Iterable[Any]) -> list[str]:
    seen: list[str] = []
    for v in values:
        s = clean(v) if v is not None else ""
        if s and s not in seen:
            seen.append(s)
    return seen


def _header(*parts: Any) -> str:
    return clean(*[scrub(str(p)) for p in parts if p], sep=" | ")


def _short(text: Any, n: int = 60) -> str:
    s = clean(text)
    if len(s) <= n:
        return s
    cut = s.rfind(" ", 0, n)
    return s[: cut if cut > n // 2 else n].rstrip() + "..."


def _numbered(prefix: str, number: Any) -> str:
    """'RFI' + '12' -> 'RFI #12'; 'CO' + 'CO-003' -> 'CO-003';
    'Submittal' + 'SUB-0002' -> 'Submittal SUB-0002'."""
    n = clean(number)
    if not n:
        return prefix
    if n.upper().startswith(prefix.upper()):
        return n
    if n.isdigit():
        return f"{prefix} #{n}"
    return f"{prefix} {n}"


def _chunks(kind: str, header: str, paras: list[str], **meta: Any) -> list[Chunk]:
    """One chunk per body piece; everything scrubbed. No text -> no chunks."""
    clean_paras = [p for p in (scrub(x).strip() for x in paras) if p]
    bodies = _split(clean_paras)
    return [Chunk(kind=kind, header=header, body=b, **meta) for b in bodies]


async def _project(ctx: ChunkContext, tenant_id: Optional[str],
                   project_id: Optional[str]) -> Optional[dict]:
    """The row's project, or None when the row is an orphan (project gone,
    deleted, or belonging to another company)."""
    if not tenant_id or not project_id:
        return None
    p = await ctx.one("projects", {"id": project_id},
                      {"id": 1, "tenant_id": 1, "name": 1, "number": 1,
                       "status": 1, "deleted_at": 1})
    if not p:
        return None
    if p.get("tenant_id") and p.get("tenant_id") != tenant_id:
        return None
    if _is_deleted(p):
        return None
    return p


def _project_name(p: Optional[dict]) -> str:
    if not p:
        return ""
    return clean(p.get("name") or p.get("number") or "")


async def _task_codes(ctx: ChunkContext, tenant_id: str,
                      task_ids: Iterable[Any]) -> tuple[list[str], list[str]]:
    """Activity codes and trade codes of the linked schedule activities."""
    acts: list[Any] = []
    trades: list[Any] = []
    for tid in _uniq(task_ids):
        t = await ctx.one("tasks", {"id": tid, "tenant_id": tenant_id},
                          {"activity_code": 1, "trade_code": 1,
                           "status": 1, "deleted_at": 1})
        # A deleted activity no longer exists for search purposes.
        if not t or _is_deleted(t):
            continue
        acts.append(t.get("activity_code"))
        trades.append(t.get("trade_code"))
    return _uniq(acts), _uniq(trades)


async def _many(ctx: ChunkContext, collection: str, filt: dict,
                projection: dict, limit: int = 200) -> list[dict]:
    """Several rows (child rows like review events or links). Never raises."""
    try:
        proj = {**projection, "_id": 0}
        return await ctx.db[collection].find(filt, proj).to_list(limit)
    except Exception:
        return []


def _acts_part(acts: list[str]) -> str:
    return f"activities {', '.join(acts)}" if acts else ""


def _trade_part(trades: list[str]) -> str:
    return f"trade {', '.join(trades)}" if trades else ""


# --------------------------------------------------------------------------
# delays
# --------------------------------------------------------------------------
PAPERWORK_BLOCKER_SOURCE = "paperwork_blocker"  # routers/tasks_router.py:187


def _with_mitigations(res: ChunkResult, doc: dict) -> ChunkResult:
    """Re-queue the delay's recovery scenarios (mitigations). Their chunks
    carry the delay's description and vanish when the delay is deleted, so
    every change to a delay must refresh them. Unchanged chunks cost
    nothing: the worker skips them by content hash."""
    tid, did = doc.get("tenant_id"), doc.get("id")
    if tid and did:
        res.requeue.append(("mitigations", {"tenant_id": tid, "linked_delay_id": did}))
    return res


async def chunk_delay(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    return _with_mitigations(await _chunk_delay(ctx, doc), doc)


async def _chunk_delay(ctx: ChunkContext, doc: dict) -> ChunkResult:
    if _is_deleted(doc):
        return _empty(doc)
    tid, pid = doc.get("tenant_id"), doc.get("project_id")
    proj = await _project(ctx, tid, pid)
    if not proj:
        return _empty(doc)
    pname = _project_name(proj)
    acts, trades = await _task_codes(
        ctx, tid, list(doc.get("linked_task_ids") or []) + [doc.get("schedule_activity_id")])
    date = iso_date(doc.get("date") or doc.get("logged_date") or doc.get("reported_at"))
    status = clean(doc.get("status")) or None

    # Rows the app creates on its own (a paperwork blocker or quality
    # rework) are labelled so people can tell them from field-logged delays.
    if doc.get("source") == PAPERWORK_BLOCKER_SOURCE:
        origin = "paperwork blocker (automatic)"
    elif doc.get("linked_qc_issue_id"):
        origin = "quality rework (automatic)"
    else:
        origin = ""

    header = _header(
        pname,
        clean("Delay", date),
        clean(_label(doc.get("severity")), _label(doc.get("reason_category")), origin),
        _acts_part(acts),
        f"status {_label(status)}" if status else "",
    )
    title = clean("Delay", date, "-", _short(doc.get("description"))) if doc.get("description") \
        else clean("Delay", date)
    meta = dict(date=date, activity_code=acts, trade_code=trades, status=status,
                title=title, url=f"/delays/{doc.get('id')}")

    paras = _paras(doc.get("description"))
    paras += _labelled("Resolved", doc.get("resolved_note"))
    paras += _labelled("Filed after the daily report because", doc.get("supplement_reason"))
    chunks = _chunks("delay", header, paras, **meta)
    if not chunks:
        return _empty(doc)
    # The AI-written narrative is its own kind so it can be ranked below
    # the super's own words.
    ai_header = _header(pname, clean("AI delay report", date), _acts_part(acts))
    chunks += _chunks("delay_ai_report", ai_header, _paras(doc.get("ai_report")),
                      **{**meta, "title": clean("AI delay report", date)})
    return ChunkResult(tenant_id=tid, project_id=pid, source_id=doc.get("id"), chunks=chunks)


# --------------------------------------------------------------------------
# rfis
# --------------------------------------------------------------------------
async def chunk_rfi(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    if _is_deleted(doc):
        return _empty(doc)
    tid, pid = doc.get("tenant_id"), doc.get("project_id")
    proj = await _project(ctx, tid, pid)
    if not proj:
        return _empty(doc)
    acts, task_trades = await _task_codes(
        ctx, tid, list(doc.get("linked_task_ids") or []) + [doc.get("schedule_activity_id")])
    trades = _uniq([doc.get("trade_code")]) or task_trades
    date = iso_date(doc.get("date_initiated") or doc.get("created_at"))
    status = clean(doc.get("status")) or None
    number = _numbered("RFI", doc.get("rfi_number"))
    subject = clean(doc.get("subject"))
    specs = _uniq(doc.get("spec_section_refs") or [])

    header = _header(
        _project_name(proj),
        clean(number, subject, date),
        clean(f"priority {_label(doc.get('priority'))}" if doc.get("priority") else "",
              f"status {_label(status)}" if status else ""),
        _trade_part(trades),
        f"spec {', '.join(specs)}" if specs else "",
        _acts_part(acts),
    )
    paras = _paras(doc.get("description"))
    parties = clean(f"from {clean(doc.get('from_party'))}" if doc.get("from_party") else "",
                    f"to {clean(doc.get('to_party'))}" if doc.get("to_party") else "")
    if parties:
        paras.append("Sent " + parties)
    paras += _labelled("Response", doc.get("response_text"))
    paras += _labelled("Filed after the daily report because", doc.get("supplement_reason"))
    if not paras and not subject:
        return _empty(doc)
    if subject:
        paras.insert(0, subject)
    chunks = _chunks(
        "rfi", header, paras,
        date=date, activity_code=acts, trade_code=trades, status=status,
        title=clean(number, "-", subject) if subject else number,
        url=f"/rfis?openId={doc.get('id')}",
    )
    return ChunkResult(tenant_id=tid, project_id=pid, source_id=doc.get("id"), chunks=chunks)


# --------------------------------------------------------------------------
# submittals (+ notes from submittal_review_events)
# --------------------------------------------------------------------------
async def chunk_submittal(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    if _is_deleted(doc):
        return _empty(doc)
    tid, pid = doc.get("tenant_id"), doc.get("project_id")
    proj = await _project(ctx, tid, pid)
    if not proj:
        return _empty(doc)
    acts, task_trades = await _task_codes(
        ctx, tid, list(doc.get("linked_task_ids") or []) + [doc.get("schedule_activity_id")])
    trades = _uniq([doc.get("discipline")]) or task_trades
    # Note: aging / SLA day counters are deliberately left out of the text
    # so the nightly timer refresh does not change the content hash.
    date = iso_date(doc.get("submitted_at") or doc.get("created_at"))
    status = clean(doc.get("status")) or None
    number = _numbered("Submittal", doc.get("submittal_number"))
    rev = doc.get("revision_number")
    title_text = clean(doc.get("title"))
    sla = doc.get("sla_status")
    typ = _label(doc.get("type"))

    header = _header(
        _project_name(proj),
        clean(number, f"rev {rev}" if rev else "", title_text, date),
        f"spec {clean(doc.get('spec_section_code'))}" if doc.get("spec_section_code") else "",
        clean(typ, _label(doc.get("discipline"))),
        f"sub {clean(doc.get('sub_responsible'))}" if doc.get("sub_responsible") else "",
        clean(f"status {_label(status)}" if status else "",
              f"SLA {_label(sla)}" if sla and sla != "n_a" else "",
              "superseded" if doc.get("superseded_by") else ""),
        _acts_part(acts),
    )
    paras = _paras(title_text)
    paras += _labelled("Type", doc.get("type_other_description"))
    paras += _labelled("Late because", doc.get("sla_breach_reason"))
    # Review notes people typed on the review timeline.
    events = await _many(ctx, "submittal_review_events",
                         {"tenant_id": tid, "submittal_id": doc.get("id")},
                         {"performed_at": 1, "event_type": 1, "notes": 1})
    events = [e for e in events if clean(e.get("notes"))]
    events.sort(key=lambda e: str(e.get("performed_at") or ""))
    for e in events:
        paras += _labelled(clean(iso_date(e.get("performed_at")), _label(e.get("event_type"))) or "Review note",
                           e.get("notes"))
    chunks = _chunks(
        "submittal", header, paras,
        date=date, activity_code=acts, trade_code=trades, status=status,
        title=clean(number, "-", title_text) if title_text else number,
        url=f"/submittals?openId={doc.get('id')}",
    )
    if not chunks:
        return _empty(doc)
    return ChunkResult(tenant_id=tid, project_id=pid, source_id=doc.get("id"), chunks=chunks)


async def chunk_submittal_review_event(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    """No chunks of its own: a note rolls up into the parent submittal, so
    a review event with a note re-queues that submittal."""
    tid = doc.get("tenant_id")
    sid = doc.get("submittal_id")
    parent = None
    if tid and sid:
        parent = await ctx.one("submittals", {"id": sid, "tenant_id": tid},
                               {"id": 1, "project_id": 1})
    if not parent:
        return _empty(doc)  # orphan: the submittal (or its company) is gone
    res = _empty(doc, tenant_id=tid, project_id=doc.get("project_id") or parent.get("project_id"))
    if clean(doc.get("notes")):
        res.requeue.append(("submittals", {"tenant_id": tid, "id": sid}))
    return res


# --------------------------------------------------------------------------
# coordination_events
# --------------------------------------------------------------------------
_COORD_TOPICS = {"tenant_access", "building_coordination", "a_e_clarification",
                 "owner_direction", "sub_coordination", "regulatory",
                 "safety", "quality", "scope", "payment", "other"}  # models.py:3056


async def chunk_coordination_event(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    if _is_deleted(doc):
        return _empty(doc)
    tid, pid = doc.get("tenant_id"), doc.get("project_id")
    proj = await _project(ctx, tid, pid)
    if not proj:
        return _empty(doc)
    acts, task_trades = await _task_codes(
        ctx, tid, list(doc.get("linked_task_ids") or []) + list(doc.get("linked_activity_ids") or []))
    trades = _uniq([doc.get("trade_code")]) or task_trades
    date = iso_date(doc.get("occurred_at") or doc.get("created_at") or doc.get("date"))

    # Same fallbacks as the list page (routers/coordination_router.py:176-201):
    # legacy and voice rows may keep free text in `topic` and no summary.
    topic = doc.get("topic")
    topic_text = ""
    if topic and topic not in _COORD_TOPICS:
        topic_text, topic = clean(topic), "other"
    summary = clean(doc.get("summary")) or topic_text or clean(doc.get("description"))

    party = clean(doc.get("other_party_name") or doc.get("with_party"))
    party_extra = ", ".join(_uniq([doc.get("other_party_org"), doc.get("other_party_role")]))
    with_part = clean(f"with {party}" if party and party != "—" else "",
                      f"({party_extra})" if party_extra else "")
    # other_party_email / other_party_phone are never read here.
    outcome = doc.get("outcome")
    header = _header(
        _project_name(proj),
        clean("Coordination", _label(doc.get("channel")), _label(doc.get("direction")), date),
        with_part,
        clean(f"topic {_label(topic)}" if topic else "",
              f"outcome {_label(outcome)}" if outcome else ""),
        _trade_part(trades),
        _acts_part(acts),
    )
    paras = _paras(summary)
    paras += _labelled("Topic", doc.get("topic_other_description"))
    paras += _paras(doc.get("detail_text"))
    paras += _labelled("Outcome", doc.get("outcome_notes"))
    paras += _labelled("Filed after the daily report because", doc.get("supplement_reason"))
    chunks = _chunks(
        "coordination", header, paras,
        date=date, activity_code=acts, trade_code=trades,
        status=_label(outcome) or None,
        title=clean("Coordination", date, "-", _short(summary)) if summary else clean("Coordination", date),
        url=f"/coordination/{doc.get('id')}",
    )
    if not chunks:
        return _empty(doc)
    return ChunkResult(tenant_id=tid, project_id=pid, source_id=doc.get("id"), chunks=chunks)


# --------------------------------------------------------------------------
# work_items (field issues) + work_item_links
# --------------------------------------------------------------------------
async def chunk_work_item(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    if _is_deleted(doc):
        return _empty(doc)
    tid, pid = doc.get("tenant_id"), doc.get("project_id")
    proj = await _project(ctx, tid, pid)
    if not proj:
        return _empty(doc)
    links = await _many(ctx, "work_item_links",
                        {"tenant_id": tid, "work_item_id": doc.get("id")},
                        {"entity_type": 1, "entity_id": 1, "notes": 1, "created_at": 1})
    links.sort(key=lambda r: str(r.get("created_at") or ""))
    acts, trades = await _task_codes(
        ctx, tid, [l.get("entity_id") for l in links if l.get("entity_type") == "task"])
    location = ""
    if doc.get("location_id"):
        loc = await ctx.one("locations", {"id": doc["location_id"], "tenant_id": tid}, {"name": 1})
        location = clean((loc or {}).get("name"))
    date = iso_date(doc.get("opened_at") or doc.get("created_at"))
    status = clean(doc.get("status")) or None
    title_text = clean(doc.get("title"))

    header = _header(
        _project_name(proj),
        clean("Field issue", title_text, date),
        clean(f"priority {_label(doc.get('priority'))}" if doc.get("priority") else "",
              f"status {_label(status)}" if status else ""),
        f"location {location}" if location else "",
        _acts_part(acts),
    )
    # Cost fields (estimated_cost_impact, actual_cost_impact) are never read.
    paras = _paras(title_text)
    paras += _paras(doc.get("description"))
    paras += _labelled("Root cause", doc.get("root_cause"))
    paras += _labelled("Resolution", doc.get("closure_summary"))
    for l in links:
        paras += _labelled("Link note", l.get("notes"))
    chunks = _chunks(
        "work_item", header, paras,
        date=date, activity_code=acts, trade_code=trades, status=status,
        title=clean("Field issue -", title_text) if title_text else "Field issue",
        url=f"/work-items?openId={doc.get('id')}",
    )
    if not chunks:
        return _empty(doc)
    return ChunkResult(tenant_id=tid, project_id=pid, source_id=doc.get("id"), chunks=chunks)


async def chunk_work_item_link(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    """No chunks of its own (the row has no project_id): it re-queues its
    parent field issue, whose chunk carries the linked activity codes and
    the link's note."""
    tid = doc.get("tenant_id")
    wid = doc.get("work_item_id")
    parent = None
    if tid and wid:
        parent = await ctx.one("work_items", {"id": wid, "tenant_id": tid},
                               {"id": 1, "project_id": 1})
    if not parent:
        return _empty(doc, project_id=None)
    res = _empty(doc, tenant_id=tid, project_id=parent.get("project_id"))
    res.requeue.append(("work_items", {"tenant_id": tid, "id": wid}))
    return res


# --------------------------------------------------------------------------
# change_orders
# --------------------------------------------------------------------------
async def chunk_change_order(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    if _is_deleted(doc):  # soft delete via routers/iter8_router.py:73
        return _empty(doc)
    tid, pid = doc.get("tenant_id"), doc.get("project_id")
    proj = await _project(ctx, tid, pid)
    if not proj:
        return _empty(doc)
    acts, trades = await _task_codes(ctx, tid, [doc.get("schedule_activity_id")])
    date = iso_date(doc.get("proposed_at") or doc.get("created_at"))
    status = clean(doc.get("status")) or None
    number = _numbered("CO", doc.get("co_number"))
    title_text = clean(doc.get("title"))
    rfi_label = ""
    if doc.get("originating_rfi_id"):
        rfi = await ctx.one("rfis", {"id": doc["originating_rfi_id"], "tenant_id": tid},
                            {"rfi_number": 1})
        if rfi:
            rfi_label = _numbered("RFI", rfi.get("rfi_number"))
    days = doc.get("time_extension_days")
    # amount_usd is never read (money stays out of the text).
    header = _header(
        _project_name(proj),
        clean("Change order", number, title_text, date),
        clean(_label(doc.get("type")), f"status {_label(status)}" if status else ""),
        f"time extension {days} days" if isinstance(days, (int, float)) and days else "",
        f"from {rfi_label}" if rfi_label else "",
        _acts_part(acts),
    )
    paras = _paras(title_text) + _paras(doc.get("description"))
    chunks = _chunks(
        "change_order", header, paras,
        date=date, activity_code=acts, trade_code=trades, status=status,
        title=clean("Change order", number, "-", title_text) if title_text else clean("Change order", number),
        url=f"/change-orders?openId={doc.get('id')}",
    )
    if not chunks:
        return _empty(doc)
    return ChunkResult(tenant_id=tid, project_id=pid, source_id=doc.get("id"), chunks=chunks)


# --------------------------------------------------------------------------
# kb_entries (lessons learned)
# --------------------------------------------------------------------------
# Entries imported from RFIs / field issues / submittals copy text that is
# already indexed from the source record, so only hand-written ones count.
_KB_OWN_KINDS = {"manual", "lesson"}


async def chunk_kb_entry(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    if _is_deleted(doc):
        return _empty(doc)
    if (doc.get("source_kind") or "manual") not in _KB_OWN_KINDS:
        return _empty(doc)
    tid, pid = doc.get("tenant_id"), doc.get("project_id") or None
    if not tid:
        return _empty(doc)
    if pid:
        proj = await _project(ctx, tid, pid)
        if not proj:
            return _empty(doc)
        where = _project_name(proj)
    else:
        # Company-level entry: orphan when the company itself is gone.
        tenant = await ctx.one("tenants", {"id": tid}, {"id": 1})
        if not tenant:
            return _empty(doc, project_id=None)
        where = "Company-wide"
    date = iso_date(doc.get("created_at"))
    title_text = clean(doc.get("title"))
    tags = _uniq(doc.get("tags") or [])
    header = _header(
        where,
        clean("Lesson learned", title_text, date),
        f"tags {', '.join(tags)}" if tags else "",
    )
    paras = _paras(doc.get("body"))
    if not paras:
        return _empty(doc, project_id=pid)
    chunks = _chunks(
        "kb", header, [title_text] + paras if title_text else paras,
        date=date, status=None,
        title=clean("Lesson -", title_text) if title_text else "Lesson learned",
        url=f"/kb?openId={doc.get('id')}",
    )
    return ChunkResult(tenant_id=tid, project_id=pid, source_id=doc.get("id"), chunks=chunks)


# --------------------------------------------------------------------------
# master_docs (one per project)
# --------------------------------------------------------------------------
_MASTER_FLAGS = (
    ("occupancy_during_work", "Building occupied during work"),
    ("historic_designation", "Historic designation"),
    ("abatement_required", "Abatement required"),
    ("asbestos_known", "Asbestos known"),
    ("lead_paint_known", "Lead paint known"),
    ("swing_shifts_allowed", "Swing shifts allowed"),
)


async def chunk_master_doc(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    # Anything above "public" (fouo / secret / ts, routers/iter7_router.py:593)
    # must never leave for an outside provider: no chunks at all.
    if doc.get("govt_security_level") != "public":
        return _empty(doc)
    if _is_deleted(doc):
        return _empty(doc)
    tid, pid = doc.get("tenant_id"), doc.get("project_id")
    proj = await _project(ctx, tid, pid)
    if not proj:
        return _empty(doc)
    pname = _project_name(proj)
    paras = _paras(doc.get("notes"))
    constraints = _uniq(doc.get("site_constraints") or [])
    if constraints:
        paras.append("Site constraints: " + "; ".join(constraints))
    subs = _uniq(doc.get("primary_subs") or [])
    if subs:
        paras.append("Primary subcontractors: " + ", ".join(subs))
    flags = [label for key, label in _MASTER_FLAGS if doc.get(key)]
    if flags:
        paras.append("; ".join(flags))
    if not paras:
        return _empty(doc)
    date = iso_date(doc.get("updated_at") or doc.get("created_at"))
    header = _header(
        pname,
        clean("Master document", date),
        clean(_label(doc.get("construction_type")), _label(doc.get("delivery_method"))),
        f"sketch review {_label(doc.get('sketch_review_method'))}" if doc.get("sketch_review_method") else "",
    )
    chunks = _chunks(
        "master_doc", header, paras,
        date=date, title=clean("Master document -", pname) if pname else "Master document",
        url="/master-doc",
    )
    return ChunkResult(tenant_id=tid, project_id=pid, source_id=doc.get("id"), chunks=chunks)


# --------------------------------------------------------------------------
# Registry entries
# --------------------------------------------------------------------------
ENTRIES: list[Entry] = [
    Entry("delays", "index",
          "Field-logged delays: the super's own description and resolution note, plus the AI delay report as its own kind.",
          chunk_delay),
    Entry("rfis", "index",
          "Requests for information: question, answer and parties are prime search text.",
          chunk_rfi),
    Entry("submittals", "index",
          "Submittal log titles, spec section and sub, plus any review notes typed on the review timeline.",
          chunk_submittal),
    Entry("submittal_review_events", "index",
          "No chunks of its own: a review note rolls into the parent submittal, so this only re-queues that submittal.",
          chunk_submittal_review_event),
    Entry("coordination_events", "index",
          "Calls, emails and meetings: summary, detail and outcome are key evidence (contact email and phone never read).",
          chunk_coordination_event),
    Entry("work_items", "index",
          "Field issues: problem, root cause and resolution text (cost fields never read).",
          chunk_work_item),
    Entry("work_item_links", "index",
          "No chunks of its own (no project on the row): re-queues the parent field issue, which carries the linked activity codes and link notes.",
          chunk_work_item_link),
    Entry("change_orders", "index",
          "Change order title and description; the dollar amount stays out of the text.",
          chunk_change_order),
    Entry("kb_entries", "index",
          "Hand-written lessons learned (manual / lesson only; imported entries would duplicate the RFI and field-issue chunks).",
          chunk_kb_entry),
    Entry("master_docs", "index",
          "Per-project notes, site constraints and main subs; skipped entirely unless the security level is public.",
          chunk_master_doc),
    Entry("approvals", "query_only",
          "Workflow records answered by filters (what is pending, who decided); the payload is arbitrary data and is never embedded."),
    Entry("gating_entities", "query_only",
          "A mirror of RFIs, submittals and field issues for the schedule engine; its only text copies the source record."),
    Entry("work_item_history", "exclude",
          "Event log of field-issue changes; the field issue's own chunk already holds the current text."),
]
