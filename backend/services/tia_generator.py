"""Bucket C3c Phase 4 — TIA source-document generator.

Single entrypoint generate_tia() that:
  1. Queries every TIA-relevant collection across the date range
  2. Builds a structured JSON document
  3. Renders a 12-section .docx Word document via python-docx
  4. Renders a navigable evidence-index PDF via WeasyPrint
  5. Persists all three artifacts in storage
  6. Inserts the tia_generations record with FK refs + counts
  7. Writes a TIA_GENERATED audit entry on the hash chain

The output is a *source document* -- the structured data a scheduling
engineer or counsel needs to write a TIA narrative. The platform never
adjudicates causation; owner attribution is recorded as the user logged
it. The privilege flag is set true by default per the C3c Phase 5 spec.
"""
from __future__ import annotations
import io
import json
import html
from datetime import date, datetime, timezone
from typing import Optional

from database import get_db, tenant_filter
from models import gen_id, utc_now, TIA_DEFAULT_PRIVILEGE_FLAG
from audit import write_audit
from services.storage import put_object, get_object, make_path, record_file


# ============================================================
# Data gathering
# ============================================================

async def _gather_data(
    tenant_id: str, project_id: str,
    start: date, end: date, filters: dict,
) -> dict:
    db = get_db()
    start_iso, end_iso = start.isoformat(), end.isoformat()
    range_str = f"{start_iso}/{end_iso}"

    cause_class = filters.get("cause_class")
    zone_id = filters.get("zone_id")
    gating_entity_id = filters.get("gating_entity_id")

    project = await db.projects.find_one(
        tenant_filter(tenant_id, {"id": project_id}), {"_id": 0},
    ) or {}

    # ---- Delays ----
    dq: dict = {"project_id": project_id,
                "logged_date": {"$gte": start_iso, "$lte": end_iso},
                "status": {"$ne": "deleted"}}
    if cause_class:
        dq["cause_class"] = cause_class
    delays = await db.delays.find(tenant_filter(tenant_id, dq), {"_id": 0}).to_list(5000)

    # ---- Gating entities (created OR closed in range) ----
    gq: dict = {"project_id": project_id,
                "$or": [
                    {"created_at": {"$gte": start_iso, "$lte": end_iso + "T23:59:59Z"}},
                    {"closed_at": {"$gte": start_iso, "$lte": end_iso + "T23:59:59Z"}},
                ]}
    if cause_class:
        gq["cause_class"] = cause_class
    if gating_entity_id:
        gq = {"id": gating_entity_id}
    gating_entities = await db.gating_entities.find(
        tenant_filter(tenant_id, gq), {"_id": 0},
    ).to_list(5000)

    # ---- Constraint captures from daily_report_activities ----
    # Pull DR ids in range first, then activities pointing at them.
    drs = await db.daily_reports.find(
        tenant_filter(tenant_id, {
            "project_id": project_id,
            "report_date": {"$gte": start_iso, "$lte": end_iso},
        }),
        {"_id": 0, "id": 1, "report_date": 1, "status": 1},
    ).to_list(5000)
    dr_ids = [d["id"] for d in drs]
    dr_dates = {d["id"]: d["report_date"] for d in drs}
    constraints: list[dict] = []
    presence: list[dict] = []
    if dr_ids:
        cq: dict = {"daily_report_id": {"$in": dr_ids},
                     "constraint_reference_entity_id": {"$ne": None}}
        if gating_entity_id:
            cq["constraint_reference_entity_id"] = gating_entity_id
        constraints = await db.daily_report_activities.find(
            cq,
            {"_id": 0, "id": 1, "daily_report_id": 1, "schedule_activity_id": 1,
             "activity_code": 1, "activity_description": 1, "constraint_type": 1,
             "constraint_reference_entity_id": 1, "constraint_auto_filled": 1,
             "production_impact_hours": 1, "hours_worked": 1},
        ).to_list(20000)
        for c in constraints:
            c["report_date"] = dr_dates.get(c.get("daily_report_id"))

        pq: dict = {"daily_report_id": {"$in": dr_ids}}
        if zone_id:
            pq["zone_id"] = zone_id
        presence = await db.daily_presence.find(
            tenant_filter(tenant_id, pq),
            {"_id": 0},
        ).to_list(20000)
        for p in presence:
            p["report_date"] = dr_dates.get(p.get("daily_report_id"))

    # ---- Coordination events ----
    cq2: dict = {"project_id": project_id,
                 "occurred_at": {"$gte": start_iso, "$lte": end_iso + "T23:59:59Z"},
                 "deleted_at": None}
    coord = await db.coordination_events.find(
        tenant_filter(tenant_id, cq2), {"_id": 0},
    ).to_list(5000)

    # ---- Mitigations ----
    mq: dict = {"project_id": project_id,
                "$or": [
                    {"outcome_recorded_at": {"$gte": start_iso, "$lte": end_iso + "T23:59:59Z"}},
                    {"decision_made_at": {"$gte": start_iso, "$lte": end_iso + "T23:59:59Z"}},
                    {"proposed_at": {"$gte": start_iso, "$lte": end_iso + "T23:59:59Z"}},
                ]}
    mitigations = await db.mitigations.find(
        tenant_filter(tenant_id, mq), {"_id": 0},
    ).to_list(5000)

    # ---- Photos: only evidentiary classes (discovery, damage) per spec ----
    pq2: dict = {"project_id": project_id,
                 "photo_class": {"$in": ["discovery", "damage"]}}
    if zone_id:
        pq2["location_id"] = zone_id
    photos = await db.photos.find(
        tenant_filter(tenant_id, pq2),
        {"_id": 0, "id": 1, "storage_path": 1, "caption": 1, "photo_class": 1,
         "evidentiary_flag": 1, "sha256_hash": 1, "device_timestamp": 1,
         "server_received_timestamp": 1, "timestamp_mismatch_flag": 1,
         "drawing_pin_id": 1, "pin_x": 1, "pin_y": 1, "pin_drawing_id": 1,
         "location_id": 1, "created_at": 1, "schedule_activity_id": 1,
         # Bucket F Phase 4 -- AI lineage on the tag row. The tray row is
         # the authoritative source for the *suggestion* fields; the tag
         # row carries lineage flags (created_via_ai_suggestion +
         # ai_suggestion_id + ai_confidence_at_tag_time) so counsel can
         # see "this tag was authored from AI suggestion X with
         # confidence Y at tag time".
         "tray_photo_id": 1, "created_via_ai_suggestion": 1,
         "ai_suggestion_id": 1, "ai_confidence_at_tag_time": 1},
    ).to_list(5000)
    photos = [p for p in photos
              if (p.get("device_timestamp") or p.get("server_received_timestamp") or p.get("created_at") or "")[:10] >= start_iso
              and (p.get("device_timestamp") or p.get("server_received_timestamp") or p.get("created_at") or "")[:10] <= end_iso]

    # ---- Bucket F Phase 4 -- enrich evidentiary photos with AI metadata
    # from the linked tray row. Counsel needs to see what the AI
    # suggested, what the user kept vs. overrode, and any safety
    # concerns the model flagged. We pull the tray rows in one batch
    # then merge by tray_photo_id.
    tray_ids = [p.get("tray_photo_id") for p in photos if p.get("tray_photo_id")]
    tray_by_id: dict[str, dict] = {}
    if tray_ids:
        tray_rows = await db.photo_tray.find(
            tenant_filter(tenant_id, {"id": {"$in": tray_ids}}),
            {"_id": 0, "id": 1, "ai_analysis_status": 1,
             "ai_suggested_class": 1, "ai_suggested_caption": 1,
             "ai_suggested_event_type": 1, "ai_suggested_trade": 1,
             "ai_confidence_score": 1, "ai_user_overridden": 1,
             "ai_user_override_reason": 1, "ai_safety_concerns_detected": 1,
             "ai_model_version": 1, "ai_reused_from_phash": 1},
        ).to_list(length=5000)
        tray_by_id = {t["id"]: t for t in tray_rows}
    for p in photos:
        tray = tray_by_id.get(p.get("tray_photo_id") or "")
        if not tray:
            # No tray row (legacy photo, pre-AI upload, or skipped CUI).
            # Stamp empty AI fields so renderers can branch uniformly.
            p["ai_analysis_status"] = None
            p["ai_suggested_class"] = None
            p["ai_suggested_caption"] = None
            p["ai_suggested_event_type"] = None
            p["ai_suggested_trade"] = None
            p["ai_confidence_score"] = None
            p["ai_user_overridden"] = False
            p["ai_user_override_reason"] = None
            p["ai_safety_concerns_detected"] = []
            p["ai_reused_from_phash"] = False
            continue
        p["ai_analysis_status"] = tray.get("ai_analysis_status")
        p["ai_suggested_class"] = tray.get("ai_suggested_class")
        p["ai_suggested_caption"] = tray.get("ai_suggested_caption")
        p["ai_suggested_event_type"] = tray.get("ai_suggested_event_type")
        p["ai_suggested_trade"] = tray.get("ai_suggested_trade")
        p["ai_confidence_score"] = tray.get("ai_confidence_score")
        p["ai_user_overridden"] = bool(tray.get("ai_user_overridden"))
        p["ai_user_override_reason"] = tray.get("ai_user_override_reason")
        p["ai_safety_concerns_detected"] = list(
            tray.get("ai_safety_concerns_detected") or []
        )
        p["ai_reused_from_phash"] = bool(tray.get("ai_reused_from_phash"))
        # Compose a per-row override-detail string in the form the spec
        # requires: "AI suggested X; user marked as Y, reason: Z" --
        # the docx + pdf renderers read this directly so the override
        # narrative is single-sourced.
        if p["ai_user_overridden"]:
            suggested_class = p.get("ai_suggested_class") or "—"
            user_class = p.get("photo_class") or "—"
            reason = p.get("ai_user_override_reason") or "(no reason recorded)"
            p["ai_override_detail"] = (
                f"AI suggested {suggested_class}; user marked as {user_class}, "
                f"reason: {reason}"
            )
        else:
            p["ai_override_detail"] = ""

    # ---- Schedule update diffs ----
    sd_q: dict = {"project_id": project_id,
                  "computed_at": {"$gte": start_iso, "$lte": end_iso + "T23:59:59Z"}}
    schedule_diffs = await db.schedule_update_diffs.find(
        tenant_filter(tenant_id, sd_q),
        {"_id": 0, "activities_added": 0, "activities_removed": 0,
         "activities_modified": 0, "predecessor_changes": 0},
    ).to_list(500)

    # ---- Latest cascade snapshot per gating entity in range ----
    cs_pipeline = [
        {"$match": {"tenant_id": tenant_id, "project_id": project_id,
                     "computed_at": {"$gte": start_iso, "$lte": end_iso + "T23:59:59Z"}}},
        {"$sort": {"computed_at": -1}},
        {"$group": {"_id": "$gating_entity_id", "snap": {"$first": "$$ROOT"}}},
    ]
    cs_docs = await db.cascade_snapshots.aggregate(cs_pipeline).to_list(5000)
    cascade_snapshots = []
    for d in cs_docs:
        snap = d.get("snap") or {}
        snap.pop("_id", None)
        cascade_snapshots.append(snap)

    # ---- Calendar reconciliation rows for the range ----
    from routers.calendar_reconciliation_router import _build_rows
    calendar_rows = await _build_rows(tenant_id, project_id, start, end)

    # ---- Bucket D Phase 4 -- supplements pointing at DRs in range ----
    # Events filed AFTER a DR's submit (with parent_daily_report_id set
    # to one of the in-range DRs) belong to the DR's date in the TIA
    # timeline, even though they live in a later container DR. Pull
    # them now and merge into each per-collection list with a `_supp`
    # marker that the renderers use to print [SUPPLEMENT].
    if dr_ids:
        async def _pull_sup(coll: str) -> list[dict]:
            rows = await db[coll].find(
                tenant_filter(tenant_id, {
                    "parent_daily_report_id": {"$in": dr_ids},
                    "is_supplement": True,
                }),
                {"_id": 0},
            ).to_list(length=5000)
            for r in rows:
                r["_supp"] = True
                # Pin the row to its parent DR's date for chronological
                # placement in the TIA timeline.
                r["_supp_parent_date"] = dr_dates.get(r.get("parent_daily_report_id"))
            return rows
        delays.extend(await _pull_sup("delays"))
        coord.extend(await _pull_sup("coordination_events"))
        mitigations.extend(await _pull_sup("mitigations"))
        photos.extend(await _pull_sup("photos"))
        # gating_entities / qc / safety supplements go into their own keys
        # below (no current TIA section pulls qc/safety as a list, but
        # the data structure includes them under section_13/etc.).

    # Addition 4 -- annotate each row that has been edited post-submit
    # with an `edits_summary` cell that the renderers print as
    # "edited N times, last YYYY-MM-DD". Counsel sees the original
    # contemporaneous record + a clear flag that it was edited later.
    def _annotate_edits(rows: list[dict]) -> None:
        for r in rows:
            if r.get("post_submit_edited"):
                cnt = int(r.get("edit_count") or 0)
                last = (r.get("last_edited_at") or "")[:10]
                r["edits_summary"] = (
                    f"edited {cnt} time(s) post-submit, last {last}"
                    if last else f"edited {cnt} time(s) post-submit"
                )
            else:
                r["edits_summary"] = ""
    _annotate_edits(delays)
    _annotate_edits(photos)
    _annotate_edits(coord)
    _annotate_edits(gating_entities)

    # ---- Audit log: filtered to delay-driving entities in range ----
    relevant_tables = [
        "delays", "mitigations", "gating_entities", "coordination_events",
        "schedule_update_diffs", "cascade_snapshots", "photos",
        "daily_reports", "daily_presence", "daily_report_activities",
        # Bucket D Phase 4 -- include qc_issues + safety_events + deliveries
        # so SUPPLEMENT_FILED entries on those collections show in the
        # TIA Section 12 audit extract.
        "qc_issues", "safety_events", "deliveries", "rfis",
        # Addition 4 -- surface EDIT_REQUEST_FILED / APPROVED / DENIED
        # ops in the audit extract so counsel can see the approval
        # workflow alongside the EVENT_EDITED_POST_SUBMIT entries.
        "edit_requests",
        # Bucket G Phase 4 (2026-05-11) -- field_suggestions lifecycle
        # ops give counsel the full "AI suggested X, human confirmed
        # Y" trail per Sid's federal-audit requirement.
        "field_suggestions",
    ]
    audit_entries = await db.audit_log.find(
        {"tenant_id": tenant_id,
         "table_name": {"$in": relevant_tables},
         "created_at": {"$gte": start_iso, "$lte": end_iso + "T23:59:59Z"}},
        {"_id": 0, "id": 1, "created_at": 1, "table_name": 1, "op": 1,
         "record_id": 1, "actor_id": 1, "hash": 1, "prev_hash": 1,
         # Pull the after-body so Section 12a (Phase 4) can render the
         # suggestion lifecycle without a per-row second fetch.
         "after": 1},
    ).sort("created_at", 1).to_list(20000)

    # ---- Bucket G Phase 4 -- field_suggestion lifecycle timeline ----
    # Walks the audit entries, groups by record_id (= suggestion id),
    # and builds a per-suggestion lineage: created -> [confirmed |
    # rejected | superseded | expired]. Lets Section 12a render the
    # "AI suggested X on date, human confirmed Y on date" timeline
    # counsel needs as audit evidence.
    suggestion_lifecycle: dict[str, dict] = {}
    for entry in audit_entries:
        op = entry.get("op") or ""
        if not op.startswith("FIELD_SUGGESTION_"):
            continue
        sid = entry.get("record_id")
        if not sid:
            continue
        bucket = suggestion_lifecycle.setdefault(sid, {
            "suggestion_id": sid,
            "events": [],
            "created_at": None, "terminal_at": None, "terminal_op": None,
            "entity_type": None, "entity_id": None,
            "field_path": None, "suggested_value": None,
            "suggested_by": None, "confidence_score": None,
            "confirmed_by": None, "user_role": None,
            "rejection_reason": None,
        })
        bucket["events"].append({
            "op": op, "when": entry.get("created_at"),
            "actor_id": entry.get("actor_id"),
            "after": entry.get("after") or {},
        })
        after = entry.get("after") or {}
        if op == "FIELD_SUGGESTION_CREATED":
            bucket["created_at"] = entry.get("created_at")
            bucket["entity_type"] = after.get("entity_type")
            bucket["entity_id"] = after.get("entity_id")
            bucket["field_path"] = after.get("field_path")
            bucket["suggested_by"] = after.get("suggested_by")
            bucket["confidence_score"] = after.get("confidence_score")
        elif op == "FIELD_SUGGESTION_CONFIRMED":
            bucket["terminal_at"] = entry.get("created_at")
            bucket["terminal_op"] = "confirmed"
            bucket["confirmed_by"] = (
                after.get("confirmed_by") or entry.get("actor_id")
            )
            bucket["user_role"] = after.get("user_role")
            if after.get("suggested_value") is not None:
                bucket["suggested_value"] = after.get("suggested_value")
            if after.get("field_path"):
                bucket["field_path"] = after.get("field_path")
            if after.get("entity_type"):
                bucket["entity_type"] = after.get("entity_type")
            if after.get("entity_id"):
                bucket["entity_id"] = after.get("entity_id")
        elif op == "FIELD_SUGGESTION_REJECTED":
            bucket["terminal_at"] = entry.get("created_at")
            bucket["terminal_op"] = "rejected"
            bucket["rejection_reason"] = after.get("reason")
            bucket["user_role"] = after.get("user_role")
            if after.get("field_path"):
                bucket["field_path"] = after.get("field_path")
            if after.get("entity_type"):
                bucket["entity_type"] = after.get("entity_type")
            if after.get("entity_id"):
                bucket["entity_id"] = after.get("entity_id")
        elif op == "FIELD_SUGGESTION_SUPERSEDED":
            if bucket["terminal_op"] is None:
                bucket["terminal_at"] = entry.get("created_at")
                bucket["terminal_op"] = "superseded"
        elif op == "FIELD_SUGGESTION_EXPIRED":
            if bucket["terminal_op"] is None:
                bucket["terminal_at"] = entry.get("created_at")
                bucket["terminal_op"] = "expired"
            if after.get("entity_type"):
                bucket["entity_type"] = after.get("entity_type")
            if after.get("field_path"):
                bucket["field_path"] = after.get("field_path")

    # Sort lifecycle rows by created_at then by terminal_at so the
    # rendered timeline reads in temporal order. Also enrich with
    # the current persisted suggestion row when available so we can
    # surface suggested_value even on rows where the CONFIRMED audit
    # entry didn't include it (CONFIRMED in Phase 1 captures the
    # value, but older audit rows might not).
    lifecycle_list = sorted(
        suggestion_lifecycle.values(),
        key=lambda b: (b.get("created_at") or b.get("terminal_at") or ""),
    )
    if lifecycle_list:
        sids = [b["suggestion_id"] for b in lifecycle_list
                 if b.get("suggestion_id")]
        if sids:
            sugg_rows = await db.field_suggestions.find(
                tenant_filter(tenant_id, {"id": {"$in": sids}}),
                {"_id": 0, "id": 1, "suggested_value": 1,
                 "confidence_score": 1, "suggested_by": 1,
                 "entity_type": 1, "entity_id": 1,
                 "field_path": 1},
            ).to_list(length=None)
            by_id = {r["id"]: r for r in sugg_rows}
            for b in lifecycle_list:
                src = by_id.get(b.get("suggestion_id"))
                if not src:
                    continue
                if b.get("suggested_value") is None:
                    b["suggested_value"] = src.get("suggested_value")
                if b.get("confidence_score") is None:
                    b["confidence_score"] = src.get("confidence_score")
                if not b.get("suggested_by"):
                    b["suggested_by"] = src.get("suggested_by")
                if not b.get("entity_type"):
                    b["entity_type"] = src.get("entity_type")
                if not b.get("entity_id"):
                    b["entity_id"] = src.get("entity_id")
                if not b.get("field_path"):
                    b["field_path"] = src.get("field_path")

    return {
        "_meta": {
            "tenant_id": tenant_id,
            "project": project,
            "date_range": {"start": start_iso, "end": end_iso, "iso8601_interval": range_str},
            "filters": filters,
            "generated_at": utc_now().isoformat(),
        },
        "section_1_project_header": {
            "name": project.get("name"),
            "agency": project.get("agency"),
            "contract_number": project.get("contract_number"),
            "ntp_date": project.get("ntp_date"),
            "original_substantial_completion": project.get("original_substantial_completion"),
            "current_substantial_completion": project.get("current_substantial_completion"),
            "contract_days": project.get("contract_days"),
            "calendar_basis": project.get("calendar_basis"),
        },
        "section_2_delay_event_ledger": delays,
        "section_3_cpm_cascade_analysis": cascade_snapshots,
        "section_4_owner_attribution_summary": _summarize_attribution(delays, gating_entities),
        "section_5_mitigations": mitigations,
        "section_6_coordination_events": coord,
        "section_7_schedule_update_diffs": schedule_diffs,
        "section_8_photo_evidence_pack": photos,
        "section_9_gating_entities": gating_entities,
        "section_10_constraint_captures": constraints,
        "section_11_calendar_reconciliation": calendar_rows,
        "section_12_audit_log_extract": audit_entries,
        # Bucket G Phase 4 -- field_suggestions lifecycle timeline.
        # Section 12a in the rendered output.
        "section_12a_field_suggestion_lifecycle": lifecycle_list,
        "section_13_daily_presence": presence,
    }


