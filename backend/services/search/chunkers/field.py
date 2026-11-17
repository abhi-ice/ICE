"""Search chunkers for the field group: daily reports and everything filed on them.

Each chunker turns ONE source document into chunks (see services.search.types).
The change-stream listener queues every write on an indexed collection; the
worker loads the row and calls the chunker here. Rows that only feed another
record's chunk (presence, attendance, photo tray) return no chunks of their
own and ask for the parent record to be rebuilt instead (`requeue`).

What goes into chunk text: the people's own words (notes, descriptions,
reasons, findings) plus a short header. What never goes in: emails, phone
numbers, money, GPS, storage keys / links, tokens and attendance names.
Free text is passed through `_scrub` as a second line of defence, because
someone can type a phone number or an email address into a note.

allowed_roles stays None on every chunk here: none of today's read endpoints
for these records limit by role, only by company and project (daily reports
list daily_reports_router.py:362-368, presence :2611, deliveries
iter5_router.py:1062, safety events field_events_router.py:1239, quality
issues :1487, photos :1810, photo reviews photo_tray_router.py:1256-1269).
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional
from urllib.parse import quote

from services.search.types import (
    Chunk, ChunkContext, ChunkResult, Entry, clean, iso_date,
)

# Notes longer than this (about 1,500 tokens) are split into several chunks.
MAX_BODY_CHARS = 6000

# The note the server writes on presence rows it creates by itself from the
# activity grid's crew dropdown (routers/daily_reports_router.py:1436). It is
# template text, not something a person said.
_AUTO_PRESENCE_NOTE = "auto-created from activity grid crew dropdown"

# Same projections everywhere so ChunkContext's cache is shared.
_PROJECT_PROJ = {"name": 1, "number": 1, "status": 1, "deleted_at": 1}
_TASK_PROJ = {"activity_code": 1, "trade_code": 1, "status": 1}
_REPORT_PROJ = {"id": 1, "tenant_id": 1, "project_id": 1, "report_date": 1, "status": 1}

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
_STORAGE_RE = re.compile(
    r"\b[\w.-]+(?:/[\w.-]+){2,}\.(?:jpe?g|png|gif|heic|webp|pdf|mp3|m4a|wav|webm|ogg|xlsx?|mpp|docx?)\b",
    re.I,
)
_PHONE_RE = re.compile(r"(?<![\w.])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?![\w])")
_MONEY_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s?[kKmM]\b)?")
_GPS_RE = re.compile(r"-?\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}")


def _scrub(text: str) -> str:
    """Mask contact details, links, file keys, money and coordinates in free text."""
    if not text:
        return ""
    text = _EMAIL_RE.sub("[email]", text)
    text = _URL_RE.sub("[link]", text)
    text = _STORAGE_RE.sub("[file]", text)
    text = _GPS_RE.sub("[location]", text)
    text = _MONEY_RE.sub("[amount]", text)
    text = _PHONE_RE.sub("[phone]", text)
    return text


def _meaningful(v: Any) -> bool:
    """True for text a person would search on: at least 3 characters and more
    than one distinct letter or digit (so 'aaaa' or '...' do not count)."""
    if not isinstance(v, str):
        return False
    s = v.strip()
    if len(s) < 3:
        return False
    return len({c for c in s.lower() if c.isalnum()}) >= 2


def _text(v: Any) -> str:
    """Scrubbed, whitespace-collapsed text, or '' when it says nothing."""
    if not _meaningful(v):
        return ""
    return _scrub(clean(v))


def _labelled(label: str, v: Any) -> str:
    t = _text(v)
    return f"{label}: {t}" if t else ""


def _num(v: Any) -> Optional[float]:
    if isinstance(v, dict):  # extended JSON from a backup, e.g. {"$numberDouble": "72.7"}
        v = next(iter(v.values()), None)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt_num(v: Any) -> str:
    n = _num(v)
    if n is None:
        return ""
    return str(int(round(n))) if abs(n - round(n)) < 0.05 else f"{n:.1f}"


def _words(v: Any) -> str:
    """'in_progress' -> 'in progress'."""
    return clean(str(v).replace("_", " ")) if v else ""


def _is_deleted(doc: dict) -> bool:
    return (str(doc.get("status") or "").lower() == "deleted") or bool(doc.get("deleted_at"))


def _q(v: Any) -> str:
    return quote(str(v), safe="")


def _report_url(report_id: Optional[str], event_id: Any = None, event_type: Optional[str] = None) -> Optional[str]:
    """App path that opens a daily report, optionally scrolled to one row.
    The page honours ?openId, ?openEvent and ?openEventType
    (frontend/src/pages/DailyReports.jsx:276-283 and :826-840; the same link
    shape is built in ProjectScopedList.jsx:398 and Photos.jsx:137)."""
    if not report_id:
        return None
    url = f"/daily-reports?openId={_q(report_id)}"
    if event_id and event_type:
        url += f"&openEvent={_q(event_id)}&openEventType={_q(event_type)}"
    return url


def _split(body: str, limit: int = MAX_BODY_CHARS) -> list[str]:
    """Split long text on '[Voice intake' markers, then paragraphs, then hard."""
    body = body.strip()
    if len(body) <= limit:
        return [body] if body else []
    parts = re.split(r"(?=\[Voice intake)", body)
    if len(parts) == 1:
        parts = re.split(r"\n\s*\n", body)
    pieces: list[str] = []
    cur = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        while len(p) > limit:  # a single huge paragraph: cut it
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(p[:limit])
            p = p[limit:]
        if cur and len(cur) + 2 + len(p) > limit:
            pieces.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        pieces.append(cur)
    return pieces


async def _many(ctx: ChunkContext, collection: str, filt: dict, limit: int = 500) -> list[dict]:
    """Rows matching a filter. Uses ctx.many() when the context offers one
    (tests do), else reads through ctx.db."""
    fn = getattr(ctx, "many", None)
    if callable(fn):
        return list(await fn(collection, filt) or [])[:limit]
    cur = ctx.db[collection].find(filt, {"_id": 0})
    return await cur.to_list(limit)


async def _project(ctx: ChunkContext, project_id: Optional[str],
                   tenant_id: Optional[str]) -> Optional[str]:
    """The project's display name, or None when the project no longer exists
    or belongs to another company (an orphan row must not be indexed)."""
    if not project_id or not tenant_id:
        return None
    p = await ctx.one("projects", {"id": project_id, "tenant_id": tenant_id}, _PROJECT_PROJ)
    if not p or _is_deleted(p):
        return None
    return (p.get("name") or p.get("number") or "").strip() or "Project"


async def _report(ctx: ChunkContext, report_id: Optional[str]) -> Optional[dict]:
    if not report_id:
        return None
    return await ctx.one("daily_reports", {"id": report_id}, _REPORT_PROJ)


async def _live_report(ctx: ChunkContext, report_id: Optional[str],
                       tenant_id: Optional[str]) -> Optional[dict]:
    """The report a record was filed on, or None when that report is gone,
    soft-deleted, or belongs to another company. Used for links and dates
    only: records filed on a report outlive it."""
    report = await _report(ctx, report_id)
    if not report or _is_deleted(report):
        return None
    if tenant_id and report.get("tenant_id") != tenant_id:
        return None
    return report


async def _task(ctx: ChunkContext, task_id: Optional[str], tenant_id: Optional[str]) -> Optional[dict]:
    """A schedule activity of the SAME company. The company is part of the
    filter so a stray id pointing at another company's task can never pull
    that company's activity code into this chunk."""
    if not task_id or not tenant_id:
        return None
    t = await ctx.one("tasks", {"id": task_id, "tenant_id": tenant_id}, _TASK_PROJ)
    if not t or _is_deleted(t):
        return None
    return t


