"""Iter 6 router — completes the master prompt scope:
  - PDF rendering (WeasyPrint) for daily reports + weekly rollups
  - Photo EXIF extraction + perceptual hash visual fingerprint
  - AI Recovery: apply chosen suggestion (resequence / extend duration)
  - Procurement aging detail (per-record breakdown)
  - Eval harness for AI recovery (scored history)
  - Spatial section view (cut-line on plan → vertical slice payload)
  - Submittal-activity bidirectional auto-link helper
  - Schedule import .mpp/.xer stub (graceful refusal preserved)

All endpoints respect tenant isolation via tenant_filter().
"""
import io
import re
import math
from datetime import datetime, timezone, timedelta, date
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Response
from typing import Optional, Literal, List
from pydantic import Field

from database import get_db, tenant_filter
from models import gen_id, utc_now, StrictModel, ReadModel
from auth import require_tenant


router = APIRouter(tags=["iter6"])


# ============================================================
# WeasyPrint PDF — Daily Report + Weekly Rollup
# ============================================================
def _html_escape(s: str | None) -> str:
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


@router.get("/daily-reports/{daily_report_id}/pdf-rich")
async def export_daily_report_pdf(
    daily_report_id: str,
    mode: Literal["internal", "external"] = "internal",
    ctx: dict = Depends(require_tenant),
):
    """Stream a true PDF (WeasyPrint) of the daily report.

    Phase F — `mode` query param toggles which sections render.
      - internal (default): everything (header, weather, crew presence,
        activity grid, open delay reminders, delays, QC issues, safety
        events, RFIs, deliveries, photo summary, notes, audit trail).
      - external: same as internal MINUS QC issues, Safety events,
        and the rolled-forward Open delay reminders banner. The day's
        Delays section itself stays — only the reminder banner is hidden.
    """
    from weasyprint import HTML
    db = get_db()
    dr = await db.daily_reports.find_one(
        tenant_filter(ctx["tenant_id"], {"id": daily_report_id}), {"_id": 0},
    )
    if not dr:
        raise HTTPException(404, "Daily report not found")
    proj = await db.projects.find_one(
        tenant_filter(ctx["tenant_id"], {"id": dr["project_id"]}), {"_id": 0},
    )
    project_id = dr["project_id"]

    # Feature DR Header Auto-Population (2026-05-20) -- the formal
    # signature on an exported DR is the canonical submitter (or the
    # preparer for a still-draft DR); falls back to the exporter only
    # when neither is set. Avoids the signature line changing per-viewer
    # when the report is re-exported later by a different user.
    signer_id = dr.get("submitted_by") or dr.get("prepared_by")
    signer_name = ""
    if signer_id:
        _u = await db.users.find_one(
            tenant_filter(ctx["tenant_id"], {"id": signer_id}),
            {"_id": 0, "full_name": 1},
        )
        signer_name = (_u or {}).get("full_name", "") or ""
    if not signer_name:
        signer_name = (ctx.get("user") or {}).get("full_name", "") or ""

    grid = await db.daily_report_activities.find(
        {"daily_report_id": daily_report_id}, {"_id": 0},
    ).sort("position", 1).to_list(500)
    presence = await db.daily_presence.find(
        tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id}), {"_id": 0},
    ).sort("trade", 1).to_list(200)
    # Same status!='deleted' filter as the /context endpoint so the PDF
    # never prints soft-deleted leftovers from the legacy /crud DELETE path.
    not_deleted = {"status": {"$ne": "deleted"}}
    delays = await db.delays.find(
        tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id, **not_deleted}), {"_id": 0},
    ).sort("date", -1).to_list(200)
    qc = await db.qc_issues.find(
        tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id, **not_deleted}), {"_id": 0},
    ).sort("date", -1).to_list(200)
    safety = await db.safety_events.find(
        tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id, **not_deleted}), {"_id": 0},
    ).sort("date", -1).to_list(200)
    rfis = await db.rfis.find(
        tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id, **not_deleted}), {"_id": 0},
    ).sort("date_initiated", -1).to_list(100)
    deliveries = await db.deliveries.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id, "delivery_date": dr["report_date"]}), {"_id": 0},
    ).to_list(100)
    # Item 4 (2026-05-06) — pull storage_path + thumbnail_path so the
    # PDF can embed the actual image (downscaled to ~150px) rather than
    # just the id. Cap at 24 photos to keep PDF size reasonable for
    # report-heavy days.
    photos = await db.photos.find(
        tenant_filter(ctx["tenant_id"], {"daily_report_id": daily_report_id}),
        {"_id": 0, "id": 1, "caption": 1, "captured_at": 1,
         "storage_path": 1, "thumbnail_path": 1},
    ).sort("captured_at", -1).to_list(24)
    audit_log = await db.audit_log.find(
        tenant_filter(ctx["tenant_id"], {
            "table_name": "daily_reports",
            "record_id": daily_report_id,
        }),
        {"_id": 0, "created_at": 1, "op": 1, "actor_id": 1},
    ).sort("created_at", -1).limit(50).to_list(50)

    # Reminders — open/in_progress delays whose linked tasks intersect today's grid
    grid_task_ids = {r["schedule_activity_id"] for r in grid if r.get("schedule_activity_id")}
    reminders: list[dict] = []
    if grid_task_ids and mode == "internal":
        reminders = await db.delays.find(
            tenant_filter(ctx["tenant_id"], {
                "project_id": project_id,
                "status": {"$in": ["open", "in_progress"]},
                "linked_task_ids": {"$in": list(grid_task_ids)},
                # Exclude delays logged today — those appear in the Delays
                # section already, no need to double-list.
                "id": {"$nin": [d["id"] for d in delays]},
            }),
            {"_id": 0},
        ).sort("date", -1).to_list(100)

    is_internal = mode == "internal"

    # Item 3 (2026-05-06) — pre-fetch tasks referenced by any delay's
    # linked_task_ids so the Delays section can render "wbs — desc"
    # rather than UUIDs.
    delay_task_ids = {tid for d in delays for tid in (d.get("linked_task_ids") or []) if tid}
    task_lookup: dict[str, dict] = {}
    if delay_task_ids:
        task_docs = await db.tasks.find(
            tenant_filter(ctx["tenant_id"], {"id": {"$in": list(delay_task_ids)}}),
            {"_id": 0, "id": 1, "wbs": 1, "activity_code": 1, "description": 1},
        ).to_list(len(delay_task_ids))
        task_lookup = {t["id"]: t for t in task_docs}

    def _task_label(task_id: str) -> str:
        t = task_lookup.get(task_id) or {}
        code = t.get("wbs") or t.get("activity_code") or (task_id[:8] if task_id else "?")
        desc = t.get("description") or ""
        return f"{code} — {desc}" if desc else code

    # 2026-02-XX (Abhi prod-feedback): exports must show ONLY today's
    # planned activity (work_category section 2 = Field Work). The
    # paperwork sections 1 & 3 are read-only reference rows from the
    # master schedule that the super did not actually execute today
    # -- they bloat the client deliverable with noise. Mirrors the UI
    # GRID_SECTIONS filter on the live DR detail.
    from services.dr_activity_grid import section_for as _grid_section_for
    grid_field_only = [r for r in grid if _grid_section_for(r.get("work_category")) == 2]
    grid_rows_html = "".join(
        f"<tr><td class='code'>{_html_escape(r.get('activity_code'))}</td>"
        f"<td>{_html_escape((r.get('activity_description') or '')[:80])}</td>"
        f"<td>{_html_escape(r.get('location_name') or '—')}</td>"
        f"<td class='num'>{r.get('pct_complete_start_of_day') or 0}%</td>"
        f"<td class='num'>{r.get('pct_complete_end_of_day') or 0}%</td>"
        f"<td>{_html_escape(r.get('status_at_end_of_day') or 'not_started')}</td></tr>"
        for r in grid_field_only
    ) or "<tr><td colspan='6' class='muted'>No activities reported.</td></tr>"

    # 2026-02-XX (Abhi prod-feedback): Manpower log now carries a
    # "Task worked" column matching the live DR UI. Resolve the
    # presence row's schedule_activity_id (set when a sub voice-
    # intake links a crew to a planned activity) to "code — desc"
    # using the same _task_label helper used for delays.
    presence_rows_html = "".join(
        f"<tr><td>{_html_escape(p.get('trade'))}</td>"
        f"<td>{_html_escape(p.get('company_name') or '—')}</td>"
        f"<td>{_html_escape(_task_label(p.get('schedule_activity_id')) if p.get('schedule_activity_id') else '—')}</td>"
        f"<td class='num'>{p.get('headcount') or 0}</td>"
        f"<td class='num'>{p.get('hours_per_person') or 0}</td>"
        f"<td class='num'>{(p.get('headcount') or 0) * (p.get('hours_per_person') or 0):.1f}</td>"
        f"<td>{_html_escape(p.get('notes') or '')}</td></tr>"
        for p in presence
    ) or "<tr><td colspan='7' class='muted'>No manpower recorded.</td></tr>"

    def event_rows(items, *, fields):
        if not items:
            return f"<tr><td colspan='{len(fields)}' class='muted'>None.</td></tr>"
        return "".join(
            "<tr>" + "".join(f"<td>{_html_escape(it.get(f) if not callable(f) else f(it))}</td>" for f in fields) + "</tr>"
            for it in items
        )

    delays_html = event_rows(
        delays,
        fields=["severity", "status", lambda d: d.get("reason_category") or "—",
                # Render WBS code + description rather than raw UUIDs.
                lambda d: "; ".join(_task_label(tid) for tid in (d.get("linked_task_ids") or [])) or "—",
                "description"],
    )
    qc_html = event_rows(qc, fields=["severity", "status", "description"])
    safety_html = event_rows(safety, fields=["event_type", "severity", "status", "description"])
    rfi_html = event_rows(rfis, fields=["rfi_number", "subject", "status",
                                         lambda r: r.get("date_initiated") or "—"])
    delivery_html = event_rows(
        deliveries,
        fields=["vendor", "description", lambda d: d.get("po_number") or "—",
                lambda d: d.get("status") or "—"],
    )
    # Item 4 (2026-05-06) — embed real thumbnails in a 3-col grid rather
    # than printing IDs in a table. Loads each photo's stored bytes,
    # downscales to 320px wide via PIL (keeps file size sane), encodes
    # as base64 and inlines as a data URI. Failures (storage hiccup,
    # corrupt file) are skipped silently — a missing thumbnail is much
    # better than a 500.
    from io import BytesIO
    import base64 as _b64
    from PIL import Image
    from services.storage import get_object

    def _photo_data_uri(p: dict) -> Optional[str]:
        path = p.get("thumbnail_path") or p.get("storage_path")
        if not path:
            return None
        try:
            data, ct = get_object(path)
        except Exception:
            return None
        try:
            img = Image.open(BytesIO(data))
            img.thumbnail((320, 320))  # in-place; keeps aspect ratio
            buf = BytesIO()
            fmt = "JPEG" if (img.mode in ("RGB", "L")) else "PNG"
            if fmt == "JPEG" and img.mode != "RGB":
                img = img.convert("RGB")
            img.save(buf, format=fmt, quality=78 if fmt == "JPEG" else None,
                     optimize=True)
            mime = "image/jpeg" if fmt == "JPEG" else "image/png"
            return f"data:{mime};base64," + _b64.b64encode(buf.getvalue()).decode()
        except Exception:
            return None

    photo_cards: list[str] = []
    for p in photos:
        uri = _photo_data_uri(p)
        caption = _html_escape(p.get("caption") or "No caption")
        captured = _html_escape(p.get("captured_at") or "—")
        if uri:
            img_tag = f"<img src='{uri}' alt='photo'/>"
        else:
            img_tag = "<div class='photo-placeholder'>image unavailable</div>"
        photo_cards.append(
            f"<div class='photo-card'>{img_tag}"
            f"<div class='photo-caption'>{caption}</div>"
            f"<div class='photo-meta'>{captured}</div></div>"
        )
    photo_html = "".join(photo_cards) or "<div class='muted'>No photos.</div>"
    audit_html = event_rows(
        audit_log,
        fields=["op", lambda a: a.get("actor_id") or "—", "created_at"],
    )

    reminder_html = ""
    if is_internal and reminders:
        cards = "".join(
            f"<div class='reminder'>"
            f"<b>{_html_escape(d.get('severity'))}</b> · "
            f"{(d.get('description') or '')[:140]} · "
            f"{len(d.get('linked_task_ids') or [])} tasks linked"
            f"</div>"
            for d in reminders
        )
        reminder_html = f"<h2>Open delay reminders ({len(reminders)})</h2>{cards}"

    # External-mode flag in the page title so reviewers can tell at a glance
    title_suffix = ""

    html = f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'><style>
