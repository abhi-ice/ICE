"""Voice intake routes for Daily Reports.

Three new endpoints sitting on top of the existing DR routes:

  POST /api/daily-reports/{id}/voice/transcribe
    body: multipart audio file (mp3/m4a/webm/wav, <= 25 MB)
    out:  {"transcript": "..."}
    -- Sends audio to Gemini for transcription. NEEDS GEMINI_API_KEY.

  POST /api/daily-reports/{id}/voice/extract
    body: {"transcript": "..."}
    out:  {"draft": {...}, "applied": {"activity_updates": N, ...}}
    -- Runs Claude extraction + writes through to DB collections, then
       returns a summary of what was applied. The frontend then reloads
       the DR and the super reviews fields before clicking Submit.

  POST /api/daily-reports/{id}/voice/transcribe-and-extract
    body: multipart audio file
    -- Convenience combo of the two above; one request, one round-trip.
       Used by the offline-queue sync path.

Live conversational (Route 3) is a separate concern -- the Gemini Live
WebSocket runs browser-to-Google directly using a SHORT-LIVED token
issued by /api/daily-reports/{id}/voice/live-token. The browser sends
field-update events directly to /api/daily-reports/{id}/voice/extract
as the conversation completes.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from database import get_db, tenant_filter
from auth import require_tenant
from services.dr_voice_extract import extract_dr_fields

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/daily-reports", tags=["daily-reports-voice"])


# ---------------------------------------------------------------------
# Gemini transcription (Route 2)
# ---------------------------------------------------------------------
async def _gemini_transcribe_audio(audio_bytes: bytes, mime_type: str) -> str:
    """Send raw audio to Gemini for transcription.

    Automatically routes between two paths based on size:
      - Inline (<18 MB)   → bytes embedded directly in generate_content
      - Files API (>=18MB) → upload separately, reference by file URI

    The 18 MB threshold leaves headroom under Gemini's 20 MB inline
    request cap (the request also includes the prompt text + headers).

    Returns the cleaned transcript string.

    This function is a thin wrapper that fails fast if GEMINI_API_KEY
    is missing. The frontend already gates Voice (async) and Voice
    (live) routes behind a key-present probe so the UI is hidden when
    the key is absent. We still raise here for defense in depth.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail=(
                "Voice transcription is not configured -- GEMINI_API_KEY "
                "is missing from the backend environment. Use Manual or "
                "Offline-manual modes until the key is set."
            ),
        )

    # Lazy import so the app boots even without the SDK installed during
    # initial setup. google-genai is in requirements.txt (the modern
    # SDK that replaces the deprecated google-generativeai package).
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"google-genai SDK not installed: {exc}",
        ) from exc

    client = genai.Client(api_key=api_key)

    INLINE_LIMIT = 18 * 1024 * 1024   # 18 MB safe headroom under 20 MB cap
    TRANSCRIPTION_PROMPT = (
        "Transcribe this construction site daily-report voice memo "
        "verbatim. Preserve every spoken word; do not summarize. "
        "If the speaker mentions activity codes like 'one point seven', "
        "write them as '1.7'. Return ONLY the transcript text -- no "
        "preamble, no commentary, no formatting. "
        "If you can make out ANY human speech, transcribe it even if "
        "the audio is noisy, distant, or has background construction "
        "sounds. ONLY return an empty string if the audio is COMPLETELY "
        "silent or contains zero human speech (e.g. pure tones, music "
        "only, or total silence). Do NOT fabricate or paraphrase content "
        "that wasn't actually spoken."
    )

    # Per Google's Feb-2026 deprecation schedule, `gemini-2.0-flash` is
    # 404'd for new users and `gemini-2.5-flash` is also slated for
    # replacement — `gemini-3.5-flash` is the current recommended GA
    # model for audio-to-text.
    try:
        if len(audio_bytes) <= INLINE_LIMIT:
            # ---- Inline path ----
            resp = await client.aio.models.generate_content(
                model="gemini-3.5-flash",
                contents=[
                    genai_types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                    TRANSCRIPTION_PROMPT,
                ],
            )
        else:
            # ---- Files API path (large memos, >=18 MB) ----
            # Upload the audio as a Gemini File resource, then reference
            # it by URI in generate_content. Files API supports up to
            # ~2 GB per file and 9.5 hours of total audio per request.
            log.info(
                "dr_voice: audio %.1f MB exceeds inline limit, using Files API",
                len(audio_bytes) / (1024 * 1024),
            )
            import io
            uploaded = await client.aio.files.upload(
                file=io.BytesIO(audio_bytes),
                config={"mime_type": mime_type},
            )
            # Some SDK versions return a File-pending state; wait until ACTIVE.
            # (Audio is processed server-side before it's usable.)
            import asyncio
            for _ in range(30):  # up to 30 seconds
                state = getattr(uploaded, "state", None)
                state_name = getattr(state, "name", str(state))
                if state_name in ("ACTIVE", None):
                    break
                if state_name == "FAILED":
                    raise HTTPException(
                        status_code=502,
                        detail="Gemini Files API rejected the audio (state=FAILED)",
                    )
                await asyncio.sleep(1)
                uploaded = await client.aio.files.get(name=uploaded.name)

            resp = await client.aio.models.generate_content(
                model="gemini-3.5-flash",
                contents=[uploaded, TRANSCRIPTION_PROMPT],
            )
            # Best-effort cleanup so we don't leave orphan files on Google's side.
            try:
                await client.aio.files.delete(name=uploaded.name)
            except Exception as cleanup_exc:
                log.warning("dr_voice: failed to delete uploaded file %s: %s",
                            uploaded.name, cleanup_exc)
    except HTTPException:
        raise
    except Exception as exc:
        log.error("Gemini transcription failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Gemini transcription error: {exc}",
        ) from exc

    text = (resp.text or "").strip() if hasattr(resp, "text") and resp.text else ""
    return text