async def _codes_for_tasks(ctx: ChunkContext, task_ids: Iterable[Any], tenant_id: Optional[str],
                           limit: int = 10) -> list[str]:
    codes: list[str] = []
    for tid in list(task_ids or [])[:limit]:
        t = await _task(ctx, tid, tenant_id)
        code = (t or {}).get("activity_code")
        if code and str(code) not in codes:
            codes.append(str(code))
    return codes


def _empty(tenant_id: Optional[str], project_id: Optional[str], source_id: Optional[str],
           requeue: Optional[list] = None) -> ChunkResult:
    return ChunkResult(tenant_id=tenant_id, project_id=project_id, source_id=source_id,
                       chunks=[], requeue=list(requeue or []))


def _header(*parts: str) -> str:
    return clean(*[p for p in parts if p], sep=" | ")


# ---------------------------------------------------------------------------
# daily_reports (the report chunk plus the manpower roll-up)
# ---------------------------------------------------------------------------

async def _manpower_chunk(ctx: ChunkContext, report: dict, pname: str) -> Optional[Chunk]:
    """One chunk listing who was on site: presence rows plus attendance notes.
    Attendance person names are never read into the text."""
    rid, tid = report.get("id"), report.get("tenant_id")
    rdate = iso_date(report.get("report_date"))
    presence = await _many(ctx, "daily_presence", {"tenant_id": tid, "daily_report_id": rid})
    attendance = await _many(ctx, "daily_report_attendance", {"tenant_id": tid, "daily_report_id": rid})

    lines: list[str] = []
    trades: list[str] = []
    codes: list[str] = []
    for row in sorted(presence, key=lambda r: str(r.get("created_at") or "")):
        if _is_deleted(row):
            continue
        note = row.get("notes") or ""
        if clean(note).lower() == _AUTO_PRESENCE_NOTE:
            note = ""
        note_t = _text(note)
        heads = _num(row.get("headcount")) or 0
        if heads <= 0 and not note_t:
            continue  # an empty placeholder row says nothing
        trade = clean(row.get("trade"))
        company = clean(row.get("company_name") or row.get("sub_name"))
        crew = ""
        if heads > 0:
            hrs = _fmt_num(row.get("hours_per_person"))
            crew = f"{_fmt_num(heads)} x {hrs}h" if hrs else f"{_fmt_num(heads)} workers"
        zone = clean(row.get("zone_name"))
        lines.append(clean(" - ".join(p for p in (trade, _scrub(company), crew, zone, note_t) if p)))
        if trade and trade not in trades:
            trades.append(trade)
        t = await _task(ctx, row.get("schedule_activity_id"), tid)
        if t and t.get("activity_code") and str(t["activity_code"]) not in codes:
            codes.append(str(t["activity_code"]))
    for row in attendance:
        note_t = _text(row.get("notes"))
        if not note_t:
            continue
        role = _words(row.get("role"))
        lines.append(f"Attendance note{f' ({role})' if role else ''}: {note_t}")

    if not lines:
        return None
    return Chunk(
        kind="manpower",
        header=_header(pname, f"Daily report {rdate or ''}".strip(), "Manpower"),
        body="\n".join(lines),
        date=rdate,
        activity_code=codes,
        trade_code=trades,
        status=report.get("status"),
        parent_id=rid,
        title=f"Manpower - Daily report {rdate or ''}".strip(),
        url=_report_url(rid),
    )


