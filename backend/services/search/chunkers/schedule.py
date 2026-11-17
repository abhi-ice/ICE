"""Search chunkers for the schedule group.

Each chunker turns ONE source document into search chunks (see
services/search/types.py for the contract). The change-stream listener
queues every write on an indexed collection; the worker loads the row and
calls the chunker here. Deletes are handled by the worker (it removes chunks
by the row's Mongo _id), so a chunker only has to say "no chunks" for rows
that are deleted, soft-deleted, placeholders or orphans.

Collections covered and why:
  * tasks               - index: schedule activities are the core thing
                          people look up.
  * schedule_proposals  - index: one small "import summary" per schedule
                          upload (the review summary, flags and the
                          reviewer's notes). Never the parsed rows.
  * mitigations         - index: recovery scenarios for a delay, with the
                          decision and outcome notes.
  * recovery_reports    - index: narrative plus one chunk per recovery
                          action, limited to the roles that can open them.
  * everything else here is query-only or excluded (reasons in ENTRIES).
"""
from __future__ import annotations

import re
from typing import Any, Optional

from services.search.types import (
    Chunk, ChunkContext, ChunkResult, Entry, clean, iso_date,
)


# ---------------------------------------------------------------- helpers

# Text that must never reach the embedded text, even when someone typed it
# into a note or the AI wrote it into a narrative.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
_PHONE_RE = re.compile(
    r"(?<![\w-])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\w-])")
# Ten (or eleven, with a leading 1) digits with no separators, such as
# "2125550199". Digits touching letters, hyphens or other digits are left
# alone, so activity codes such as VA6B-1680 survive.
_PHONE_BARE_RE = re.compile(r"(?<![\w-])1?\d{10}(?![\w-])")
# Dollar amounts in the usual spellings: "$2,500", "US$2.5k", "USD 2,500",
# "2,500 USD", "2.5k USD", "2,500 dollars".
_MONEY_RE = re.compile(
    r"(?:US)?\$\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:k|m|mm|million|thousand)\b)?"
    r"|\bUSD\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:k|m|mm|million|thousand)\b)?"
    r"|\b\d[\d,]*(?:\.\d+)?\s?(?:(?:k|m|mm|million|thousand)\s?)?(?:USD|dollars?)\b",
    re.I)

# About 1,500 tokens. Longer bodies are split on paragraph breaks.
_MAX_BODY_CHARS = 6000


def _scrub(text: Any) -> str:
    """Plain text with emails, links, phone numbers and dollar amounts
    taken out, whitespace collapsed."""
    if text is None:
        return ""
    s = str(text)
    s = _EMAIL_RE.sub("[email removed]", s)
    s = _URL_RE.sub("[link removed]", s)
    s = _PHONE_RE.sub("[phone removed]", s)
    s = _PHONE_BARE_RE.sub("[phone removed]", s)
    s = _MONEY_RE.sub("[amount removed]", s)
    return " ".join(s.split())


def _scrub_keep_paragraphs(text: Any) -> str:
    """Like _scrub, but keeps blank-line paragraph breaks so long text can
    be split at them."""
    if text is None:
        return ""
    paras = re.split(r"\n\s*\n", str(text))
    return "\n\n".join(p for p in (_scrub(x) for x in paras) if p)


def _words(v: Any) -> str:
    """'not_started' -> 'not started'."""
    return str(v).replace("_", " ").strip() if v else ""


def _split_body(body: str, limit: int = _MAX_BODY_CHARS) -> list[str]:
    """Split a long body at paragraph breaks (then sentences) so each piece
    stays under about 1,500 tokens."""
    body = body.strip()
    if len(body) <= limit:
        return [body] if body else []
    pieces: list[str] = []
    cur = ""
    units: list[str] = []
    for para in re.split(r"\n\s*\n", body):
        if len(para) <= limit:
            units.append(para)
        else:
            units.extend(re.split(r"(?<=[.!?])\s+", para))
    for u in units:
        while len(u) > limit:          # one enormous sentence: hard cut
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(u[:limit])
            u = u[limit:]
        if cur and len(cur) + 2 + len(u) > limit:
            pieces.append(cur)
            cur = u
        else:
            cur = f"{cur}\n\n{u}" if cur else u
    if cur:
        pieces.append(cur)
    return [p.strip() for p in pieces if p.strip()]


def _short(text: Any, n: int = 80) -> str:
    s = _scrub(text)
    return s if len(s) <= n else s[: n - 1].rstrip() + "..."