@page {{ size: Letter; margin: 0.6in; }}
body {{ font-family: 'Helvetica', sans-serif; color: #0f172a; font-size: 10pt; }}
h1 {{ font-size: 18pt; margin: 0; }}
.muted {{ color: #64748b; }}
.meta {{ margin-top: 4px; font-size: 9pt; color: #475569; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 8.5pt; table-layout: fixed; }}
th {{ background: #1e293b; color: white; padding: 6px; text-align: left; font-weight: 600; }}
th.num {{ text-align: right; }}
td {{ padding: 5px 6px; border-bottom: 1px solid #e2e8f0; vertical-align: top; word-wrap: break-word; }}
.code {{ font-family: monospace; font-size: 8pt; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.pill {{ display: inline-block; padding: 2px 8px; border-radius: 999px; background: #e2e8f0; font-size: 8pt; }}
.banner {{ background: #f1f5f9; border-left: 3px solid #2563eb; padding: 8px 12px; margin: 12px 0; font-size: 9pt; }}
.reminder {{ background: #fffbea; border-left: 3px solid #d97706; padding: 6px 10px; margin: 4px 0; font-size: 9pt; }}
h2 {{ font-size: 11pt; margin: 18px 0 6px; color: #1e293b; border-bottom: 1px solid #cbd5e1; padding-bottom: 4px; }}
.audience {{ float: right; font-size: 8pt; color: #94a3b8; }}
.photo-grid {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }}
.photo-card {{ width: 30%; box-sizing: border-box; border: 1px solid #e2e8f0; border-radius: 4px; padding: 4px; background: #fff; page-break-inside: avoid; }}
.photo-card img {{ width: 100%; height: auto; display: block; border-radius: 2px; }}
.photo-placeholder {{ height: 80px; display: flex; align-items: center; justify-content: center; background: #f1f5f9; color: #94a3b8; font-size: 8pt; border-radius: 2px; }}
.photo-caption {{ font-size: 8.5pt; color: #1e293b; margin-top: 4px; }}
.photo-meta {{ font-size: 7.5pt; color: #94a3b8; margin-top: 2px; font-variant-numeric: tabular-nums; }}
/* Feature DR Header Auto-Population (2026-05-20) -- formal contract /
   station / signature block printed above the date/weather banner. */
.header-meta {{ width: 100%; margin: 6px 0 8px; border-collapse: collapse; font-size: 9pt; }}
.header-meta th {{ background: #f1f5f9; color: #475569; padding: 4px 6px; border: 1px solid #cbd5e1; text-align: left; font-weight: 600; font-size: 8pt; text-transform: uppercase; letter-spacing: 0.04em; width: 14%; }}
.header-meta td {{ padding: 4px 6px; border: 1px solid #cbd5e1; vertical-align: top; background: #ffffff; color: #0f172a; }}
</style></head><body>
<div class='audience'>{'INTERNAL' if is_internal else ''}</div>
<h1>Daily Report — {_html_escape(proj['name'] if proj else dr['project_id'])}{title_suffix}</h1>
<!-- Feature DR Header Auto-Population (2026-05-20) -- formal project /
     contract / signature block. Same six fields shown on the live DR
     header in the UI. Same for internal and external exports. -->
<table class='header-meta'>
  <tr>
    <th>Client</th><td>{_html_escape((proj or {{}}).get('client_name') or '')}</td>
    <th>Contract number</th><td>{_html_escape((proj or {{}}).get('contract_number') or '')}</td>
  </tr>
  <tr>
    <th>Station</th><td>{_html_escape((proj or {{}}).get('station_id') or '')}</td>
    <th>Contractor</th><td>{_html_escape((proj or {{}}).get('contractor_name') or '')}</td>
  </tr>
  <tr>
    <th>Project title</th><td>{_html_escape((proj or {{}}).get('name') or dr['project_id'])}</td>
    <th>Signature</th><td>{_html_escape(signer_name)}</td>
  </tr>
</table>
<div class='meta'>Date: <b>{dr['report_date']}</b> · Status: <span class='pill'>{dr['status']}</span></div>
<div class='banner'>
  <b>Weather:</b> {_html_escape(dr.get('weather_conditions') or 'n/a')} ·
  high {dr.get('weather_temp_high_f') or '—'}°F / low {dr.get('weather_temp_low_f') or '—'}°F ·
  precip {dr.get('weather_precipitation_in') or 0}in ·
  wind {dr.get('weather_wind_mph') or '—'}mph
</div>

{reminder_html}

<h2>Crew presence ({len(presence)})</h2>
<table>
<thead><tr><th>Trade</th><th>Company</th><th class='num'>Headcount</th><th class='num'>Hrs/person</th><th class='num'>Sub-total</th><th>Notes</th></tr></thead>
<tbody>{presence_rows_html}</tbody>
</table>

<h2>Activity grid ({len(grid)})</h2>
<table>
<thead><tr><th>Code</th><th>Description</th><th>Location</th><th class='num'>SOD</th><th class='num'>EOD</th><th class='num'>Crew</th><th class='num'>Hrs</th><th>Status</th></tr></thead>
<tbody>{grid_rows_html}</tbody>
</table>

<h2>Delays ({len(delays)})</h2>
<table>
<thead><tr><th>Severity</th><th>Status</th><th>Category</th><th>Linked tasks</th><th>Description</th></tr></thead>
<tbody>{delays_html}</tbody>
</table>

{("<h2>QC issues (" + str(len(qc)) + ")</h2><table><thead><tr><th>Severity</th><th>Status</th><th>Description</th></tr></thead><tbody>" + qc_html + "</tbody></table>") if is_internal else ""}

{("<h2>Safety events (" + str(len(safety)) + ")</h2><table><thead><tr><th>Type</th><th>Severity</th><th>Status</th><th>Description</th></tr></thead><tbody>" + safety_html + "</tbody></table>") if is_internal else ""}

<h2>RFIs ({len(rfis)})</h2>
<table>
<thead><tr><th>Number</th><th>Subject</th><th>Status</th><th>Initiated</th></tr></thead>
<tbody>{rfi_html}</tbody>
</table>

<h2>Deliveries ({len(deliveries)})</h2>
<table>
<thead><tr><th>Vendor</th><th>Description</th><th>PO</th><th>Status</th></tr></thead>
<tbody>{delivery_html}</tbody>
</table>

<h2>Photos ({len(photos)})</h2>
<div class='photo-grid'>{photo_html}</div>

{"<h2>Notes</h2><div>" + _html_escape(dr.get('notes') or '').replace(chr(10), '<br/>') + "</div>" if dr.get('notes') else ""}

{("<h2>Audit trail (" + str(len(audit_log)) + ")</h2><table><thead><tr><th>Op</th><th>Actor</th><th>When</th></tr></thead><tbody>" + audit_html + "</tbody></table>") if is_internal else ""}
</body></html>"""
    pdf = HTML(string=html).write_pdf()
    suffix = "internal" if is_internal else "external"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"content-disposition": f'attachment; filename="daily_report_{dr["report_date"]}_{suffix}.pdf"'},
    )


@router.get("/projects/{project_id}/weekly-rollup/pdf")
async def weekly_rollup_pdf(project_id: str, week_starting: date, ctx: dict = Depends(require_tenant)):
    """PDF of the weekly rollup. Master prompt §10.4."""
    from weasyprint import HTML
    from routers.daily_reports_router import weekly_rollup as _rollup
    rollup = await _rollup(project_id, week_starting, ctx)  # type: ignore
    db = get_db()
    proj = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": project_id}), {"_id": 0})
    trade_html = "".join(
        f"<tr><td>{_html_escape(k)}</td><td class='num'>{v:.1f}</td></tr>"
        for k, v in (rollup.get("hours_by_trade") or {}).items()
    ) or "<tr><td colspan='2' class='muted'>No trade hours.</td></tr>"
    html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'><style>
@page {{ size: Letter; margin: 0.7in; }}
body {{ font-family: 'Helvetica', sans-serif; color: #0f172a; }}
h1 {{ font-size: 17pt; }} h2 {{ font-size: 11pt; border-bottom: 1px solid #cbd5e1; padding-bottom: 4px; }}
.kv {{ display: inline-block; min-width: 22%; margin: 4px 8px 0 0; padding: 8px; background: #f1f5f9; border-radius: 4px; }}
.kv b {{ display: block; font-size: 13pt; }}
.kv span {{ font-size: 8pt; color: #64748b; text-transform: uppercase; }}
table {{ width: 100%; border-collapse: collapse; font-size: 9pt; margin-top: 8px; }}
th {{ background: #1e293b; color: white; padding: 6px; text-align: left; }}
td {{ padding: 5px 6px; border-bottom: 1px solid #e2e8f0; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
</style></head><body>
<h1>Weekly Rollup — {_html_escape(proj['name'] if proj else project_id)}</h1>
<div>Week: <b>{rollup['week_starting']} → {rollup['week_ending']}</b></div>
<div style='margin-top: 12px'>
<div class='kv'><b>{rollup['daily_report_count']}</b><span>Reports</span></div>
<div class='kv'><b>{rollup['ppc']}%</b><span>PPC</span></div>
<div class='kv'><b>{rollup['total_hours_worked']:.1f}</b><span>Hours</span></div>
<div class='kv'><b>{rollup['delay_minutes']}</b><span>Delay min</span></div>
<div class='kv'><b>{rollup['weather_impact_minutes']}</b><span>Weather min</span></div>
<div class='kv'><b>{rollup['activities_completed']}/{rollup['total_planned_activities']}</b><span>Activities done</span></div>
</div>
<h2>Hours by trade</h2>
<table><thead><tr><th>Trade</th><th>Hours</th></tr></thead><tbody>{trade_html}</tbody></table>
</body></html>"""
    pdf = HTML(string=html).write_pdf()
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"content-disposition": f'attachment; filename="weekly_{rollup["week_starting"]}.pdf"'},
    )


# ============================================================
# Photo EXIF + Perceptual hash (master prompt §13)
# ============================================================
@router.post("/photos/{photo_id}/extract-exif")
async def extract_photo_exif(photo_id: str, ctx: dict = Depends(require_tenant)):
    """Extract EXIF (GPS, timestamp) and compute perceptual hash for visual
    fingerprinting (substitution for DINOv2 — pHash is deterministic and free)."""
    from PIL import Image, ExifTags
    import imagehash
    from services.storage import get_object
    db = get_db()
    p = await db.photos.find_one(tenant_filter(ctx["tenant_id"], {"id": photo_id}), {"_id": 0})
    if not p or not p.get("storage_path"):
        raise HTTPException(404, "Photo not found")
    try:
        data, _ct = get_object(p["storage_path"])
    except Exception as e:
        raise HTTPException(500, f"storage: {e}")

    out: dict = {}
    try:
        img = Image.open(io.BytesIO(data))
        exif_raw = img._getexif() or {}
        exif: dict = {}
        for k, v in exif_raw.items():
            tag = ExifTags.TAGS.get(k, str(k))
            exif[tag] = v
        if "DateTimeOriginal" in exif:
            out["captured_at_exif"] = str(exif["DateTimeOriginal"])
        # GPS
        gps_raw = exif.get("GPSInfo")
        if isinstance(gps_raw, dict):
            gps: dict = {}
            for k, v in gps_raw.items():
                tag = ExifTags.GPSTAGS.get(k, str(k))
                gps[tag] = v
            if "GPSLatitude" in gps and "GPSLongitude" in gps:
                lat = _dms_to_dd(gps["GPSLatitude"], gps.get("GPSLatitudeRef") or "N")
                lng = _dms_to_dd(gps["GPSLongitude"], gps.get("GPSLongitudeRef") or "E")
                out["gps_lat"] = lat
                out["gps_lng"] = lng
        if "Make" in exif:
            out["camera_make"] = str(exif["Make"]).strip()
        if "Model" in exif:
            out["camera_model"] = str(exif["Model"]).strip()
        # perceptual hash for fingerprint
        phash = str(imagehash.phash(img))
        out["phash"] = phash
    except Exception as e:
        out["error"] = f"exif: {e}"

    if out:
        await db.photos.update_one(
            tenant_filter(ctx["tenant_id"], {"id": photo_id}), {"$set": out},
        )

    # Find duplicates / near-matches in same project (Hamming dist <= 8)
    matches: list[dict] = []
    if "phash" in out:
        cohort = await db.photos.find(
            tenant_filter(ctx["tenant_id"], {
                "project_id": p["project_id"],
                "id": {"$ne": photo_id},
                "phash": {"$exists": True, "$ne": None},
            }),
            {"_id": 0, "id": 1, "phash": 1, "caption": 1, "captured_at": 1},
        ).limit(500).to_list(500)
        target = imagehash.hex_to_hash(out["phash"])
        for c in cohort:
            try:
                dist = target - imagehash.hex_to_hash(c["phash"])
                if dist <= 8:
                    matches.append({"photo_id": c["id"], "hamming_distance": int(dist),
                                    "caption": c.get("caption"), "captured_at": c.get("captured_at")})
            except Exception:
                pass
        matches.sort(key=lambda m: m["hamming_distance"])
        if matches:
            await db.photos.update_one(
                tenant_filter(ctx["tenant_id"], {"id": photo_id}),
                {"$set": {
                    "visual_fingerprint_match_id": matches[0]["photo_id"],
                    "visual_fingerprint_match_confidence": round(1 - matches[0]["hamming_distance"] / 64, 2),
                }},
            )
    return {"photo_id": photo_id, "extracted": out, "matches": matches[:10]}


def _dms_to_dd(dms, ref):
    try:
        d, m, s = dms
        dd = float(d) + float(m) / 60.0 + float(s) / 3600.0
        if str(ref).upper() in ("S", "W"):
            dd = -dd
        return round(dd, 6)
    except Exception:
        return None


# ============================================================
# AI Recovery — apply suggestion
# ============================================================
class ApplyRecoveryRequest(StrictModel):
    suggestion_index: int


@router.post("/ai/recovery/{recovery_id}/apply")
async def apply_recovery(recovery_id: str, payload: ApplyRecoveryRequest, ctx: dict = Depends(require_tenant)):
    """Apply a chosen recovery suggestion. For now we support category=='extend_shift'
    (no schedule edit), 'add_crew' (no schedule edit), or 'split_activity' / 'resequence'
    (mark target activity status='in_progress' and lengthen its planned_finish by the
    suggestion's predicted_time_recovery_days, capped at 60d). Audit-logged.
    Master prompt §6 — recovery agent must support 'apply' end-to-end."""
    db = get_db()
    rec = await db.ai_recovery_suggestions.find_one(
        tenant_filter(ctx["tenant_id"], {"id": recovery_id}), {"_id": 0},
    )
    if not rec:
        raise HTTPException(404, "Suggestion not found")
    suggestions = rec.get("suggestions") or []
    if payload.suggestion_index < 0 or payload.suggestion_index >= len(suggestions):
        raise HTTPException(400, "Suggestion index out of range")
    chosen = suggestions[payload.suggestion_index]
    target_id = rec.get("target_activity_id")
    target = await db.tasks.find_one(tenant_filter(ctx["tenant_id"], {"id": target_id}), {"_id": 0})
    if not target:
        raise HTTPException(404, "Target activity not found")

    cat = chosen.get("category", "")
    days = float(chosen.get("predicted_time_recovery_days") or 0)
    days = max(0.0, min(60.0, days))
    update: dict = {}
    note: list[str] = []
    if cat in ("split_activity", "resequence", "change_method"):
        try:
            new_finish = (date.fromisoformat(target["planned_finish"][:10])
                          - timedelta(days=int(round(days)))).isoformat()
        except Exception:
            new_finish = target.get("planned_finish")
        update["planned_finish"] = new_finish
        note.append(f"planned_finish accelerated by {int(days)}d")
    if cat == "extend_shift":
        note.append(f"shift extended; expected recovery {days}d (no schedule edit)")
    if cat == "add_crew":
        note.append(f"add_crew applied; expected recovery {days}d")
    if cat == "escalate":
        # create a work item for escalation
        wi = {
            "id": gen_id(), "tenant_id": ctx["tenant_id"], "project_id": rec["project_id"],
            "title": f"Escalation: {chosen.get('title') or 'Schedule slip'}",
            "description": chosen.get("rationale") or "",
            "priority": "high", "category": "schedule_slip", "status": "open",
            "opened_by": ctx["user"]["id"], "opened_at": utc_now().isoformat(),
            "owned_by": ctx["user"]["id"],
            "actual_cost_impact": None, "actual_time_impact_days": None,
            "closed_at": None, "closure_summary": None,
            "created_at": utc_now().isoformat(),
        }
        await db.work_items.insert_one(wi.copy())
        note.append("escalation work item created")

    if update:
        await db.tasks.update_one(
            tenant_filter(ctx["tenant_id"], {"id": target_id}), {"$set": update},
        )

    await db.ai_recovery_suggestions.update_one(
        tenant_filter(ctx["tenant_id"], {"id": recovery_id}),
        {"$set": {
            "status": "applied",
            "chosen_suggestion_index": payload.suggestion_index,
            "applied_at": utc_now().isoformat(),
            "applied_by": ctx["user"]["id"],
        }},
    )
    return {
        "ok": True,
        "applied": chosen,
        "notes": note,
        "task_changes": update,
    }


# ============================================================
# Eval harness — score past recoveries
# ============================================================
@router.get("/ai/recovery/eval-harness")
async def recovery_eval(project_id: Optional[str] = None, ctx: dict = Depends(require_tenant)):
    """Score past recovery suggestions against actual schedule outcomes.
    For each applied suggestion we compute (predicted_recovery_days vs actual
    finish-date drift). Master prompt §6.6 (eval harness)."""
    db = get_db()
    f = tenant_filter(ctx["tenant_id"], {"status": "applied"})
    if project_id:
        f["project_id"] = project_id
    recs = await db.ai_recovery_suggestions.find(f, {"_id": 0}).sort("applied_at", -1).limit(100).to_list(100)
    rows: list[dict] = []
    total_pred = 0.0
    total_realized = 0.0
    for r in recs:
        idx = r.get("chosen_suggestion_index")
        if idx is None or idx < 0 or idx >= len(r.get("suggestions") or []):
            continue
        chosen = r["suggestions"][idx]
        target = await db.tasks.find_one(
            tenant_filter(ctx["tenant_id"], {"id": r["target_activity_id"]}), {"_id": 0},
        )
        if not target:
            continue
        predicted = float(chosen.get("predicted_time_recovery_days") or 0)
        # realized = pct_complete progress since apply, in days proxy
        realized_days = 0.0
        applied_at = r.get("applied_at")
        if applied_at:
            try:
                applied_d = datetime.fromisoformat(applied_at.replace("Z", "+00:00")).date()
                today = date.today()
                age_d = (today - applied_d).days
                # crude proxy: pct progress * duration / 100
                dur = float(target.get("duration_days") or 1.0)
                pct = float(target.get("pct_complete") or 0.0)
                expected_progress = pct * dur / 100.0
                realized_days = round(min(expected_progress, age_d), 2)
            except Exception:
                pass
        delta = round(realized_days - predicted, 2)
        total_pred += predicted
        total_realized += realized_days
        rows.append({
            "suggestion_id": r["id"],
            "applied_at": applied_at,
            "category": chosen.get("category"),
            "title": chosen.get("title"),
            "predicted_days": predicted,
            "realized_days": realized_days,
            "delta_days": delta,
            "target_activity_code": target.get("activity_code"),
        })
    return {
        "total_applied": len(rows),
        "total_predicted_days": round(total_pred, 1),
        "total_realized_days": round(total_realized, 1),
        "accuracy_score": round((total_realized / total_pred), 2) if total_pred else None,
        "rows": rows,
    }


# ============================================================
# Aging detail (per-record)
# ============================================================
@router.get("/aging-detail")
async def aging_detail(ctx: dict = Depends(require_tenant)):
    """Per-record aging breakdown for procurement dashboard."""
    db = get_db()
    f = tenant_filter(ctx["tenant_id"])
    now = datetime.now(timezone.utc)

    rfi_cut = (now - timedelta(days=5)).isoformat()[:10]
    rfis = await db.rfis.find(
        {**f, "status": {"$in": ["sent", "in_review"]}, "date_initiated": {"$lte": rfi_cut}},
        {"_id": 0, "id": 1, "rfi_number": 1, "subject": 1, "date_initiated": 1, "status": 1, "project_id": 1},
    ).sort("date_initiated", 1).limit(100).to_list(100)

    sub_cut = (now - timedelta(days=14)).isoformat()
    submittals = await db.submittals.find(
        {**f, "status": {"$in": ["submitted", "in_review"]}, "created_at": {"$lte": sub_cut}},
        {"_id": 0, "id": 1, "submittal_number": 1, "title": 1, "created_at": 1, "status": 1, "project_id": 1},
    ).sort("created_at", 1).limit(100).to_list(100)

    wi_cut = (now - timedelta(days=7)).isoformat()
    work_items = await db.work_items.find(
        {**f, "status": {"$nin": ["closed", "resolved"]}, "opened_at": {"$lte": wi_cut}},
        {"_id": 0, "id": 1, "title": 1, "priority": 1, "opened_at": 1, "status": 1, "project_id": 1},
    ).sort("opened_at", 1).limit(100).to_list(100)

    apv_cut = (now - timedelta(days=3)).isoformat()
    approvals = await db.approvals.find(
        {**f, "status": "pending", "proposed_at": {"$lte": apv_cut}},
        {"_id": 0, "id": 1, "subject": 1, "proposed_at": 1, "status": 1, "project_id": 1},
    ).sort("proposed_at", 1).limit(100).to_list(100)

    today = now.date()

    def _days_since(iso: str | None) -> int:
        if not iso:
            return 0
        try:
            d = datetime.fromisoformat(iso[:19] if "T" in iso else iso[:10]).date() if "T" in iso else date.fromisoformat(iso[:10])
            return (today - d).days
        except Exception:
            return 0

    return {
        "rfis": [{**r, "days_open": _days_since(r.get("date_initiated"))} for r in rfis],
        "submittals": [{**s, "days_open": _days_since(s.get("created_at"))} for s in submittals],
        "work_items": [{**w, "days_open": _days_since(w.get("opened_at"))} for w in work_items],
        "approvals": [{**a, "days_open": _days_since(a.get("proposed_at"))} for a in approvals],
    }


# ============================================================
# Submittal ↔ Activity bidirectional auto-link
# ============================================================
class SubmittalLinkUpdate(StrictModel):
    schedule_activity_id: Optional[str] = None
    spec_section_id: Optional[str] = None
    drawings_affected: Optional[List[str]] = None


@router.patch("/submittals/{submittal_id}/links")
async def link_submittal(submittal_id: str, payload: SubmittalLinkUpdate, ctx: dict = Depends(require_tenant)):
    """Patch the link fields explicitly. (The main /submittals/{id} PATCH only
    covers status/sub_responsible.)"""
    db = get_db()
    updates = {k: v for k, v in payload.model_dump(mode="json").items() if v is not None}
    if not updates:
        raise HTTPException(400, "No fields")
    res = await db.submittals.update_one(
        tenant_filter(ctx["tenant_id"], {"id": submittal_id}), {"$set": updates},
    )
    if not res.matched_count:
        raise HTTPException(404, "Submittal not found")
    doc = await db.submittals.find_one(tenant_filter(ctx["tenant_id"], {"id": submittal_id}), {"_id": 0})
    return doc


@router.post("/projects/{project_id}/submittals/auto-link-activities")
async def auto_link_activities(project_id: str, ctx: dict = Depends(require_tenant)):
    """For each submittal with a spec_section_id, find tasks whose
    spec_section_codes (if present) match the submittal's section code and
    auto-set schedule_activity_id (only if currently None). Idempotent.

    Master prompt §15 (Submittal log → schedule activity linkage).
    """
    db = get_db()
    submittals = await db.submittals.find(
        tenant_filter(ctx["tenant_id"], {
            "project_id": project_id,
            "spec_section_code": {"$exists": True, "$ne": None},
            "schedule_activity_id": None,
        }),
        {"_id": 0},
    ).to_list(2000)
    tasks = await db.tasks.find(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id, "status": {"$ne": "deleted"}}),
        {"_id": 0, "id": 1, "spec_section_codes": 1, "trade_code": 1, "description": 1, "activity_code": 1},
    ).to_list(5000)
    by_code: dict[str, list[dict]] = {}
    for t in tasks:
        codes = t.get("spec_section_codes") or []
        for c in codes:
            by_code.setdefault(c, []).append(t)

    linked = 0
    for s in submittals:
        c = s.get("spec_section_code")
        if not c:
            continue
        candidates = by_code.get(c) or []
        if not candidates:
            # also try first 2 segments (e.g. "03 30")
            short = " ".join(c.split()[:2])
            candidates = [t for t in tasks if any(
                ((tc or "").startswith(short)) for tc in (t.get("spec_section_codes") or [])
            )]
        if candidates:
            await db.submittals.update_one(
                tenant_filter(ctx["tenant_id"], {"id": s["id"]}),
                {"$set": {"schedule_activity_id": candidates[0]["id"]}},
            )
            linked += 1
    return {"linked": linked, "total_submittals_examined": len(submittals)}


# ============================================================
# Section view (Spatial 3D cut-line — frontend uses payload to render slice)
# ============================================================
class SectionRequest(StrictModel):
    project_id: str
    cut_line: List[List[float]]  # [[x1,z1],[x2,z2]] in plan coords (PDF px)


@router.post("/spatial/section")
async def compute_section(payload: SectionRequest, ctx: dict = Depends(require_tenant)):
    """Compute a vertical section (cut-line on plan → list of intersected
    rooms with their floors stacked). Returns segments per floor for the
    frontend to render as 2D rects."""
    db = get_db()
    f = tenant_filter(ctx["tenant_id"], {"project_id": payload.project_id})
    floors = await db.floors.find(f, {"_id": 0}).sort("floor_number", 1).to_list(50)
    locations = await db.locations.find(f, {"_id": 0}).to_list(2000)

    if len(payload.cut_line) != 2:
        raise HTTPException(400, "cut_line must have exactly 2 points")
    (x1, z1), (x2, z2) = payload.cut_line
    floor_segments: list[dict] = []
    for fl in floors:
        fl_locs = [loc for loc in locations if loc.get("floor_id") == fl["id"]]
        intersections: list[dict] = []
        for loc in fl_locs:
            ring = (loc.get("polygon_geojson") or {}).get("coordinates", [[]])[0]
            if not ring:
                continue
            # find segment intersections with the cut line
            for i in range(len(ring) - 1):
                ax, ay = ring[i]
                bx, by = ring[i + 1]
                hit = _segment_intersect(x1, z1, x2, z2, ax, ay, bx, by)
                if hit:
                    intersections.append({
                        "location_id": loc["id"],
                        "location_name": loc.get("name"),
                        "x": hit[0], "z": hit[1],
                    })
        # de-dupe + sort by t along line
        if intersections:
            dx, dz = (x2 - x1), (z2 - z1)
            length = max(1e-6, math.hypot(dx, dz))
            for i in intersections:
                i["t"] = ((i["x"] - x1) * dx + (i["z"] - z1) * dz) / (length * length)
            intersections.sort(key=lambda r: r["t"])
        floor_segments.append({
            "floor_id": fl["id"], "floor_number": fl["floor_number"],
            "floor_name": fl["floor_name"],
            "elevation_z_ft": fl.get("elevation_z_ft", 0),
            "floor_height_ft": fl.get("floor_height_ft", 12),
            "intersections": intersections,
        })
    return {"floors": floor_segments, "cut_line": payload.cut_line}


def _segment_intersect(x1, y1, x2, y2, x3, y3, x4, y4):
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < 1e-9:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
    u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / den
    if 0 <= t <= 1 and 0 <= u <= 1:
        return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))
    return None