def _summarize_attribution(delays: list[dict], gates: list[dict]) -> dict:
    by_owner: dict[str, dict] = {}
    for d in delays:
        owner = (d.get("owner_attribution") or "none").lower()
        days = float(d.get("delay_days") or 0)
        slot = by_owner.setdefault(owner, {"events": 0, "delay_days": 0.0})
        slot["events"] += 1
        slot["delay_days"] += days
    for g in gates:
        owner = (g.get("owner_attribution") or "none").lower()
        slot = by_owner.setdefault(owner, {"events": 0, "delay_days": 0.0})
        slot.setdefault("gates", 0)
        slot["gates"] += 1
    for slot in by_owner.values():
        slot["delay_days"] = round(slot["delay_days"], 2)
    return {"by_owner": by_owner}


# ============================================================
# Renderers
# ============================================================

def _render_json(data: dict) -> bytes:
    return json.dumps(data, default=str, indent=2).encode("utf-8")


def _render_docx(data: dict) -> bytes:
    """12-section narrative .docx via python-docx. Sections that have
    no data render as 'No events to report' so the document remains
    coherent on empty ranges."""
    from docx import Document
    from docx.shared import Pt, RGBColor

    doc = Document()
    meta = data.get("_meta", {})
    proj = meta.get("project", {}) or {}

    title = doc.add_heading(f"{proj.get('name', 'Project')} — Time Impact Analysis Source Document", level=0)
    p = doc.add_paragraph()
    p.add_run(
        f"Generated: {meta.get('generated_at')}  ·  "
        f"Date range: {meta.get('date_range', {}).get('start')} to {meta.get('date_range', {}).get('end')}"
    ).italic = True
    if (meta.get("filters") or {}):
        doc.add_paragraph(
            "Filters applied: " + ", ".join(f"{k}={v}" for k, v in meta["filters"].items() if v)
        )

    def H(t: str, level: int = 1):
        doc.add_heading(t, level=level)

    def P(t: str):
        doc.add_paragraph(t)

    def empty():
        p = doc.add_paragraph()
        r = p.add_run("No events to report.")
        r.italic = True
        r.font.color.rgb = RGBColor(0x88, 0x88, 0x88)

    def table(rows: list[dict], cols: list[tuple[str, str]]):
        if not rows:
            empty(); return
        # Bucket D Phase 4 -- supplements show first column annotated
        # with [SUPP] so the chronological ledger stays honest.
        # Sort: parent date if supplement, else native date field.
        def _sort_key(r):
            return (r.get("_supp_parent_date") or r.get("logged_date")
                    or r.get("date") or r.get("occurred_at") or r.get("created_at") or "")
        rows = sorted(rows, key=_sort_key)
        t = doc.add_table(rows=1 + min(len(rows), 200), cols=len(cols))
        t.style = "Light Grid Accent 1"
        hdr = t.rows[0].cells
        for i, (_, label) in enumerate(cols):
            hdr[i].text = label
        for ridx, row in enumerate(rows[:200], start=1):
            cells = t.rows[ridx].cells
            for cidx, (key, _) in enumerate(cols):
                v = row.get(key)
                if isinstance(v, list):
                    v = ", ".join(str(x) for x in v[:10])
                elif isinstance(v, dict):
                    v = json.dumps(v, default=str)[:200]
                cell_text = str(v) if v is not None else ""
                if cidx == 0 and row.get("_supp"):
                    cell_text = "[SUPP] " + cell_text
                cells[cidx].text = cell_text
        if len(rows) > 200:
            P(f"... and {len(rows) - 200} more (full set in JSON artifact).")

    # SECTION 1
    H("1. Project Header")
    s1 = data["section_1_project_header"]
    info = doc.add_paragraph()
    for k, v in s1.items():
        info.add_run(f"{k.replace('_', ' ').title()}: ").bold = True
        info.add_run(f"{v if v is not None else '—'}\n")

    # SECTION 2
    H("2. Delay Event Ledger")
    delays = data["section_2_delay_event_ledger"] or []
    P(f"{len(delays)} delay event(s) in range.")
    table(delays, [
        ("id", "ID"), ("logged_date", "Date"), ("cause_class", "Type"),
        ("delay_days", "Days"), ("owner_attribution", "Owner"),
        ("linked_task_ids", "Gated activities"), ("status", "Status"),
        ("edits_summary", "Post-submit edits"),
    ])

    # SECTION 3
    H("3. CPM Cascade Analysis")
    cs = data["section_3_cpm_cascade_analysis"] or []
    P(f"{len(cs)} latest cascade snapshot(s) in range.")
    table(cs, [
        ("gating_entity_id", "Gate"), ("severity_band", "Severity"),
        ("sla_status", "SLA"), ("open_age_working_days", "Open age (wd)"),
        ("projected_slip_at_n10_days", "Slip @ N10"),
        ("projected_cp_impact_at_n10_days", "CP impact @ N10"),
        ("projected_cp_impact_at_n20_days", "CP impact @ N20"),
    ])

    # SECTION 4
    H("4. Owner Attribution Summary")
    by_owner = (data["section_4_owner_attribution_summary"] or {}).get("by_owner", {})
    if not by_owner:
        empty()
    else:
        rows = [{"owner": k, **v} for k, v in by_owner.items()]
        table(rows, [
            ("owner", "Owner"), ("events", "Delay events"),
            ("delay_days", "Total delay days"), ("gates", "Open gates"),
        ])

    # SECTION 5
    H("5. Mitigation Log Extract")
    table(data["section_5_mitigations"] or [], [
        ("scenario_label", "Scen."), ("strategy_name", "Strategy"),
        ("decision_status", "Decision"), ("decision_made_at", "Chosen"),
        ("outcome_recorded_at", "Outcome recorded"),
        ("estimated_schedule_impact_days", "Predicted Δ"),
        ("actual_time_recovered_days", "Actual Δ"),
    ])

    # SECTION 6
    H("6. Coordination Event Log Extract")
    table(data["section_6_coordination_events"] or [], [
        ("occurred_at", "When"), ("channel", "Channel"),
        ("direction", "Direction"), ("topic", "Topic"),
        ("summary", "Summary"), ("outcome", "Outcome"),
    ])

    # SECTION 7
    H("7. Schedule Update Diffs")
    table(data["section_7_schedule_update_diffs"] or [], [
        ("computed_at", "Computed"), ("activities_added_count", "Added"),
        ("activities_removed_count", "Removed"),
        ("activities_modified_count", "Modified"),
        ("cp_changes_count", "CP changes"),
        ("total_change_percentage", "Change %"),
    ])

    # SECTION 8
    H("8. Photo Evidence Pack (discovery + damage)")
    photo_rows = data["section_8_photo_evidence_pack"] or []
    # Bucket F Phase 4 -- expanded photo table with AI lineage columns.
    # Counsel needs to see (a) what the AI suggested at upload time,
    # (b) confidence the suggestion was made with, (c) whether the
    # user overrode and what the user wrote instead, and (d) safety
    # concerns the model flagged. The override-detail copy renders as
    # a separate paragraph block under the table for any photo whose
    # tag was authored over an AI suggestion AND ai_user_overridden=true.
    table(photo_rows, [
        ("id", "ID"), ("photo_class", "Class"), ("caption", "Caption"),
        ("device_timestamp", "Device ts"),
        ("server_received_timestamp", "Server ts"),
        ("timestamp_mismatch_flag", "TS mismatch"),
        ("sha256_hash", "SHA-256"),
        ("ai_suggested_class", "AI class"),
        ("ai_suggested_caption", "AI caption"),
        ("ai_confidence_score", "AI conf."),
        ("ai_user_overridden", "User override"),
        ("ai_safety_concerns_detected", "AI safety flags"),
        ("created_via_ai_suggestion", "AI-authored tag"),
        ("ai_reused_from_phash", "Analysis reused"),
        ("edits_summary", "Post-submit edits"),
    ])
    # Per-photo override-detail block: only renders when a photo's tag
    # was authored over an AI suggestion AND the user changed at least
    # one suggested field. Renders one bullet per affected photo with
    # the exact spec-required copy.
    overrides = [p for p in photo_rows if p.get("ai_user_overridden")]
    if overrides:
        H("8a. AI Suggestion Overrides", level=2)
        P(
            f"{len(overrides)} photo(s) where the user changed an AI-suggested "
            f"value at tag time. The original AI suggestion and the user-recorded "
            f"reason are preserved verbatim for cross-examination purposes."
        )
        for p in overrides:
            para = doc.add_paragraph(style="List Bullet")
            r0 = para.add_run(f"Photo {p.get('id') or '—'}: ")
            r0.bold = True
            para.add_run(p.get("ai_override_detail") or "")

    # SECTION 9
    H("9. Gating Entities Log (RFI / Submittal / Field Issue)")
    table(data["section_9_gating_entities"] or [], [
        ("entity_number", "Number"), ("entity_type", "Type"),
        ("display_label", "Label"), ("submitted_at", "Submitted"),
        ("status", "Status"), ("owner_attribution", "Owner"),
        ("is_over_sla", "Over SLA"),
        ("edits_summary", "Post-submit edits"),
    ])

    # SECTION 10
    H("10. Constraint Captures (daily-report grid)")
    table(data["section_10_constraint_captures"] or [], [
        ("report_date", "Date"), ("activity_code", "Activity"),
        ("constraint_type", "Type"), ("constraint_reference_entity_id", "Gate"),
        ("production_impact_hours", "Impact (h)"),
        ("constraint_auto_filled", "Auto-filled"),
    ])

    # SECTION 11
    H("11. Working-Day / Calendar-Day Reconciliation")
    table(data["section_11_calendar_reconciliation"] or [], [
        ("date", "Date"), ("is_working_day", "Working"),
        ("is_holiday", "Holiday"), ("is_building_closed", "Closed"),
        ("is_restricted_access", "Restricted"),
        ("weather_impact_hours", "Weather (h)"),
        ("production_hours", "Production (h)"),
        ("headcount_total", "Headcount"),
        # Addition 3 -- explicit no-work-day acknowledgments surface
        # here so legal sees "this was a planned blank day" rather
        # than "looks like the super forgot to log anything."
        ("no_work_reason", "No-work reason"),
        ("no_work_note", "No-work note"),
    ])

    # SECTION 12
    H("12. Audit Log Extract (relevant entities)")
    audit = data["section_12_audit_log_extract"] or []
    P(f"{len(audit)} audit entr(ies) in range. SHA-256 chain integrity is verifiable via /api/audit-log/verify.")
    table(audit, [
        ("created_at", "When"), ("table_name", "Table"), ("op", "Op"),
        ("record_id", "Record"), ("actor_id", "Actor"), ("hash", "Hash"),
    ])

    # SECTION 12a -- field_suggestions lifecycle timeline.
    # Counsel needs the "AI suggested X / human did Y" trail. We
    # render one bullet per suggestion in chronological order with
    # source, confidence, suggested value, and the human terminal
    # decision (confirmed / rejected with reason / superseded /
    # expired). Bucket G Phase 4.
    H("12a. AI Suggestion Lifecycle (Jigsaw audit trail)", level=2)
    lifecycle = data.get("section_12a_field_suggestion_lifecycle") or []
    P(
        f"{len(lifecycle)} AI suggestion(s) in range. Each row shows "
        f"the AI's proposal and the human-confirmation outcome -- the "
        f"federal-audit evidence chain for AI-assisted decisions."
    )
    if not lifecycle:
        empty()
    else:
        for b in lifecycle:
            sid = b.get("suggestion_id") or "—"
            entity = (b.get("entity_type") or "—").replace("_", " ")
            field_path = b.get("field_path") or "—"
            source = b.get("suggested_by") or "—"
            conf = b.get("confidence_score")
            conf_str = (
                f", conf {conf:.2f}" if isinstance(conf, (int, float)) else ""
            )
            val = b.get("suggested_value")
            if isinstance(val, (dict, list)):
                val_str = json.dumps(val, default=str)[:160]
            else:
                val_str = str(val) if val is not None else "—"
            created_at = (b.get("created_at") or "")[:19]
            terminal_op = b.get("terminal_op")
            terminal_at = (b.get("terminal_at") or "")[:19]

            # Build the line. Two paragraphs per suggestion: the AI
            # proposal + the human terminal decision.
            p = doc.add_paragraph(style="List Bullet")
            r0 = p.add_run(f"[{sid[:8]}] ")
            r0.bold = True
            p.add_run(
                f"{entity}.{field_path} -- AI suggested '{val_str}' "
                f"on {created_at} (source: {source}{conf_str})."
            )
            decision = doc.add_paragraph()
            decision.paragraph_format.left_indent = Pt(18)
            if terminal_op == "confirmed":
                r1 = decision.add_run("Confirmed")
                r1.font.color.rgb = RGBColor(0x0A, 0x6E, 0x3A)  # emerald
                r1.bold = True
                actor = b.get("confirmed_by") or "—"
                role = b.get("user_role")
                decision.add_run(
                    f" by {str(actor)[:36]}"
                    + (f" ({role})" if role else "")
                    + (f" at {terminal_at}." if terminal_at else ".")
                )
            elif terminal_op == "rejected":
                r1 = decision.add_run("Rejected")
                r1.font.color.rgb = RGBColor(0xB1, 0x2A, 0x2A)  # rose
                r1.bold = True
                reason = b.get("rejection_reason") or "(no reason recorded)"
                decision.add_run(
                    f" at {terminal_at}. Reason: {reason[:240]}"
                )
            elif terminal_op == "superseded":
                r1 = decision.add_run("Superseded by newer AI suggestion")
                r1.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
                decision.add_run(f" at {terminal_at}.")
            elif terminal_op == "expired":
                r1 = decision.add_run("Auto-expired (30+ days pending)")
                r1.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
                decision.add_run(f" at {terminal_at}.")
            else:
                r1 = decision.add_run("Pending (no terminal decision yet)")
                r1.italic = True
                r1.font.color.rgb = RGBColor(0xB8, 0x80, 0x00)

    # Daily presence appendix (numbered 13 to keep canonical 12 above)
    H("Appendix A. Daily Presence Roster")
    table(data.get("section_13_daily_presence") or [], [
        ("report_date", "Date"), ("trade", "Trade"),
        ("sub_name", "Sub"), ("zone_name", "Zone"),
        ("shift", "Shift"), ("headcount", "Headcount"),
        ("hours_per_person", "Hrs/person"), ("total_hours", "Total hrs"),
    ])

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _render_pdf(data: dict, generation_id: str) -> bytes:
    """Evidence-index PDF: one entry per evidence item with hyperlinks
    back to the platform record (using the deployed app's URL). Spec
    calls for navigable hyperlinks; we embed clickable a-tags that
    WeasyPrint preserves in PDF output."""
    from weasyprint import HTML

    meta = data.get("_meta", {})
    proj = meta.get("project", {}) or {}

    def _link(label: str, path: str) -> str:
        return "<a href='" + html.escape(path) + "'>" + html.escape(str(label)) + "</a>"

    def _section(heading: str, items: list[dict], render_row) -> str:
        if not items:
            return "<h2>" + html.escape(heading) + "</h2><p class='empty'>No entries.</p>"
        rows = "".join(render_row(i, x) for i, x in enumerate(items, start=1))
        return ("<h2>" + html.escape(heading) + f" ({len(items)})</h2>"
                "<ol class='index'>" + rows + "</ol>")

    def _delay_row(i, d):
        date_s = html.escape(str(d.get("logged_date") or ""))
        desc = html.escape(str(d.get("description") or d.get("id"))[:120])
        link = _link("detail", "/delays/" + str(d.get("id") or ""))
        return f"<li>{date_s} — {desc} ({link})</li>"

    def _gate_row(i, g):
        num = html.escape(str(g.get("entity_number") or ""))
        lbl = html.escape(str(g.get("display_label") or "")[:120])
        link = _link("detail", "/gating-entities/" + str(g.get("id") or ""))
        return f"<li>{num} — {lbl} ({link})</li>"

    def _coord_row(i, c):
        when = html.escape(str(c.get("occurred_at") or ""))
        summary = html.escape(str(c.get("summary") or "")[:120])
        link = _link("detail", "/coordination/" + str(c.get("id") or ""))
        return f"<li>{when} — {summary} ({link})</li>"

    def _diff_row(i, d):
        when = html.escape(str(d.get("computed_at") or ""))
        added = d.get("activities_added_count", 0)
        modified = d.get("activities_modified_count", 0)
        cp = d.get("cp_changes_count", 0)
        link = _link("detail", "/schedule-diffs/" + str(d.get("id") or ""))
        return f"<li>{when} — {added} added / {modified} modified / {cp} CP ({link})</li>"

    def _photo_row(i, p):
        cls = html.escape(str(p.get("photo_class") or ""))
        ts = html.escape(str(p.get("device_timestamp") or p.get("server_received_timestamp") or ""))
        cap = html.escape(str(p.get("caption") or "")[:80])
        sha = html.escape(str(p.get("sha256_hash") or "")[:16])
        # Bucket F Phase 4 -- inline AI lineage on each row. The block
        # is rendered as a sub-paragraph so the index stays scannable
        # but counsel can see the AI provenance without flipping to
        # the .docx.
        ai_bits: list[str] = []
        if p.get("ai_suggested_class"):
            conf = p.get("ai_confidence_score")
            conf_s = f" ({conf:.0%})" if isinstance(conf, (int, float)) else ""
            ai_bits.append(
                f"AI suggested: {html.escape(str(p.get('ai_suggested_class')))}{conf_s}"
            )
        if p.get("ai_user_overridden"):
            ai_bits.append(
                "<b>user overrode</b> — " + html.escape(str(
                    p.get("ai_override_detail")
                    or "(no override detail recorded)"
                )[:240])
            )
        if p.get("ai_safety_concerns_detected"):
            flags = ", ".join(p["ai_safety_concerns_detected"][:5])
            ai_bits.append(
                f"safety flags: {html.escape(flags)}"
            )
        if p.get("ai_reused_from_phash"):
            ai_bits.append("analysis reused from prior upload")
        ai_html = ""
        if ai_bits:
            ai_html = (
                "<div class='ai-line'>" + " · ".join(ai_bits) + "</div>"
            )
        return (
            f"<li>[{cls}] {ts} — {cap} "
            f"<span class='hash'>sha256:{sha}…</span>"
            f"{ai_html}</li>"
        )

    def _mit_row(i, m):
        label = html.escape(str(m.get("strategy_name") or m.get("scenario_label") or m.get("id"))[:120])
        status = html.escape(str(m.get("decision_status") or ""))
        return f"<li>{label} — decision: {status}</li>"

    delays_html = _section("Delay events", data["section_2_delay_event_ledger"] or [], _delay_row)
    gates_html = _section("Gating entities (RFI / Submittal / Field Issue)",
                            data["section_9_gating_entities"] or [], _gate_row)
    coord_html = _section("Coordination events",
                            data["section_6_coordination_events"] or [], _coord_row)
    diffs_html = _section("Schedule update diffs",
                            data["section_7_schedule_update_diffs"] or [], _diff_row)
    photos_html = _section("Evidentiary photos",
                             data["section_8_photo_evidence_pack"] or [], _photo_row)
    mitigations_html = _section("Mitigations",
                                  data["section_5_mitigations"] or [], _mit_row)

    # Bucket G Phase 4 -- field_suggestion lifecycle rows. Two-line
    # format per row: AI proposal + human terminal decision. Mirrors
    # the DOCX Section 12a.
    def _sugg_row(i, b):
        sid = html.escape(str(b.get("suggestion_id") or "")[:8])
        entity = html.escape(str(b.get("entity_type") or "").replace("_", " "))
        field_path = html.escape(str(b.get("field_path") or ""))
        source = html.escape(str(b.get("suggested_by") or ""))
        conf = b.get("confidence_score")
        conf_str = (
            f" · conf {conf:.2f}" if isinstance(conf, (int, float)) else ""
        )
        val = b.get("suggested_value")
        if isinstance(val, (dict, list)):
            val_str = json.dumps(val, default=str)[:160]
        else:
            val_str = str(val) if val is not None else "—"
        val_str = html.escape(val_str)
        created_at = html.escape(str(b.get("created_at") or "")[:19])
        terminal_op = b.get("terminal_op")
        terminal_at = html.escape(str(b.get("terminal_at") or "")[:19])
        decision_html = ""
        if terminal_op == "confirmed":
            actor = html.escape(str(b.get("confirmed_by") or "—")[:36])
            role = b.get("user_role") or ""
            role_str = f" ({html.escape(str(role))})" if role else ""
            decision_html = (
                "<span class='sugg-confirmed'>Confirmed</span>"
                f" by {actor}{role_str} at {terminal_at}."
            )
        elif terminal_op == "rejected":
            reason = html.escape(str(b.get("rejection_reason") or "(no reason)")[:240])
            decision_html = (
                f"<span class='sugg-rejected'>Rejected</span>"
                f" at {terminal_at}. Reason: {reason}"
            )
        elif terminal_op == "superseded":
            decision_html = (
                f"<span class='sugg-superseded'>Superseded by newer "
                f"AI suggestion</span> at {terminal_at}."
            )
        elif terminal_op == "expired":
            decision_html = (
                f"<span class='sugg-superseded'>Auto-expired</span> at {terminal_at}."
            )
        else:
            decision_html = (
                "<span class='sugg-pending'>Pending</span> "
                "(no terminal decision yet)."
            )
        return (
            f"<li>"
            f"<b>[{sid}]</b> {entity}.{field_path} — "
            f"AI suggested <i>'{val_str}'</i> on {created_at} "
            f"(source: {source}{conf_str}).<br>"
            f"<span class='sugg-decision'>{decision_html}</span>"
            f"</li>"
        )

    suggestions_html = _section(
        "AI Suggestion Lifecycle (12a) — Jigsaw audit trail",
        data.get("section_12a_field_suggestion_lifecycle") or [],
        _sugg_row,
    )

    css = """
      body { font-family: Helvetica, Arial, sans-serif; font-size: 10pt; color: #1a2845; }
      h1 { font-size: 18pt; margin-bottom: 4pt; }
      h2 { font-size: 12pt; border-bottom: 1px solid #d97757; padding-bottom: 2pt; margin-top: 14pt; }
      .meta { color: #666; font-size: 9pt; margin-bottom: 12pt; }
      ol.index li { margin: 3pt 0; line-height: 1.4; }
      a { color: #1a2845; text-decoration: underline; }
      .empty { color: #888; font-style: italic; }
      .privilege { background: #fff5e6; border: 1px solid #d97757; padding: 6pt 10pt; margin: 8pt 0; font-size: 9pt; }
      .hash { color: #999; font-family: monospace; font-size: 8pt; }
      .ai-line { color: #555; font-size: 9pt; margin-top: 2pt; padding-left: 8pt; border-left: 2px solid #d97757; }
      .sugg-decision { color: #444; padding-left: 12pt; font-size: 9pt; display: block; margin-top: 2pt; }
      .sugg-confirmed { color: #0a6e3a; font-weight: bold; }
      .sugg-rejected { color: #b12a2a; font-weight: bold; }
      .sugg-superseded { color: #888; font-weight: bold; }
      .sugg-pending { color: #b88000; font-style: italic; }
    """
    privilege_block = (
        "<div class='privilege'><b>Privileged work product (counsel privileged).</b> "
        "This document is generated for litigation/claim preparation. "
        "Distribution outside the project legal team requires admin unflagging "
        "via /tia/generations/{id}/unflag-privilege.</div>"
    )

    body = f"""
    <html><head><meta charset='utf-8'><style>{css}</style></head>
    <body>
      <h1>Time Impact Analysis — Evidence Index</h1>
      <div class='meta'>
        Project: {html.escape(str(proj.get('name') or ''))}<br>
        Date range: {html.escape(str(meta.get('date_range', {}).get('start') or ''))}
        to {html.escape(str(meta.get('date_range', {}).get('end') or ''))}<br>
        Generated: {html.escape(str(meta.get('generated_at') or ''))}<br>
        Generation ID: {html.escape(generation_id)}
      </div>
      {privilege_block}
      {delays_html}
      {gates_html}
      {coord_html}
      {diffs_html}
      {photos_html}
      {mitigations_html}
      {suggestions_html}
    </body></html>
    """
    return HTML(string=body).write_pdf()