async def _project(ctx: ChunkContext, tenant_id: Optional[str],
                   project_id: Optional[str]) -> Optional[dict]:
    """The project row, or None when the record is an orphan (no project,
    or the project was deleted - projects are hard-deleted,
    projects_router.py:323)."""
    if not tenant_id or not project_id:
        return None
    return await ctx.one("projects", {"id": project_id, "tenant_id": tenant_id},
                         {"id": 1, "name": 1, "number": 1})


def _project_label(p: dict) -> str:
    return (p.get("name") or p.get("number") or "").strip() or "Project"


def _empty(doc: dict) -> ChunkResult:
    return ChunkResult(tenant_id=doc.get("tenant_id"),
                       project_id=doc.get("project_id"),
                       source_id=doc.get("id"), chunks=[])


def _is_deleted(doc: dict) -> bool:
    return (doc.get("status") == "deleted" or bool(doc.get("deleted_at"))
            or bool(doc.get("is_deleted")))


# ---------------------------------------------------------------- tasks

async def chunk_task(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    """One chunk per schedule activity.

    Deleted activities stay in the collection with status 'deleted'
    (tasks_router.py:474) and may be brought back by a re-import
    (schedule_proposals_router.py:414), so this returns no chunks while
    deleted and a chunk again once undeleted.
    """
    if _is_deleted(doc):
        return _empty(doc)
    proj = await _project(ctx, doc.get("tenant_id"), doc.get("project_id"))
    if not proj:
        return _empty(doc)

    description = _scrub(doc.get("description"))
    display = _scrub(doc.get("display_name"))
    if not description and not display:
        return _empty(doc)   # an activity code alone says nothing searchable

    code = clean(doc.get("activity_code"))
    trade = clean(doc.get("trade_code") or doc.get("required_trade"))
    start = iso_date(doc.get("planned_start"))
    finish = iso_date(doc.get("planned_finish"))
    status = clean(doc.get("status")) or None
    name = description or display

    when = ""
    if start and finish and start != finish:
        when = f"{start} to {finish}"
    elif start or finish:
        when = start or finish or ""
    header = clean(
        _project_label(proj), "|",
        clean("Activity", code, name, when), "|",
        clean(f"trade {trade}" if trade else "", f"status {_words(status)}" if status else "",
              sep=" | "),
        sep=" ",
    ).rstrip(" |")

    lines = [name]
    if display and display != description and description:
        lines.append(f"Also called: {display}")
    path = [_scrub(p) for p in (doc.get("section_path") or []) if _scrub(p)]
    if path:
        lines.append("Section: " + " > ".join(path))
    if doc.get("wbs"):
        lines.append(f"WBS {_scrub(doc.get('wbs'))}")
    resources = [_scrub(r) for r in (doc.get("resource_names") or []) if _scrub(r)]
    if resources:
        lines.append("Resources: " + ", ".join(resources))
    if doc.get("is_milestone"):
        lines.append("Milestone")

    chunk = Chunk(
        kind="task",
        header=header,
        body="\n".join(lines),
        date=start or finish,
        activity_code=[code] if code else [],
        trade_code=[trade] if trade else [],
        status=status,
        title=clean("Activity", code, "-" if code else "", _short(name, 60)),
        # Same deep link the approvals queue uses for an activity
        # (frontend/src/pages/Approvals.jsx:192).
        url=f"/tasks?openId={doc.get('id')}",
    )
    return ChunkResult(tenant_id=doc.get("tenant_id"),
                       project_id=doc.get("project_id"),
                       source_id=doc.get("id"), chunks=[chunk])


# ------------------------------------------------------ schedule_proposals

async def chunk_schedule_proposal(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    """One small "import summary" per schedule upload: the review summary and
    flags plus the reviewer's own notes. The parsed rows and the diff lists
    repeat what the task chunks already hold, so they are never embedded.
    A review produced by the no-AI fallback (model 'heuristic',
    services/schedule_ai_review.py:147) is just counts, so it is left out.
    """
    if _is_deleted(doc):
        return _empty(doc)
    proj = await _project(ctx, doc.get("tenant_id"), doc.get("project_id"))
    if not proj:
        return _empty(doc)

    review = doc.get("ai_review") if isinstance(doc.get("ai_review"), dict) else {}
    use_ai = bool(review) and review.get("model") != "heuristic" and not review.get("ai_error")
    summary = _scrub_keep_paragraphs(review.get("summary")) if use_ai else ""
    flags = [_scrub(f) for f in (review.get("flags") or []) if use_ai and _scrub(f)]
    notes = _scrub_keep_paragraphs(doc.get("review_notes"))
    if not (summary or flags or notes):
        return _empty(doc)

    filename = _scrub(doc.get("filename")) or "schedule file"
    date = iso_date(doc.get("uploaded_at")) or iso_date(doc.get("created_at"))
    status = clean(doc.get("status")) or None
    header = clean(_project_label(proj), "|", "Schedule import", filename, date or "",
                   "|", f"status {_words(status)}" if status else "").rstrip(" |")

    parts = []
    if summary:
        parts.append(f"Review summary: {summary}")
    if flags:
        parts.append("Flags: " + "; ".join(flags))
    if use_ai and review.get("recommendation"):
        parts.append(f"Recommendation: {_words(review.get('recommendation'))}")
    if notes:
        parts.append(f"Reviewer notes: {notes}")

    chunks = []
    for body in _split_body("\n\n".join(parts)):
        chunks.append(Chunk(
            kind="schedule_import",
            header=header,
            body=body,
            date=date,
            status=status,
            title=clean("Schedule import", _short(filename, 60)),
            # ScheduleProposals opens one proposal from ?id=
            # (frontend/src/pages/ScheduleProposals.jsx:48).
            url=f"/schedule-changes?id={doc.get('id')}",
        ))
    return ChunkResult(tenant_id=doc.get("tenant_id"),
                       project_id=doc.get("project_id"),
                       source_id=doc.get("id"), chunks=chunks)


# ---------------------------------------------------------------- mitigations

async def chunk_mitigation(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    """One chunk per recovery scenario for a delay.

    Visibility follows the parent delay: the endpoints have no role check
    (mitigations_router.py:31, 79, 180, 297, 384). Delays are hard-deleted
    now and mitigations are not in any delete cascade, so a scenario whose
    delay is gone (or still marked 'deleted' from the old soft-delete path,
    field_events_router.py:567-569) is an orphan and gets no chunks.
    Cost estimates and actual cost are never embedded.
    """
    if _is_deleted(doc):
        return _empty(doc)
    tenant_id = doc.get("tenant_id")
    proj = await _project(ctx, tenant_id, doc.get("project_id"))
    if not proj:
        return _empty(doc)
    delay_id = doc.get("linked_delay_id")
    delay = None
    if delay_id:
        delay = await ctx.one("delays", {"id": delay_id, "tenant_id": tenant_id},
                              {"id": 1, "description": 1, "status": 1,
                               "project_id": 1, "deleted_at": 1, "is_deleted": 1})
    if not delay or _is_deleted(delay):
        return _empty(doc)
    # Who may see a scenario follows its delay (project_access.py resolves a
    # mitigation to its delay). If the two rows ever disagree about the
    # project, filing the chunk under the mitigation's own project could show
    # it to the wrong people, so it gets no chunk.
    if delay.get("project_id") != doc.get("project_id"):
        return _empty(doc)

    prereqs = [_scrub(p) for p in (doc.get("prerequisites") or []) if _scrub(p)]
    parts = [
        ("Strategy", _scrub(doc.get("strategy_name"))),
        ("Proposed action", _scrub(doc.get("proposed_action_description"))),
        ("Assumption", _scrub(doc.get("assumption"))),
        ("Prerequisites", "; ".join(prereqs)),
        ("Reasoning", _scrub(doc.get("ai_reasoning"))),
        ("Decision rationale", _scrub(doc.get("decision_rationale"))),
        ("Outcome", _scrub(doc.get("outcome_notes"))),
    ]
    body = "\n".join(f"{k}: {v}" for k, v in parts if v)
    if not body:
        return _empty(doc)

    codes: list[str] = []
    trades: list[str] = []
    for tid in doc.get("linked_task_ids") or []:
        t = await ctx.one("tasks", {"id": tid, "tenant_id": tenant_id},
                          {"activity_code": 1, "trade_code": 1, "status": 1})
        if not t or t.get("status") == "deleted":
            continue
        if t.get("activity_code") and str(t["activity_code"]) not in codes:
            codes.append(str(t["activity_code"]))
        if t.get("trade_code") and str(t["trade_code"]) not in trades:
            trades.append(str(t["trade_code"]))

    label = clean(doc.get("scenario_label"))
    date = iso_date(doc.get("proposed_at")) or iso_date(doc.get("created_at"))
    status = clean(doc.get("decision_status")) or None
    delay_text = _short(delay.get("description"), 70)
    header = clean(
        _project_label(proj), "|",
        clean("Mitigation", label, "for delay", f'"{delay_text}"' if delay_text else "", date or ""),
        "|", clean(f"status {_words(status)}" if status else "",
                   f"activities {', '.join(codes)}" if codes else "", sep=" | "),
    ).rstrip(" |")

    chunks = [Chunk(
        kind="mitigation",
        header=header,
        body=b,
        date=date,
        activity_code=codes,
        trade_code=trades,
        status=status,
        parent_id=delay_id,
        title=clean("Mitigation", label, "-", _short(doc.get("strategy_name"), 50)),
        # The delay detail page lists its scenarios
        # (frontend/src/pages/DelayDetail.jsx:308; route App.js:97).
        url=f"/delays/{delay_id}",
    ) for b in _split_body(body)]
    return ChunkResult(tenant_id=tenant_id, project_id=doc.get("project_id"),
                       source_id=doc.get("id"), chunks=chunks)


# ---------------------------------------------------------------- recovery_reports

# Who may open a recovery report today: _ROLE_VIEW at
# routers/recovery_reports_router.py:38 (checked at :280, :298, :378).
# submittal_tech and scheduling_engineer are left out on purpose.
# Search must ALSO check project membership for these chunks even when
# project access is not enforced (recovery_reports_router.py:45-83).
RECOVERY_REPORT_ROLES = ["admin", "foreman", "pe", "pm", "px", "super", "viewer"]

_RECOVERY_TYPE_WORDS = {
    "weekly_variance": "weekly variance",
    "friday_recovery": "Friday recovery",
    "daily_variance": "daily variance",
    "project_health_summary": "project health summary",
    "custom": "custom",
}


async def chunk_recovery_report(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    """Narrative chunk(s) plus one chunk per recovery action.

    Only draft and finalized reports are indexed; archive is the soft
    delete (services/recovery_report.py:956-982). The "generating..."
    placeholder row (_placeholder, recovery_reports_router.py:190) is
    skipped. Storage keys, the data snapshot and the AI cost are never
    embedded.
    """
    if doc.get("_placeholder") or doc.get("status") not in ("draft", "finalized"):
        return _empty(doc)
    tenant_id = doc.get("tenant_id")
    proj = await _project(ctx, tenant_id, doc.get("project_id"))
    if not proj:
        return _empty(doc)

    rtype = _RECOVERY_TYPE_WORDS.get(doc.get("report_type") or "", _words(doc.get("report_type")))
    ps, pe = iso_date(doc.get("period_start")), iso_date(doc.get("period_end"))
    period = f"{ps} to {pe}" if ps and pe and ps != pe else (ps or pe or "")
    date = pe or ps or iso_date(doc.get("generated_at"))
    status = clean(doc.get("status")) or None
    label = _scrub(doc.get("audit_op_label"))
    title = label or clean("Recovery report", rtype, period)
    base = clean(_project_label(proj), "|", "Recovery report", rtype,
                 period or (date or ""))
    # NOTE: today the Weekly reports page (frontend/src/pages/SystemPages.jsx,
    # WeeklyReports) always opens on its "Weekly rollup" tab and reads neither
    # ?tab= nor ?openId=, so this link only reaches /weekly-reports. The
    # parameters are kept so the link opens the right report once that page
    # learns to read them.
    url = f"/weekly-reports?tab=generator&openId={doc.get('id')}"
    roles = list(RECOVERY_REPORT_ROLES)

    chunks: list[Chunk] = []
    narrative = _scrub_keep_paragraphs(doc.get("ai_narrative"))
    for body in _split_body(narrative):
        chunks.append(Chunk(
            kind="recovery_report",
            header=clean(base, "|", f"status {status}" if status else "").rstrip(" |"),
            body=body, date=date, status=status, allowed_roles=roles,
            title=title, url=url,
        ))
    for i, a in enumerate(doc.get("recovery_actions") or []):
        if not isinstance(a, dict):
            continue
        action = _scrub(a.get("action"))
        if not action:
            continue
        lines = [f"Action: {action}"]
        if _scrub(a.get("rationale")):
            lines.append(f"Rationale: {_scrub(a.get('rationale'))}")
        if _scrub(a.get("owner")):
            lines.append(f"Owner: {_scrub(a.get('owner'))}")
        if iso_date(a.get("due_date")):
            lines.append(f"Due {iso_date(a.get('due_date'))}")
        chunks.append(Chunk(
            kind="recovery_action",
            header=clean(base, "|", f"action {i + 1}", "|",
                         f"status {status}" if status else "").rstrip(" |"),
            body="\n".join(lines), date=date, status=status,
            parent_id=doc.get("id"), allowed_roles=roles,
            title=clean(title, "- action", str(i + 1)), url=url,
        ))
    return ChunkResult(tenant_id=tenant_id, project_id=doc.get("project_id"),
                       source_id=doc.get("id"), chunks=chunks)


# ---------------------------------------------------------------- registry entries

ENTRIES: list[Entry] = [
    Entry("tasks", "index",
          "Schedule activities are what people look up most; short human-written "
          "names, section path, WBS and trade. Deleted activities get no chunk.",
          chunk_task),
    Entry("schedule_proposals", "index",
          "One small import summary per schedule upload (review summary, flags, "
          "reviewer notes); the parsed rows repeat the task chunks and are never embedded.",
          chunk_schedule_proposal),
    Entry("mitigations", "index",
          "Recovery scenarios for a delay with the decision rationale and outcome "
          "notes; orphans (delay gone) get no chunk, cost fields never embedded.",
          chunk_mitigation),
    Entry("recovery_reports", "index",
          "Recovery narrative and actions; only draft/finalized, limited to the roles "
          "in recovery_reports_router.py:38 (not submittal_tech or scheduling_engineer).",
          chunk_recovery_report),
    Entry("schedule_update_diffs", "query_only",
          "Lists of changed activity codes and counts between schedule versions; the "
          "AI narrative is empty in practice and 'what changed' is a structured question."),
    Entry("logic_proposals", "query_only",
          "Short-lived reviewer queue of machine-written predecessor suggestions, "
          "limited to review roles (schedule_review_router.py:33); repeats task names."),
    Entry("activity_name_suggestions", "query_only",
          "Reviewer queue of proposed activity names; approved names are written onto "
          "the task, which is indexed. Review roles only (schedule_review_router.py:33)."),
    Entry("ai_recovery_suggestions", "query_only",
          "Speculative AI option lists, mostly never applied; surfacing them as project "
          "record would mislead. The assistant can still query them."),
    Entry("project_calendar_days", "query_only",
          "One row per project per day with working/holiday/closed flags only (notes "
          "empty); 'is the site closed on a date' is an exact lookup."),
    Entry("imports", "query_only",
          "Schedule upload history: filename and row counts only; the summary text "
          "lives on schedule_proposals, which is indexed."),
    Entry("tia_generations", "exclude",
          "Delay-analysis run records marked counsel-privileged (models.py:3266, 3323); "
          "no searchable prose, and indexing could expose privileged work."),
    Entry("cascade_snapshots", "exclude",
          "Computed numeric history of projected slip, rewritten on every recompute; no human text."),
    Entry("task_sync_events", "exclude",
          "System log of daily-report-to-activity progress syncs (before/after numbers); "
          "no human text, the activity chunk already shows the result."),
    Entry("schedule_jobs", "exclude",
          "MS Project worker job queue (ms_project_sync.py); system plumbing, no human text."),
    Entry("schedule_job_seq", "exclude",
          "Counter for MS Project worker jobs; system plumbing."),
    Entry("schedule_job_watermark", "exclude",
          "MS Project worker bookkeeping; system plumbing."),
    Entry("ms_project_sync_state", "exclude",
          "MS Project sync status per project (file pointers, timestamps); system plumbing."),
    # 'calendars' is listed in docs_company.py, so it is not repeated here.
    Entry("mitigation_outcomes", "exclude",
          "Rework notes written by the quality-control cascade "
          "(qc_rework_cascade.py:240); they repeat the quality-control records."),
    Entry("work_calendars", "exclude",
          "Per-activity work-week and holiday settings read by the MS Project impact "
          "math (ms_project_sync.py:394); numbers and dates only."),
    Entry("resource_levelling_gaps", "exclude",
          "Computed crew-shortfall rows per activity (trade, date, size of gap) read "
          "only by the task export (tasks_exports.py:166); no human text."),
    Entry("field_activity_code_counters", "exclude",
          "Counter used to number activities added from the field; no content."),
]