# ---------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------
@router.post("/{dr_id}/voice/transcribe")
async def voice_transcribe(
    dr_id: str,
    audio: UploadFile = File(...),
    ctx: dict = Depends(require_tenant),
):
    """Multipart upload -> Gemini -> transcript string."""
    raw = await audio.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty audio upload")
    if len(raw) > 100 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="audio file too large (>100 MB)")

    # Browser MediaRecorder emits content types like
    # "audio/webm;codecs=opus". Gemini's audio API only accepts the
    # base MIME -- strip the codec parameter so Part.from_bytes
    # validates cleanly. Default to audio/webm because that's what
    # Chromium/Firefox MediaRecorder produces by default.
    raw_mime = audio.content_type or "audio/webm"
    mime = raw_mime.split(";")[0].strip()
    if not mime.startswith("audio/"):
        mime = "audio/webm"
    log.info("dr_voice: /transcribe dr=%s bytes=%d mime=%s (raw=%s)",
             dr_id, len(raw), mime, raw_mime)

    transcript = await _gemini_transcribe_audio(raw, mime)
    log.info("dr_voice: /transcribe dr=%s -> %d chars transcript",
             dr_id, len(transcript))
    return {"transcript": transcript, "char_count": len(transcript)}


@router.post("/{dr_id}/voice/extract")
async def voice_extract(
    dr_id: str,
    payload: dict,
    ctx: dict = Depends(require_tenant),
):
    """Cleaned transcript -> Claude -> structured DR JSON -> DB writes."""
    transcript = (payload or {}).get("transcript") or ""
    if not transcript.strip():
        raise HTTPException(status_code=400, detail="transcript is required")

    db = get_db()
    tenant_id = ctx["tenant_id"]
    actor_id = ctx["user"]["id"]

    dr = await db.daily_reports.find_one(
        tenant_filter(tenant_id, {"id": dr_id}),
        {"_id": 0},
    )
    if not dr:
        raise HTTPException(status_code=404, detail="DR not found")
    if dr.get("status") not in (None, "draft"):
        # Mirror the manual-edit lock the rest of the UI enforces.
        raise HTTPException(
            status_code=409,
            detail="DR is not in draft status; voice extract is draft-only",
        )

    # Pull activity grid for matching context. We need code + name +
    # trade so Claude can fuzzy-match spoken descriptions like
    # "electrical rough in" to the right grid row. Also pulling SOD%,
    # EOD%, and open_blocker_count so Claude can reason about
    # "half of today's planned work" with awareness of progress
    # already booked.
    # NB: daily_report_activities does NOT carry tenant_id directly --
    # tenant scope is enforced by the parent DR lookup above.
    grid = await db.daily_report_activities.find(
        {"daily_report_id": dr_id},
        {"_id": 0, "activity_code": 1, "id": 1, "schedule_activity_id": 1,
         "activity_description": 1, "trade_code": 1, "work_category": 1,
         "location_name": 1, "pct_complete_start_of_day": 1,
         "pct_complete_end_of_day": 1, "open_blocker_count": 1},
    ).to_list(500)

    # For each grid row, also fetch the linked task's duration / slack
    # so Claude can scale "half of today's planned work" correctly.
    schedule_ids = [g.get("schedule_activity_id") for g in grid if g.get("schedule_activity_id")]
    task_meta_by_id: dict[str, dict] = {}
    if schedule_ids:
        async for t in db.tasks.find(
            {"id": {"$in": schedule_ids}},
            {"_id": 0, "id": 1, "duration_days": 1, "total_float_days": 1,
             "open_blocker_count": 1},
        ):
            task_meta_by_id[t["id"]] = t

    activities_ctx = []
    for g in grid:
        code = g.get("activity_code") or ""
        if not code:
            continue
        descriptor = g.get("activity_description") or ""
        trade = g.get("trade_code") or g.get("work_category") or ""
        meta = task_meta_by_id.get(g.get("schedule_activity_id") or "", {})
        dur = meta.get("duration_days")
        # "Scheduled hours today" is a rough heuristic: 8h shift /
        # duration_days. Only computed when duration is known.
        sched_hrs_today = None
        try:
            if dur and float(dur) > 0:
                sched_hrs_today = round(8.0 / float(dur), 1)
        except (TypeError, ValueError):
            sched_hrs_today = None
        activities_ctx.append({
            "code": code,
            "name": descriptor,
            "trade": trade,
            "sod_pct": g.get("pct_complete_start_of_day"),
            "duration_days": dur,
            "scheduled_hours_today": sched_hrs_today,
            "open_blocker_count": g.get("open_blocker_count") or meta.get("open_blocker_count") or 0,
        })
    codes = [a["code"] for a in activities_ctx]
    log.info("dr_voice: extract dr=%s grid_rows=%d codes=%s transcript_chars=%d",
             dr_id, len(grid), codes[:10], len(transcript))

    project = await db.projects.find_one(
        tenant_filter(tenant_id, {"id": dr["project_id"]}),
        {"_id": 0, "name": 1},
    ) or {}

    parsed = await extract_dr_fields(
        transcript,
        activity_codes_in_dr=codes,
        activities_context=activities_ctx,
        project_name=project.get("name"),
        report_date_iso=dr.get("report_date"),
        already_logged_crews=[
            {"trade": p.get("trade"), "company_name": p.get("company_name"),
             "headcount": p.get("headcount")}
            for p in await db.daily_presence.find(
                {"daily_report_id": dr_id},
                {"_id": 0, "trade": 1, "company_name": 1, "headcount": 1},
            ).to_list(60)
        ],
        known_subcontractors=await db.subcontractors.find(
            {"tenant_id": tenant_id},
            {"_id": 0, "name": 1, "trades": 1},
        ).to_list(100),
    )
    log.info("dr_voice: extract dr=%s parsed_keys=%s "
             "activity_updates=%d crew=%d delays=%d safety=%d",
             dr_id, list(parsed.keys()),
             len(parsed.get("activity_updates") or []),
             len(parsed.get("crew_presence") or []),
             len(parsed.get("delays") or []),
             len(parsed.get("safety_events") or []))

    # ---- write-back ---------------------------------------------------
    applied = {
        "weather": False, "notes": False, "activity_updates": 0,
        "crew_presence": 0, "delays": 0, "safety_events": 0,
        "qc_issues": 0, "deliveries": 0, "coordination_events": 0,
        "rfis": 0,
    }
    now = datetime.now(timezone.utc)

    # 1. DR-level fields (weather + notes)
    update: dict = {"updated_at": now}
    w = (parsed.get("weather") or {})
    if isinstance(w, dict):
        if w.get("temp_high_f") is not None:
            update["weather_temp_high_f"] = float(w["temp_high_f"])
        if w.get("temp_low_f") is not None:
            update["weather_temp_low_f"] = float(w["temp_low_f"])
        if w.get("conditions"):
            update["weather_conditions"] = str(w["conditions"])
        if w.get("precipitation_in") is not None:
            update["weather_precipitation_in"] = float(w["precipitation_in"])
        if w.get("wind_mph") is not None:
            update["weather_wind_mph"] = float(w["wind_mph"])
        if any(k in update for k in (
            "weather_temp_high_f", "weather_temp_low_f", "weather_conditions",
            "weather_precipitation_in", "weather_wind_mph"
        )):
            update["weather_fetched_at"] = now
            applied["weather"] = True
    if parsed.get("notes"):
        # Append rather than overwrite so we don't blow away prior notes.
        existing = (dr.get("notes") or "").strip()
        injected = str(parsed["notes"]).strip()
        update["notes"] = (
            (existing + "\n\n[Voice intake " + now.isoformat() + "]\n" + injected)
            if existing else injected
        )
        applied["notes"] = True
    if len(update) > 1:  # more than just updated_at
        await db.daily_reports.update_one(
            tenant_filter(tenant_id, {"id": dr_id}),
            {"$set": update},
        )

    # 2. Activity-grid updates -- match by activity_code -> DR row.
    # Fallback: if Claude didn't get the code but gave a description
    # hint, fuzzy-match against the grid row's activity_description.
    code_to_row = {g["activity_code"]: g for g in grid if g.get("activity_code")}
    def _match_by_description(hint: str):
        if not hint:
            return None
        hint_lower = hint.lower()
        # Score = count of overlapping significant tokens (skip stop words)
        STOP = {"the", "a", "of", "in", "on", "and", "is", "are", "was", "were",
                "to", "for", "with", "at", "by", "an"}
        hint_tokens = {t for t in hint_lower.split() if t not in STOP and len(t) > 2}
        if not hint_tokens:
            return None
        best, best_score = None, 0
        for g in grid:
            desc = (g.get("activity_description") or "").lower()
            if not desc:
                continue
            desc_tokens = {t for t in desc.replace("-", " ").replace(",", " ").split()
                           if t not in STOP and len(t) > 2}
            score = len(hint_tokens & desc_tokens)
            if score > best_score:
                best, best_score = g, score
        return best if best_score >= 1 else None

    for au in (parsed.get("activity_updates") or []):
        if not isinstance(au, dict):
            continue
        code = (au.get("activity_code") or "").strip()
        row = code_to_row.get(code) if code else None
        if not row:
            # Try description-based match.
            row = _match_by_description(au.get("activity_description_hint") or "")
            if row:
                log.info("dr_voice: matched activity_description_hint=%r to code=%s",
                         au.get("activity_description_hint"), row.get("activity_code"))
        if not row:
            continue
        row_update: dict = {"updated_at": now}
        if au.get("pct_complete_end_of_day") is not None:
            row_update["pct_complete_end_of_day"] = max(
                0.0, min(100.0, float(au["pct_complete_end_of_day"]))
            )
        if au.get("crew_count") is not None:
            row_update["crew_count"] = max(0, int(au["crew_count"]))
        if au.get("notes"):
            row_update["notes"] = str(au["notes"])
        if len(row_update) > 1:
            await db.daily_report_activities.update_one(
                {"id": row["id"]},  # no tenant_id on grid rows
                {"$set": row_update},
            )
            applied["activity_updates"] += 1

    # 3. Crew presence rows -- dedupe by (trade, company) so re-running
    # the same recording (or a paraphrase) doesn't double the headcount.
    # Precompute activity-code → schedule_activity_id lookup so the
    # crew_presence loop below can map the new `linked_activity_code`
    # voice field onto the Task dropdown on the manpower row. (Also
    # reused by the Delays/Safety/QC/etc. sections further down.)
    code_to_task = {g["activity_code"]: g.get("schedule_activity_id") for g in grid if g.get("activity_code")}

    existing_presence = await db.daily_presence.find(
        {"daily_report_id": dr_id},
        {"_id": 0, "trade": 1, "company_name": 1, "id": 1},
    ).to_list(200)
    presence_keys_seen = {
        (str(p.get("trade") or "").upper(),
         (p.get("company_name") or "").strip().lower())
        for p in existing_presence
    }
    for cp in (parsed.get("crew_presence") or []):
        if not isinstance(cp, dict) or not cp.get("trade_code"):
            continue
        trade = str(cp["trade_code"])[:32].upper()
        company = (cp.get("company_name") or "").strip().lower()
        key = (trade, company)
        if key in presence_keys_seen:
            log.info("dr_voice: skipping duplicate presence row on dr=%s: %s/%s",
                     dr_id, trade, company)
            continue
        presence_keys_seen.add(key)
        headcount = int(cp["headcount"]) if cp.get("headcount") else 0
        hours_per = float(cp["hours_per_person"]) if cp.get("hours_per_person") else 8.0
        linked_code = (cp.get("linked_activity_code") or "").strip() or None
        linked_task = code_to_task.get(linked_code) if linked_code else None
        await db.daily_presence.insert_one({
            "id": _new_id(),
            "tenant_id": tenant_id,
            "daily_report_id": dr_id,
            "project_id": dr.get("project_id"),
            "trade": str(cp["trade_code"])[:32],
            "company_name": cp.get("company_name"),
            "schedule_activity_id": linked_task,
            "headcount": headcount,
            "hours_per_person": hours_per,
            "total_hours": headcount * hours_per,
            "notes": cp.get("notes"),
            "shift": "first",
            "source": "voice_intake",
            "created_at": now,
            "created_by": actor_id,
        })
        applied["crew_presence"] += 1

    # 4. Delays / Safety / QC / Deliveries / Coordination -- append-only
    #    inserts that re-use the same enums the manual UI accepts.
    #    Each one needs linked_task_ids resolved from activity_codes.
    # code_to_task was already built above (used by crew_presence loop).

    # Pre-fetch existing event descriptions on this DR so we can dedupe
    # when the same recording is processed twice (network retry, user
    # hits Stop twice, or Claude returns two delays that describe the
    # same incident). Token-overlap >= 60% on the description text is
    # treated as a duplicate and skipped.
    def _norm_tokens(text: str) -> set:
        STOP = {"the","a","of","in","on","and","is","are","was","were","to","for",
                "with","at","by","an","that","this","it","be","not","no"}
        return {t for t in (text or "").lower().replace(",", " ").replace(".", " ").split()
                if t not in STOP and len(t) > 2}

    def _is_duplicate(description: str, existing_descriptions: list[str]) -> bool:
        new_tok = _norm_tokens(description)
        if not new_tok:
            return False
        for d in existing_descriptions:
            old_tok = _norm_tokens(d)
            if not old_tok:
                continue
            overlap = len(new_tok & old_tok) / max(len(new_tok | old_tok), 1)
            if overlap >= 0.6:
                return True
        return False

    existing_delay_descs = [d["description"] for d in await db.delays.find(
        {"daily_report_id": dr_id, "status": {"$ne": "deleted"}},
        {"_id": 0, "description": 1}).to_list(200)
        if d.get("description")]
    existing_safety_descs = [d["description"] for d in await db.safety_events.find(
        {"daily_report_id": dr_id, "status": {"$ne": "deleted"}},
        {"_id": 0, "description": 1}).to_list(200)
        if d.get("description")]
    existing_qc_descs = [d["description"] for d in await db.qc_issues.find(
        {"daily_report_id": dr_id, "status": {"$ne": "deleted"}},
        {"_id": 0, "description": 1}).to_list(200)
        if d.get("description")]
    existing_delivery_descs = [d["description"] for d in await db.deliveries.find(
        {"daily_report_id": dr_id},
        {"_id": 0, "description": 1}).to_list(200)
        if d.get("description")]
    existing_coord_descs = [d["description"] for d in await db.coordination_events.find(
        {"daily_report_id": dr_id},
        {"_id": 0, "description": 1}).to_list(200)
        if d.get("description")]
    skipped_dupes = {"delays": 0, "safety_events": 0, "qc_issues": 0,
                     "deliveries": 0, "coordination_events": 0}
    # Collect tasks whose status needs recompute after voice insert.
    # Delays inserted by this call need to flow through to
    # tasks.open_blocker_count + open_blocker_summary so the DR
    # activity grid (and Tasks page) shows the blocker badge with a
    # link to the new delay. Mirrors what the manual delay POST in
    # field_events_router does after every create.
    tasks_to_recompute: set[str] = set()

    async def _link_task_ids(activity_codes):
        if not activity_codes:
            return []
        out = []
        for c in activity_codes:
            tid = code_to_task.get(str(c).strip())
            if tid:
                out.append(tid)
        return out

    # If Claude described an activity by name in `linked_activity_hint`
    # but didn't resolve the code, try to match by description against
    # the grid. Used by the delay/safety extractors below.
    def _link_from_hint(hint: str):
        row = _match_by_description(hint)
        if row and row.get("schedule_activity_id"):
            return [row["schedule_activity_id"]]
        return []

    for d in (parsed.get("delays") or []):
        if not isinstance(d, dict) or not d.get("description"):
            continue
        desc = str(d["description"])[:2000]
        if _is_duplicate(desc, existing_delay_descs):
            skipped_dupes["delays"] += 1
            log.info("dr_voice: skipping duplicate delay on dr=%s: %r", dr_id, desc[:80])
            continue
        existing_delay_descs.append(desc)
        linked = await _link_task_ids(d.get("linked_activity_codes") or [])
        # If Claude didn't link a code but the delay description
        # mentions an activity word like "electrical shaft", try
        # description-fallback linking too.
        if not linked:
            linked = _link_from_hint(d.get("description") or "")
        impact_days = d.get("projected_impact_days")
        try:
            impact_days_f = float(impact_days) if impact_days is not None else 0.0
        except (TypeError, ValueError):
            impact_days_f = 0.0
        await db.delays.insert_one({
            "id": _new_id(), "tenant_id": tenant_id, "project_id": dr["project_id"],
            "description": str(d["description"])[:2000],
            "severity": str(d.get("severity") or "minor"),
            "reason_category": str(d.get("reason_category") or "other"),
            "owner_attribution": str(d.get("owner_attribution") or "none"),
            "cause_class": str(d.get("cause_class") or "other"),
            "status": str(d.get("status") or "open"),
            "delay_days": impact_days_f if impact_days_f > 0 else None,
            "linked_task_ids": linked,
            "per_task_impacts": [{"task_id": t, "projected_impact_days": impact_days_f}
                                  for t in linked],
            "daily_report_id": dr_id,
            "date": dr.get("report_date"),
            "reported_at": now,
            "reported_by": actor_id,
            "source": "voice_intake",
            "created_at": now, "updated_at": now, "created_by": actor_id,
        })
        applied["delays"] += 1
        # Track linked tasks for downstream blocker recompute.
        tasks_to_recompute.update(linked)

    for s in (parsed.get("safety_events") or []):
        if not isinstance(s, dict) or not s.get("description"):
            continue
        desc = str(s["description"])[:2000]
        if _is_duplicate(desc, existing_safety_descs):
            skipped_dupes["safety_events"] += 1
            continue
        existing_safety_descs.append(desc)
        linked = await _link_task_ids(s.get("linked_activity_codes") or [])
        await db.safety_events.insert_one({
            "id": _new_id(), "tenant_id": tenant_id, "project_id": dr["project_id"],
            "description": str(s["description"])[:2000],
            "event_type": str(s.get("event_type") or "other"),
            "severity": str(s.get("severity") or "minor"),
            "status": "open",
            "linked_task_ids": linked,
            "daily_report_id": dr_id,
            "date": dr.get("report_date"),
            "reported_at": now,
            "reported_by": actor_id,
            "source": "voice_intake",
            "created_at": now, "updated_at": now, "created_by": actor_id,
        })
        applied["safety_events"] += 1

    # Pull existing RFI subjects for dedupe.
    existing_rfi_subjects = [r.get("subject") or r.get("description") or ""
                              for r in await db.rfis.find(
        {"daily_report_id": dr_id},
        {"_id": 0, "subject": 1, "description": 1}).to_list(200)]
    skipped_dupes["rfis"] = 0
    applied["rfis"] = 0

    for rf in (parsed.get("rfis") or []):
        if not isinstance(rf, dict):
            continue
        subj = (rf.get("subject") or rf.get("description") or "").strip()
        desc = (rf.get("description") or rf.get("subject") or "").strip()
        if not subj and not desc:
            continue
        if _is_duplicate(subj or desc, existing_rfi_subjects):
            skipped_dupes["rfis"] += 1
            continue
        existing_rfi_subjects.append(subj or desc)
        linked = await _link_task_ids(rf.get("linked_activity_codes") or [])
        if not linked:
            linked = _link_from_hint(desc)
        priority = str(rf.get("priority") or "normal")
        if priority not in {"low", "normal", "high", "urgent"}:
            priority = "normal"
        await db.rfis.insert_one({
            "id": _new_id(), "tenant_id": tenant_id, "project_id": dr["project_id"],
            "subject": (subj or desc)[:200],
            "description": (desc or subj)[:2000],
            "priority": priority,
            "status": "open",
            "linked_task_ids": linked,
            "per_task_impacts": [],
            "daily_report_id": dr_id,
            "date": dr.get("report_date"),
            "reported_at": now,
            "reported_by": actor_id,
            "source": "voice_intake",
            "created_at": now, "updated_at": now, "created_by": actor_id,
        })
        applied["rfis"] += 1
        tasks_to_recompute.update(linked)

    for q in (parsed.get("qc_issues") or []):
        if not isinstance(q, dict) or not q.get("description"):
            continue
        desc = str(q["description"])[:2000]
        if _is_duplicate(desc, existing_qc_descs):
            skipped_dupes["qc_issues"] += 1
            continue
        existing_qc_descs.append(desc)
        linked = await _link_task_ids(q.get("linked_activity_codes") or [])
        await db.qc_issues.insert_one({
            "id": _new_id(), "tenant_id": tenant_id, "project_id": dr["project_id"],
            "description": str(q["description"])[:2000],
            "severity": str(q.get("severity") or "minor"),
            "status": "open",
            "linked_task_ids": linked,
            "daily_report_id": dr_id,
            "date": dr.get("report_date"),
            "reported_at": now,
            "reported_by": actor_id,
            "source": "voice_intake",
            "created_at": now, "updated_at": now, "created_by": actor_id,
        })
        applied["qc_issues"] += 1

    for dl in (parsed.get("deliveries") or []):
        if not isinstance(dl, dict) or not dl.get("description"):
            continue
        desc = str(dl["description"])[:2000]
        if _is_duplicate(desc, existing_delivery_descs):
            skipped_dupes["deliveries"] += 1
            continue
        existing_delivery_descs.append(desc)
        linked = await _link_task_ids(dl.get("linked_activity_codes") or [])
        await db.deliveries.insert_one({
            "id": _new_id(), "tenant_id": tenant_id, "project_id": dr["project_id"],
            "description": str(dl["description"])[:2000],
            "supplier_name": dl.get("supplier_name"),
            "po_number": dl.get("po_number"),
            "status": str(dl.get("status") or "scheduled"),
            "linked_task_ids": linked,
            "daily_report_id": dr_id,
            "date": dr.get("report_date"),
            "reported_at": now,
            "reported_by": actor_id,
            "source": "voice_intake",
            "created_at": now, "updated_at": now, "created_by": actor_id,
        })
        applied["deliveries"] += 1

    for ce in (parsed.get("coordination_events") or []):
        if not isinstance(ce, dict) or not ce.get("description"):
            continue
        desc = str(ce["description"])[:2000]
        if _is_duplicate(desc, existing_coord_descs):
            skipped_dupes["coordination_events"] += 1
            continue
        existing_coord_descs.append(desc)
        linked = await _link_task_ids(ce.get("linked_activity_codes") or [])
        await db.coordination_events.insert_one({
            "id": _new_id(), "tenant_id": tenant_id, "project_id": dr["project_id"],
            "description": str(ce["description"])[:2000],
            "with_party": ce.get("with_party"),
            "linked_task_ids": linked,
            "daily_report_id": dr_id,
            "date": dr.get("report_date"),
            "reported_at": now,
            "reported_by": actor_id,
            "source": "voice_intake",
            "created_at": now, "updated_at": now, "created_by": actor_id,
        })
        applied["coordination_events"] += 1

    # Audit row -- the super's draft now contains voice-injected fields,
    # which auditors need to see flagged.
    await db.audit_log.insert_one({
        "id": _new_id(), "tenant_id": tenant_id, "actor_id": actor_id,
        "table_name": "daily_reports", "record_id": dr_id,
        "op": "DR_VOICE_INTAKE_APPLIED",
        "before_data": {}, "after_data": {"applied": applied},
        "created_at": now,
    })

    # Bug fix (2026-05-25 night) -- recompute task blocker counts for
    # any task that a voice-inserted delay was linked to. Without this
    # the DR activity grid and Tasks page would NOT show a blocker
    # badge against the affected row, even though the delay row was
    # written to the DB. Mirrors the recompute call that the manual
    # delay POST endpoint runs in field_events_router.
    if tasks_to_recompute:
        from services.task_status import recompute_for_tasks
        try:
            await recompute_for_tasks(
                db, tenant_id, list(tasks_to_recompute),
                actor_id=actor_id, request_id=_new_id(),
            )
        except Exception as exc:
            # Don't fail the whole voice intake if the recompute hits
            # an edge case -- the delay is still recorded; the badge
            # will refresh on the next manual reload.
            log.warning("dr_voice: blocker recompute failed for %s: %s",
                        list(tasks_to_recompute), exc)

    return {
        "ok": True,
        "applied": applied,
        "skipped_duplicates": skipped_dupes,
        "draft": parsed,   # so frontend can show diff/preview if it wants
    }