# ============================================================
# Public entrypoint
# ============================================================

async def generate_tia(
    *,
    tenant_id: str, project_id: str,
    start: date, end: date,
    filters: Optional[dict] = None,
    actor_id: Optional[str] = None,
    request_id: Optional[str] = None,
    existing_generation_id: Optional[str] = None,
) -> dict:
    """End-to-end. Returns the persisted tia_generations row.

    Followup-fix 2 (2026-05-07): existing_generation_id supports the
    BackgroundTask path -- the router pre-inserts a status='pending'
    row so its 202 response carries an id, then schedules the BG
    task with that id. generate_tia reuses the row instead of
    inserting a duplicate. The status flips through running on first
    write below."""
    db = get_db()
    filters = filters or {}
    generation_id = existing_generation_id or gen_id()
    now_iso = utc_now().isoformat()

    if not existing_generation_id:
        # Sync path -- insert a fresh placeholder row so concurrent
        # readers see the work-in-progress.
        placeholder = {
            "id": generation_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "generated_at": now_iso,
            "generated_by_user_id": actor_id,
            "status": "generating",
            "error": None,
            "failure_reason": None,
            "date_range_start": start.isoformat(),
            "date_range_end": end.isoformat(),
            "filters_applied": filters,
            "structured_json_artifact_id": None,
            "word_doc_artifact_id": None,
            "evidence_index_pdf_artifact_id": None,
            "delay_events_count": 0,
            "mitigations_count": 0,
            "coordination_events_count": 0,
            "photos_evidentiary_count": 0,
            "schedule_diffs_count": 0,
            "total_evidence_items": 0,
            "counsel_privileged_work_product": TIA_DEFAULT_PRIVILEGE_FLAG,
            "privilege_unflagged_at": None,
            "privilege_unflagged_by": None,
            "privilege_unflag_reason": None,
            "download_log": [],
        }
        await db.tia_generations.insert_one(placeholder.copy())
    else:
        # BG path -- the router already inserted the row at status=pending
        # and the BG wrapper flipped to status=running. Nothing to do here.
        pass

    try:
        data = await _gather_data(tenant_id, project_id, start, end, filters)

        json_bytes = _render_json(data)
        docx_bytes = _render_docx(data)
        pdf_bytes = _render_pdf(data, generation_id)

        # Persist artifacts
        json_path = make_path(tenant_id, "tia", "json")
        word_path = make_path(tenant_id, "tia", "docx")
        pdf_path = make_path(tenant_id, "tia", "pdf")
        put_object(json_path, json_bytes, "application/json")
        put_object(word_path, docx_bytes,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        put_object(pdf_path, pdf_bytes, "application/pdf")

        json_file = await record_file(
            tenant_id=tenant_id, storage_path=json_path,
            original_filename=f"tia_{generation_id[:8]}.json",
            content_type="application/json", size=len(json_bytes),
            kind="tia_artifact", actor_id=actor_id,
        )
        word_file = await record_file(
            tenant_id=tenant_id, storage_path=word_path,
            original_filename=f"tia_{generation_id[:8]}.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size=len(docx_bytes), kind="tia_artifact", actor_id=actor_id,
        )
        pdf_file = await record_file(
            tenant_id=tenant_id, storage_path=pdf_path,
            original_filename=f"tia_evidence_index_{generation_id[:8]}.pdf",
            content_type="application/pdf", size=len(pdf_bytes),
            kind="tia_artifact", actor_id=actor_id,
        )

        delay_n = len(data["section_2_delay_event_ledger"] or [])
        mit_n = len(data["section_5_mitigations"] or [])
        coord_n = len(data["section_6_coordination_events"] or [])
        photos_n = len(data["section_8_photo_evidence_pack"] or [])
        diffs_n = len(data["section_7_schedule_update_diffs"] or [])
        gates_n = len(data["section_9_gating_entities"] or [])
        constr_n = len(data["section_10_constraint_captures"] or [])

        updates = {
            "status": "complete",
            "structured_json_artifact_id": json_file["id"],
            "word_doc_artifact_id": word_file["id"],
            "evidence_index_pdf_artifact_id": pdf_file["id"],
            "delay_events_count": delay_n,
            "mitigations_count": mit_n,
            "coordination_events_count": coord_n,
            "photos_evidentiary_count": photos_n,
            "schedule_diffs_count": diffs_n,
            "total_evidence_items": delay_n + mit_n + coord_n + photos_n + diffs_n + gates_n + constr_n,
        }
        await db.tia_generations.update_one(
            tenant_filter(tenant_id, {"id": generation_id}),
            {"$set": updates},
        )
        final = await db.tia_generations.find_one(
            tenant_filter(tenant_id, {"id": generation_id}), {"_id": 0},
        )
        await write_audit(
            tenant_id=tenant_id, table_name="tia_generations", op="TIA_GENERATED",
            record_id=generation_id, before=None,
            after={
                "date_range_start": start.isoformat(),
                "date_range_end": end.isoformat(),
                "filters": filters,
                "total_evidence_items": updates["total_evidence_items"],
                "counsel_privileged_work_product": True,
            },
            actor_id=actor_id, request_id=request_id,
        )
        return final
    except Exception as e:
        # Mark generation failed; surface to caller via the row.
        await db.tia_generations.update_one(
            tenant_filter(tenant_id, {"id": generation_id}),
            # Followup-fix 2 (2026-05-07): write both `error` (legacy
            # field readers) and `failure_reason` (the field the new BG
            # path + frontend poll look for).
            {"$set": {"status": "failed", "error": str(e)[:500],
                      "failure_reason": str(e)[:500]}},
        )
        try:
            await write_audit(
                tenant_id=tenant_id, table_name="tia_generations",
                op="TIA_GENERATION_FAILED",
                record_id=generation_id, before=None,
                after={"error": str(e)[:500]},
                actor_id=actor_id, request_id=request_id,
            )
        except Exception:
            pass
        raise


async def fetch_artifact_bytes(tenant_id: str, generation_id: str,
                                kind: str) -> Optional[tuple[bytes, str, str]]:
    """Returns (bytes, content_type, filename) for the given artifact
    kind ('json' | 'word' | 'pdf'), or None if the row or the file is
    missing. Used by the download endpoints in routers/tia_router.py."""
    db = get_db()
    row = await db.tia_generations.find_one(
        tenant_filter(tenant_id, {"id": generation_id}), {"_id": 0},
    )
    if not row:
        return None
    artifact_id_key = {
        "json": "structured_json_artifact_id",
        "word": "word_doc_artifact_id",
        "pdf": "evidence_index_pdf_artifact_id",
    }.get(kind)
    if not artifact_id_key:
        return None
    artifact_id = row.get(artifact_id_key)
    if not artifact_id:
        return None
    f = await db.files.find_one(
        tenant_filter(tenant_id, {"id": artifact_id, "is_deleted": {"$ne": True}}),
        {"_id": 0},
    )
    if not f:
        return None
    data, _ct = get_object(f["storage_path"])
    return data, f.get("content_type", "application/octet-stream"), f.get("original_filename", f"tia_{generation_id[:8]}.{kind}")
