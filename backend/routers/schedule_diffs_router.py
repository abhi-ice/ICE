"""Bucket C3c Phase 1 — Schedule update diffs router.

Surfaces version-to-version schedule diffs that were auto-generated when
a schedule_proposal flipped to status='approved' (see services.schedule_diff
.auto_diff_on_approval and the BackgroundTask wired in
schedule_proposals_router.approve_proposal).

  GET    /api/projects/{pid}/schedule-diffs           list (date desc)
  GET    /api/schedule-diffs/{id}                     detail
  POST   /api/schedule-diffs/{id}/ai-narrative        generate narrative
  GET    /api/schedule-diffs/{id}/pdf                 WeasyPrint PDF

Tenant-scoped, append-only (diffs are historical record, never edited).
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from typing import Optional
import os
import re
import html

from database import get_db, tenant_filter
from auth import require_tenant
from audit import write_audit
from models import utc_now


router = APIRouter(tags=["schedule-diffs"])


# ============================================================
# List
# ============================================================
@router.get("/projects/{project_id}/schedule-diffs")
async def list_schedule_diffs(project_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    docs = await db.schedule_update_diffs.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id}),
        # Strip the heavy detail arrays from the list view; the detail
        # endpoint serves them.
        {"_id": 0, "activities_added": 0, "activities_removed": 0,
         "activities_modified": 0, "predecessor_changes": 0},
    ).sort("computed_at", -1).to_list(500)
    return docs


# ============================================================
# Detail
# ============================================================
@router.get("/schedule-diffs/{diff_id}")
async def get_schedule_diff(diff_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = await db.schedule_update_diffs.find_one(
        tenant_filter(ctx["tenant_id"], {"id": diff_id}), {"_id": 0},
    )
    if not doc:
        raise HTTPException(404, "Schedule diff not found")
    return doc


# ============================================================
# AI narrative — generated on demand (caches into the row)
# ============================================================
@router.post("/schedule-diffs/{diff_id}/ai-narrative")
async def generate_ai_narrative(diff_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = await db.schedule_update_diffs.find_one(
        tenant_filter(ctx["tenant_id"], {"id": diff_id}), {"_id": 0},
    )
    if not doc:
        raise HTTPException(404, "Schedule diff not found")

    # Build a compact summary for the LLM — the full payload would blow
    # past context on big diffs. Cap each list at 50 representative items.
    def _cap(arr: list, n: int = 50) -> list:
        return list(arr or [])[:n]

    compact = {
        "activities_added_count": doc.get("activities_added_count", 0),
        "activities_removed_count": doc.get("activities_removed_count", 0),
        "activities_modified_count": doc.get("activities_modified_count", 0),
        "predecessors_added_count": doc.get("predecessors_added_count", 0),
        "predecessors_removed_count": doc.get("predecessors_removed_count", 0),
        "cp_changes_count": doc.get("cp_changes_count", 0),
        "total_change_percentage": doc.get("total_change_percentage", 0),
        "newly_critical_activities_sample": _cap(doc.get("newly_critical_activities", []), 25),
        "removed_critical_activities_sample": _cap(doc.get("removed_critical_activities", []), 25),
        "activities_modified_sample": [
            {"code": m.get("activity_code"),
             "duration_change": m.get("duration_change"),
             "start_change": m.get("start_change"),
             "end_change": m.get("end_change")}
            for m in _cap(doc.get("activities_modified", []), 25)
        ],
    }

    api_key = os.environ.get("EMERGENT_LLM_KEY", "")
    if not api_key:
        raise HTTPException(503, "AI narrative unavailable (no LLM key configured)")

    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
    except Exception as e:
        raise HTTPException(503, f"LLM library unavailable: {e}")

    SYS = (
        "You are a senior construction scheduling engineer reviewing a diff "
        "between two approved schedule versions. Produce a plain-language "
        "narrative (under 250 words) covering: (1) the magnitude and shape "
        "of the change, (2) which moved activities are most concerning for "
        "the critical path, (3) what a PM would want to dig into. No "
        "markdown, no bullets, no headings — three short paragraphs."
    )
    import json as _json
    chat = LlmChat(
        api_key=api_key,
        session_id=f"sched-diff-narrative-{diff_id}",
        system_message=SYS,
    ).with_model("anthropic", "claude-sonnet-4-5-20250929")

    try:
        raw = await chat.send_message(UserMessage(text=_json.dumps(compact, default=str)))
    except Exception as e:
        raise HTTPException(503, f"LLM error: {str(e)[:200]}")

    narrative = re.sub(r"[`*#|]+", "", str(raw)).strip()
    now_iso = utc_now().isoformat()
    await db.schedule_update_diffs.update_one(
        tenant_filter(ctx["tenant_id"], {"id": diff_id}),
        {"$set": {
            "ai_narrative": narrative,
            "ai_narrative_generated_at": now_iso,
            "ai_narrative_generated_by": ctx["user"]["id"],
        }},
    )
    await write_audit(
        tenant_id=ctx["tenant_id"], table_name="schedule_update_diffs",
        op="AI_NARRATIVE_GENERATED",
        record_id=diff_id, before=None,
        after={"generated_at": now_iso, "generated_by": ctx["user"]["id"]},
        actor_id=ctx["user"]["id"], request_id=ctx["request_id"],
    )
    return {"ai_narrative": narrative,
            "ai_narrative_generated_at": now_iso,
            "ai_narrative_generated_by": ctx["user"]["id"]}


# ============================================================
# PDF render via WeasyPrint
# ============================================================
def _html_table(rows: list[dict], columns: list[tuple[str, str]]) -> str:
    if not rows:
        return "<p class='empty'>None.</p>"
    head = "".join(f"<th>{html.escape(label)}</th>" for _, label in columns)
    body_parts: list[str] = []
    for r in rows[:500]:  # cap to keep PDF rendering reasonable
        cells: list[str] = []
        for key, _ in columns:
            v = r.get(key)
            if isinstance(v, dict):
                v = f"{v.get('prior')} → {v.get('new')}"
            elif isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            cells.append(f"<td>{html.escape(str(v) if v is not None else '')}</td>")
        body_parts.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_parts)}</tbody></table>"


@router.get("/schedule-diffs/{diff_id}/pdf")
async def schedule_diff_pdf(diff_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = await db.schedule_update_diffs.find_one(
        tenant_filter(ctx["tenant_id"], {"id": diff_id}), {"_id": 0},
    )
    if not doc:
        raise HTTPException(404, "Schedule diff not found")
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": doc["project_id"]}),
        {"_id": 0, "name": 1},
    ) or {}

    css = """
      body { font-family: Helvetica, Arial, sans-serif; font-size: 10pt; color: #1a2845; }
      h1 { font-size: 18pt; margin-bottom: 4pt; }
      h2 { font-size: 12pt; border-bottom: 1px solid #d97757; padding-bottom: 2pt; margin-top: 14pt; }
      table { border-collapse: collapse; width: 100%; margin-top: 6pt; }
      th, td { border: 1px solid #ccc; padding: 3pt 5pt; text-align: left; vertical-align: top; }
      th { background: #f5f1ea; font-weight: bold; }
      .meta { color: #666; font-size: 9pt; }
      .stats { display: flex; gap: 12pt; margin-top: 8pt; }
      .stat { background: #f5f1ea; padding: 6pt 10pt; border-left: 3px solid #d97757; }
      .stat .num { font-size: 18pt; font-weight: bold; color: #1a2845; }
      .stat .lbl { font-size: 8pt; color: #666; text-transform: uppercase; }
      .empty { color: #888; font-style: italic; }
      .narrative { background: #fffaf3; border-left: 3px solid #d97757; padding: 8pt 12pt; margin: 10pt 0; font-style: italic; }
    """

    narrative_html = (
        f"<div class='narrative'>{html.escape(doc.get('ai_narrative') or '').replace(chr(10), '<br>')}</div>"
        if doc.get("ai_narrative") else ""
    )

    html_doc = f"""
    <html><head><meta charset='utf-8'><style>{css}</style></head>
    <body>
      <h1>Schedule Update Diff</h1>
      <div class='meta'>
        Project: {html.escape(proj.get('name') or doc.get('project_id'))}<br>
        Computed: {html.escape(str(doc.get('computed_at') or ''))}<br>
        Earlier version: {html.escape(doc.get('earlier_schedule_version_id') or '')}<br>
        Later version: {html.escape(doc.get('later_schedule_version_id') or '')}
      </div>
      <div class='stats'>
        <div class='stat'><div class='num'>{doc.get('activities_added_count', 0)}</div><div class='lbl'>Added</div></div>
        <div class='stat'><div class='num'>{doc.get('activities_removed_count', 0)}</div><div class='lbl'>Removed</div></div>
        <div class='stat'><div class='num'>{doc.get('activities_modified_count', 0)}</div><div class='lbl'>Modified</div></div>
        <div class='stat'><div class='num'>{doc.get('cp_changes_count', 0)}</div><div class='lbl'>CP changes</div></div>
        <div class='stat'><div class='num'>{doc.get('total_change_percentage', 0):.1f}%</div><div class='lbl'>Change rate</div></div>
      </div>

      {narrative_html}

      <h2>Activities added</h2>
      {_html_table(doc.get('activities_added', []),
                   [('activity_code', 'Code'), ('description', 'Description'),
                    ('duration_days', 'Days'), ('start_date', 'Start'),
                    ('end_date', 'End'), ('predecessors', 'Predecessors')])}

      <h2>Activities removed</h2>
      {_html_table(doc.get('activities_removed', []),
                   [('activity_code', 'Code'), ('description', 'Description'),
                    ('duration_days', 'Days'), ('start_date', 'Start'),
                    ('end_date', 'End')])}

      <h2>Activities modified</h2>
      {_html_table(doc.get('activities_modified', []),
                   [('activity_code', 'Code'), ('description_change', 'Description'),
                    ('duration_change', 'Duration'), ('start_change', 'Start'),
                    ('end_change', 'End'), ('status_change', 'Status'),
                    ('pct_change', '% complete')])}

      <h2>Predecessor changes</h2>
      {_html_table(doc.get('predecessor_changes', []),
                   [('successor_id', 'Successor'), ('predecessor_id', 'Predecessor'),
                    ('change_type', 'Change'),
                    ('prior_relationship', 'Prior rel'), ('new_relationship', 'New rel'),
                    ('prior_lag_days', 'Prior lag'), ('new_lag_days', 'New lag')])}

      <h2>Critical path movement</h2>
      <p><strong>Newly critical:</strong> {", ".join(html.escape(c) for c in doc.get('newly_critical_activities', [])) or "<span class='empty'>None.</span>"}</p>
      <p><strong>No longer critical:</strong> {", ".join(html.escape(c) for c in doc.get('removed_critical_activities', [])) or "<span class='empty'>None.</span>"}</p>
    </body></html>
    """

    try:
        from services.pdf_safety import arender_pdf
        pdf = await arender_pdf(html_doc)
    except Exception as e:
        raise HTTPException(500, f"PDF render failed: {str(e)[:200]}")

    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="schedule_diff_{diff_id}.pdf"'},
    )