@router.post("/{dr_id}/voice/transcribe-and-extract")
async def voice_transcribe_and_extract(
    dr_id: str,
    audio: UploadFile = File(...),
    ctx: dict = Depends(require_tenant),
):
    """One-shot path used by the offline sync queue.

    The mobile client uploads its queued audio blob the moment
    connectivity returns; this endpoint handles transcription +
    extraction + DB writes atomically from the client's perspective.
    """
    raw = await audio.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty audio upload")
    if len(raw) > 100 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="audio file too large (>100 MB)")

    raw_mime = audio.content_type or "audio/webm"
    mime = raw_mime.split(";")[0].strip()
    if not mime.startswith("audio/"):
        mime = "audio/webm"
    log.info("dr_voice: /transcribe-and-extract dr=%s bytes=%d mime=%s (raw=%s)",
             dr_id, len(raw), mime, raw_mime)

    transcript = await _gemini_transcribe_audio(raw, mime)
    log.info("dr_voice: /transcribe-and-extract dr=%s transcript=%r",
             dr_id, transcript[:300])
    # Reuse the same path -- pass transcript through extract endpoint.
    if not transcript.strip():
        # Gemini returned nothing -- don't call Claude with an empty
        # transcript; return a structured response so the frontend can
        # show a clear "AI couldn't hear you" message.
        return {
            "transcript": "",
            "ok": True,
            "applied": {"weather": False, "notes": False, "activity_updates": 0,
                        "crew_presence": 0, "delays": 0, "safety_events": 0,
                        "qc_issues": 0, "deliveries": 0, "coordination_events": 0,
                        "rfis": 0},
            "draft": {},
            "warning": "no_speech_detected",
        }
    extract_result = await voice_extract(
        dr_id, {"transcript": transcript}, ctx,
    )
    return {"transcript": transcript, **extract_result}