async def chunk_daily_report(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    rid, tid, pid = doc.get("id"), doc.get("tenant_id"), doc.get("project_id")
    if not rid or not tid or _is_deleted(doc):
        return _empty(tid, pid, rid)
    pname = await _project(ctx, pid, tid)
    if pname is None:
        return _empty(tid, pid, rid)

    rdate = iso_date(doc.get("report_date"))
    status = doc.get("status")
    weather = ""
    cond = clean(doc.get("weather_conditions"))
    hi, lo = _num(doc.get("weather_temp_high_f")), _num(doc.get("weather_temp_low_f"))
    hi = str(int(round(hi))) if hi is not None else ""  # whole degrees in the header
    lo = str(int(round(lo))) if lo is not None else ""
    if cond or hi or lo:
        temps = f"{hi or '?'}/{lo or '?'}F" if (hi or lo) else ""
        weather = clean("weather", cond, temps)
    header = _header(pname, f"Daily report {rdate or ''}".strip(),
                     clean("status", status) if status else "", weather)

    body_parts = [_text(doc.get("notes"))]
    if doc.get("is_no_work_day") or doc.get("no_work_reason") or doc.get("no_work_note"):
        reason = _words(doc.get("no_work_reason"))
        body_parts.append(clean("No work day" + (f" ({reason})" if reason else "") + ":",
                                _text(doc.get("no_work_note"))).rstrip(":"))
    body_parts.append(_labelled("Occupied areas", doc.get("occupied_areas")))
    # "No work day" alone is still worth finding; weather alone is not.
    body = "\n".join(p for p in body_parts if p)

    chunks: list[Chunk] = []
    for piece in _split(body):
        chunks.append(Chunk(
            kind="daily_report", header=header, body=piece, date=rdate, status=status,
            title=f"Daily report {rdate or ''}".strip(), url=_report_url(rid),
        ))
    mp = await _manpower_chunk(ctx, doc, pname)
    if mp:
        chunks.append(mp)
    return ChunkResult(tenant_id=tid, project_id=pid, source_id=rid, chunks=chunks)


# ---------------------------------------------------------------------------
# daily_report_activities (no tenant_id / project_id on the row)
# ---------------------------------------------------------------------------

async def chunk_dr_activity(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    aid = doc.get("id")
    report = await _report(ctx, doc.get("daily_report_id"))
    if not report or not report.get("tenant_id"):
        return _empty(None, None, aid)  # orphan: its report is gone
    tid, pid = report.get("tenant_id"), report.get("project_id")
    if _is_deleted(doc):
        return _empty(tid, pid, aid)
    pname = await _project(ctx, pid, tid)
    if pname is None:
        return _empty(tid, pid, aid)

    body = "\n".join(p for p in (
        _text(doc.get("notes")),
        _labelled("Delay", doc.get("delay_reason")),
        _labelled("Constraint", doc.get("constraint_other_description")),
    ) if p)
    if not body:
        return _empty(tid, pid, aid)  # only the activity name: the task chunk covers it

    code = clean(doc.get("activity_code"))
    trade = clean(doc.get("trade_code"))
    if not trade or not code:
        t = await _task(ctx, doc.get("schedule_activity_id"), tid)
        trade = trade or clean((t or {}).get("trade_code"))
        code = code or clean((t or {}).get("activity_code"))
    rdate = iso_date(report.get("report_date"))
    p0, p1 = _fmt_num(doc.get("pct_complete_start_of_day")), _fmt_num(doc.get("pct_complete_end_of_day"))
    status = doc.get("status_at_end_of_day")
    desc = clean(doc.get("activity_description"))
    header = _header(
        pname, f"Daily report {rdate or ''}".strip(),
        clean("Activity", code, desc),
        f"trade {trade}" if trade else "",
        f"{p0}%->{p1}%" if (p0 or p1) else "",
        _words(status),
    )
    chunk = Chunk(
        kind="dr_activity", header=header, body=body, date=rdate,
        activity_code=[code] if code else [], trade_code=[trade] if trade else [],
        status=status, parent_id=report.get("id"),
        title=clean(f"Activity {code}" if code else "Activity", "-", desc or "", f"({rdate})" if rdate else ""),
        url=_report_url(report.get("id"), code, "dr") if code else _report_url(report.get("id")),
    )
    return ChunkResult(tenant_id=tid, project_id=pid, source_id=aid, chunks=[chunk])


# ---------------------------------------------------------------------------
# daily_presence and daily_report_attendance: roll up into the report
# ---------------------------------------------------------------------------

async def chunk_daily_presence(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    rid = doc.get("daily_report_id")
    tid, pid = doc.get("tenant_id"), doc.get("project_id")
    if not rid:
        return _empty(tid, pid, doc.get("id"))
    if not pid:
        report = await _report(ctx, rid)
        pid = (report or {}).get("project_id")
    return _empty(tid, pid, doc.get("id"), requeue=[("daily_reports", {"id": rid})])


async def chunk_attendance(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    rid, tid = doc.get("daily_report_id"), doc.get("tenant_id")
    report = await _report(ctx, rid)
    if not report:
        return _empty(tid, None, doc.get("id"))
    return _empty(tid or report.get("tenant_id"), report.get("project_id"), doc.get("id"),
                  requeue=[("daily_reports", {"id": rid})])


# ---------------------------------------------------------------------------
# deliveries, safety_events, qc_issues (own tenant_id and project_id)
# ---------------------------------------------------------------------------

async def _event_scope(ctx: ChunkContext, doc: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(tenant_id, project_id, project name or None when orphan/deleted)."""
    tid, pid = doc.get("tenant_id"), doc.get("project_id")
    if not tid or _is_deleted(doc):
        return tid, pid, None
    return tid, pid, await _project(ctx, pid, tid)


async def _filed_on(ctx: ChunkContext, doc: dict, tenant_id: Optional[str]) -> Optional[str]:
    """Id of the live report this record was filed on (for the link), else None.

    Deliberately NOT used as the chunk's parent_id: when a report is hard
    deleted the worker also removes every chunk whose parent_id is that
    report, but deliveries, safety events, quality issues and photos outlive
    their report, so their search entries must not go with it."""
    for key in ("daily_report_id", "parent_daily_report_id"):
        report = await _live_report(ctx, doc.get(key), tenant_id)
        if report:
            return report.get("id")
    return None


async def chunk_delivery(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    did = doc.get("id")
    tid, pid, pname = await _event_scope(ctx, doc)
    if pname is None:
        return _empty(tid, pid, did)
    # received_by holds a person's name or id, so it stays out.
    body = "\n".join(p for p in (
        _text(doc.get("description")),
        _labelled("Notes", doc.get("notes")),
        _labelled("Late entry reason", doc.get("supplement_reason") or doc.get("add_reason")),
    ) if p)
    vendor = _scrub(clean(doc.get("vendor")))
    if not body and not _meaningful(vendor):
        return _empty(tid, pid, did)
    ddate = iso_date(doc.get("delivery_date"))
    codes = await _codes_for_tasks(ctx, doc.get("linked_task_ids") or [], tid)
    if not codes:
        t = await _task(ctx, doc.get("schedule_activity_id"), tid)
        if t and t.get("activity_code"):
            codes = [str(t["activity_code"])]
    po = clean(doc.get("po_number"))
    status = doc.get("status")
    header = _header(pname, clean("Delivery", ddate or ""), vendor, f"PO {po}" if po else "",
                     _words(status), f"activities {', '.join(codes)}" if codes else "")
    rid = await _filed_on(ctx, doc, tid)
    chunk = Chunk(
        kind="delivery", header=header, body=body or vendor, date=ddate, activity_code=codes,
        status=status, parent_id=None,
        title=clean("Delivery", "-", vendor or "", f"({ddate})" if ddate else ""),
        url=_report_url(rid, did, "delivery") if rid else "/deliveries",
    )
    return ChunkResult(tenant_id=tid, project_id=pid, source_id=did, chunks=[chunk])


async def chunk_safety_event(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    sid = doc.get("id")
    tid, pid, pname = await _event_scope(ctx, doc)
    if pname is None:
        return _empty(tid, pid, sid)
    body = "\n".join(p for p in (
        _text(doc.get("description")),
        _labelled("Closed", doc.get("closed_note")),
        _labelled("Late entry reason", doc.get("supplement_reason") or doc.get("add_reason")),
    ) if p)
    if not body:
        return _empty(tid, pid, sid)
    sdate = iso_date(doc.get("date"))
    codes = await _codes_for_tasks(ctx, doc.get("linked_task_ids") or [], tid)
    etype = _words(doc.get("event_type"))
    severity = _words(doc.get("severity"))
    status = doc.get("status")
    header = _header(pname, clean("Safety", etype, sdate or ""), severity, _words(status),
                     f"activities {', '.join(codes)}" if codes else "")
    rid = await _filed_on(ctx, doc, tid)
    chunk = Chunk(
        kind="safety_event", header=header, body=body, date=sdate, activity_code=codes,
        status=status, parent_id=None,
        title=clean("Safety", etype or "event", f"({sdate})" if sdate else ""),
        url=_report_url(rid, sid, "safety") if rid else "/safety-events",
    )
    return ChunkResult(tenant_id=tid, project_id=pid, source_id=sid, chunks=[chunk])


async def chunk_qc_issue(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    qid = doc.get("id")
    tid, pid, pname = await _event_scope(ctx, doc)
    if pname is None:
        return _empty(tid, pid, qid)
    # ai_benchmark_basis is the AI's wording, not a person's, so it is left out.
    body = "\n".join(p for p in (
        _text(doc.get("description")),
        _labelled("Resolution", doc.get("resolution_notes")),
        _labelled("Closed", doc.get("closed_note")),
        _labelled("Rework estimate basis", doc.get("rework_duration_basis")),
        _labelled("Rework outcome", doc.get("rework_outcome_notes")),
        _labelled("Late entry reason", doc.get("supplement_reason") or doc.get("add_reason")),
    ) if p)
    if not body:
        return _empty(tid, pid, qid)
    qdate = iso_date(doc.get("date"))
    codes = await _codes_for_tasks(ctx, doc.get("linked_task_ids") or [], tid)
    severity = _words(doc.get("severity"))
    status = doc.get("status")
    header = _header(pname, clean("Quality issue", qdate or ""), severity, _words(status),
                     f"activities {', '.join(codes)}" if codes else "")
    rid = await _filed_on(ctx, doc, tid)
    chunk = Chunk(
        kind="qc_issue", header=header, body=body, date=qdate, activity_code=codes,
        status=status, parent_id=None,
        title=clean("Quality issue", f"({qdate})" if qdate else ""),
        url=_report_url(rid, qid, "qc") if rid else "/qc-issues",
    )
    return ChunkResult(tenant_id=tid, project_id=pid, source_id=qid, chunks=[chunk])


# ---------------------------------------------------------------------------
# photos (+ the tray row's AI caption), photo_tray, photo_reviews
# ---------------------------------------------------------------------------

def _is_dev_stub(row: Optional[dict]) -> bool:
    """Made-up test results from the photo AI's dev stub must never be indexed
    (same rule the screens use: services/photo_ai.py:649-664)."""
    if not isinstance(row, dict):
        return False
    try:
        from services.photo_ai import is_dev_stub_result
        return bool(is_dev_stub_result(row))
    except Exception:  # pragma: no cover - fallback if the module cannot load
        if row.get("ai_model_version") == "dev-stub-v1":
            return True
        raw = row.get("ai_raw_response")
        if isinstance(raw, dict) and raw.get("_dev_mode"):
            return True
        text = row.get("text")
        return isinstance(text, str) and text.strip().startswith("(Dev stub)")


_PHOTO_LINKS = (
    ("delay_id", "delay"), ("rfi_id", "RFI"), ("qc_issue_id", "quality issue"),
    ("safety_event_id", "safety event"), ("delivery_id", "delivery"),
    ("coordination_event_id", "coordination event"),
)


async def chunk_photo(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    phid = doc.get("id")
    tid, pid, pname = await _event_scope(ctx, doc)
    if pname is None:
        return _empty(tid, pid, phid)

    caption = _text(doc.get("caption"))
    parts = [caption, _labelled("Tag note", doc.get("tag_caption"))]
    tags = [clean(t) for t in (doc.get("tags") or []) if isinstance(t, str) and _meaningful(t)]
    if tags:
        parts.append("Tags: " + _scrub(", ".join(tags)))
    tray = None
    if doc.get("tray_photo_id"):
        tray = await ctx.one("photo_tray", {"id": doc["tray_photo_id"], "tenant_id": tid}, {
            "ai_suggested_caption": 1, "ai_safety_concerns_detected": 1, "ai_model_version": 1,
            "ai_raw_response._dev_mode": 1, "ai_analysis_status": 1, "ai_user_overridden": 1,
        })
    if tray and not _is_dev_stub(tray) and tray.get("ai_analysis_status") != "rejected_by_user":
        # When the person overrode the AI and wrote their own caption, theirs wins.
        if not (tray.get("ai_user_overridden") and caption):
            parts.append(_labelled("AI caption", tray.get("ai_suggested_caption")))
        concerns = [clean(c) for c in (tray.get("ai_safety_concerns_detected") or [])
                    if isinstance(c, str) and _meaningful(c)]
        if concerns:
            parts.append("AI safety concerns: " + _scrub("; ".join(concerns)))
    body = "\n".join(p for p in parts if p)
    if not body:
        return _empty(tid, pid, phid)

    # The report is only used for the link and the date (see _filed_on for
    # why it is not the parent_id).
    rid = await _filed_on(ctx, doc, tid)
    report = await _live_report(ctx, rid, tid) if rid else None
    pdate = iso_date(doc.get("captured_at")) or iso_date((report or {}).get("report_date"))
    code = ""
    t = await _task(ctx, doc.get("schedule_activity_id"), tid)
    if t and t.get("activity_code"):
        code = str(t["activity_code"])
    elif doc.get("daily_report_activity_id"):
        # Activity rows carry no company, so check the company through the
        # activity's own report before trusting its code.
        a = await ctx.one("daily_report_activities", {"id": doc["daily_report_activity_id"]},
                          {"activity_code": 1, "daily_report_id": 1})
        if a and await _live_report(ctx, a.get("daily_report_id"), tid):
            code = clean(a.get("activity_code"))
    linked = [label for key, label in _PHOTO_LINKS if doc.get(key)]
    pclass = _words(doc.get("photo_class"))
    rdate = iso_date((report or {}).get("report_date"))
    header = _header(
        pname, clean("Photo", pclass, pdate or ""),
        f"report {rdate}" if rdate and rdate != pdate else "",
        f"activity {code}" if code else "",
        f"linked {', '.join(linked)}" if linked else "",
    )
    chunk = Chunk(
        kind="photo", header=header, body=body, date=pdate,
        activity_code=[code] if code else [], status=doc.get("status"),
        parent_id=None,
        title=clean("Photo", pclass, f"({pdate})" if pdate else ""),
        url=_report_url(rid, phid, "photo") if rid else "/photos",
    )
    return ChunkResult(tenant_id=tid, project_id=pid, source_id=phid, chunks=[chunk])


async def chunk_photo_tray(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    """Tray rows never make chunks of their own (unfiled uploads, AI-written
    text). A change to the AI caption or safety concerns re-builds the filed
    photos that came from this tray row."""
    trid = doc.get("id")
    requeue = [("photos", {"tray_photo_id": trid})] if trid else []
    return _empty(doc.get("tenant_id"), doc.get("project_id"), trid, requeue=requeue)


async def chunk_photo_review(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    rvid, tid = doc.get("id"), doc.get("tenant_id")
    report = await _report(ctx, doc.get("daily_report_id"))
    if not report or (tid and report.get("tenant_id") and report.get("tenant_id") != tid):
        return _empty(tid, None, rvid)  # orphan: report (or company) is gone
    tid = tid or report.get("tenant_id")
    pid = report.get("project_id")
    if _is_deleted(doc) or _is_dev_stub(doc):
        return _empty(tid, pid, rvid)
    pname = await _project(ctx, pid, tid)
    if pname is None:
        return _empty(tid, pid, rvid)

    lines = [_text(doc.get("label")), _text(doc.get("text"))]
    for key, label in (("safety_findings", "Safety finding"), ("qc_findings", "Quality finding")):
        for f in doc.get(key) or []:
            if isinstance(f, str):
                issue, sev, code = f, "", ""
            elif isinstance(f, dict):
                issue = f.get("issue") or f.get("description") or f.get("text") or ""
                sev, code = _words(f.get("severity")), clean(f.get("osha_code"))
            else:
                continue
            t = _text(issue)
            if t:
                extra = ", ".join(p for p in (f"OSHA {code}" if code else "", sev) if p)
                lines.append(f"{label}: {t}" + (f" ({extra})" if extra else ""))
    body = "\n".join(p for p in lines if p)
    if not body:
        return _empty(tid, pid, rvid)
    rdate = iso_date(report.get("report_date"))
    lens = _words(doc.get("lens"))
    severity = _words(doc.get("severity"))
    header = _header(pname, clean(f"Photo review ({lens})" if lens else "Photo review",
                                  f"report {rdate}" if rdate else ""), severity)
    chunk = Chunk(
        kind="photo_review", header=header, body=body,
        date=rdate or iso_date(doc.get("created_at")), status=doc.get("severity"),
        # Keep parent_id = the report. Deleting a report does not touch its
        # photo_reviews rows, so nothing would ever re-chunk the review; the
        # worker's child purge (chunks whose parent_id is the deleted report)
        # is what removes this chunk. The same holds for dr_activity and
        # manpower chunks, which really belong to the report.
        parent_id=report.get("id"),
        title=clean("Photo review", f"({rdate})" if rdate else ""),
        url=_report_url(report.get("id")),
    )
    return ChunkResult(tenant_id=tid, project_id=pid, source_id=rvid, chunks=[chunk])


# ---------------------------------------------------------------------------
# Registry entries for this group
# ---------------------------------------------------------------------------

ENTRIES: list[Entry] = [
    Entry("daily_reports", "index",
          "The day's record: notes (voice intake appends here), no-work note and occupied areas. "
          "Also carries the per-report manpower roll-up chunk.", chunk_daily_report),
    Entry("daily_report_activities", "index",
          "Per-activity field notes and delay reasons, the richest field text. Rows have no "
          "tenant or project, so scope comes from the parent report; orphans are skipped.",
          chunk_dr_activity),
    Entry("daily_presence", "index",
          "Manpower rows. They make no chunks of their own; every change rebuilds the parent "
          "report's single manpower chunk.", chunk_daily_presence),
    Entry("daily_report_attendance", "index",
          "Attendance notes fold into the parent report's manpower chunk; worker names are "
          "personal data and are never embedded. No chunks of its own.", chunk_attendance),
    Entry("deliveries", "index",
          "Vendor, description and purchase order number are what people look up "
          "('when did the duct arrive'). Who received it stays out.", chunk_delivery),
    Entry("safety_events", "index",
          "Safety incident descriptions; visible to the same people as today.", chunk_safety_event),
    Entry("qc_issues", "index",
          "Quality issue description, resolution and rework outcome notes.", chunk_qc_issue),
    Entry("photos", "index",
          "Filed photos with any caption, tags, or the tray row's real AI caption and safety "
          "concerns. Photos with no text, deleted photos and dev-stub AI text are skipped.",
          chunk_photo),
    Entry("photo_tray", "index",
          "Watched only so AI caption changes rebuild the filed photos that came from it. Unfiled "
          "tray uploads never make chunks (machine-written text, possibly dev-stub results).",
          chunk_photo_tray),
    Entry("photo_reviews", "index",
          "Saved AI safety and quality reviews of a report's photos, with findings and OSHA codes. "
          "Project comes from the parent report; orphans and dev-stub reviews are skipped.",
          chunk_photo_review),
    Entry("voice_annotations", "exclude",
          "Speech-to-text is not shipped in this version; voice text that matters is already "
          "appended to the daily report notes. Recordings and transcripts are personal data."),
    Entry("photo_ai_calibration", "exclude",
          "Admin-only pairs of AI reading and user correction used to tune the photo AI. The "
          "corrected caption lives on the photo and the reason on the tray row."),
    Entry("edit_requests", "exclude",
          "Requests to unlock a submitted daily report: an approval workflow, not field content."),
]