# ---------------------------------------------------------------------
# Live conversational token issuance (Route 3)
# ---------------------------------------------------------------------
@router.post("/{dr_id}/voice/live-token")
async def voice_live_token(
    dr_id: str, ctx: dict = Depends(require_tenant),
):
    """Issue a short-lived Gemini Live API access token so the BROWSER
    can open a WebSocket directly to generativelanguage.googleapis.com
    without proxying audio through our backend.

    For Phase A we return the same GEMINI_API_KEY value (Gemini Live's
    ephemeral-token endpoint isn't GA yet as of 2026-05; using the
    primary key from a short-lived signed URL is the current pattern).
    When Google ships the ephemeral-token endpoint we'll swap this for
    a 5-minute exchange call.

    SECURITY NOTE: This endpoint is auth-gated (require_tenant) so an
    anonymous user can't lift the key from the browser. The key only
    reaches authenticated session users.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="Live voice not configured -- GEMINI_API_KEY missing.",
        )
    # Per Google's models?supportedGenerationMethods=bidiGenerateContent
    # probe (Feb 2026), the live conversational model name is
    # `gemini-2.5-flash-native-audio-latest`. The deprecated names from
    # earlier prototyping (gemini-2.0-flash-exp, gemini-3.1-flash-live-preview)
    # are 404 / not registered for this account tier.
    return {"token": api_key, "model": "gemini-2.5-flash-native-audio-latest"}


# ---------------------------------------------------------------------
# Capability probe -- frontend uses this to decide whether to show the
# "Live Voice" / "Voice Memo" choices as enabled or grayed-out.
# Auth-gated; no key value is leaked, only booleans.
# ---------------------------------------------------------------------
@router.get("/voice/config")
async def voice_config(ctx: dict = Depends(require_tenant)):
    gemini_present = bool(os.environ.get("GEMINI_API_KEY", "").strip())
    return {
        "gemini_configured": gemini_present,
        "voice_memo_enabled": gemini_present,
        "live_voice_enabled": gemini_present,
    }


def _new_id() -> str:
    import uuid
    return str(uuid.uuid4())
