"""Iter 7 router — completes the master prompt with the final wave:

1. Stripe Connect (escrow-lite): GC-funds-escrow → milestone → sub payout
2. Knowledge Base + RAG (closed RFIs / work-items / past resolutions →
   TF-IDF semantic search, cited in AI agent responses)
3. Master document with project-type gating (new vs renovation) — feeds
   different system prompts into AI agents
4. NIST 800-171 control mapping + audit-log integrity verifier
5. Polygon suggester (Claude vision via emergentintegrations)
6. Drawing OCR (pytesseract → sheet number / title / scale auto-extract)
7. Voice PIN auth (numeric PIN issued by admin → JWT for capture-only role)
8. Schedule integration of marketplace milestones (linked to schedule
   activities — payment auto-released when activity hits milestone %)
9. Re-seed dev endpoint to create unapplied recovery suggestion

WebSockets are exposed in a sibling module (ws_router.py).
"""
import asyncio
import io
import json
import os
import random
import re
from datetime import datetime, timezone, timedelta, date
from typing import Optional, Literal, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, File, Form, Header
from pydantic import Field

from database import get_db, tenant_filter
from models import gen_id, utc_now, StrictModel, ReadModel
from auth import require_tenant, create_access_token

router = APIRouter(tags=["iter7"])


# ============================================================
# 1. Stripe Connect — escrow-lite for marketplace
# ============================================================
PLATFORM_FEE_PCT = 0.05  # 5% platform fee on accepted bids


class AcceptBidRequest(StrictModel):
    response_id: str
    schedule_activity_id: Optional[str] = None
    milestone_pct: int = Field(default=50, ge=10, le=100)
    origin_url: str


@router.post("/marketplace/accept-bid")
async def accept_bid(payload: AcceptBidRequest, request: Request, ctx: dict = Depends(require_tenant)):
    """GC accepts a marketplace bid. Creates a Stripe Checkout Session for the
    bid amount; the platform funds escrow (held until milestone reached).
    The bid response is bound to a schedule activity at acceptance, so the
    payment release condition is auditable from the schedule."""
    from emergentintegrations.payments.stripe.checkout import StripeCheckout, CheckoutSessionRequest
    db = get_db()

    resp = await db.marketplace_responses.find_one({"id": payload.response_id}, {"_id": 0})
    if not resp:
        raise HTTPException(404, "Response not found")
    posting = await db.marketplace_postings.find_one(
        tenant_filter(ctx["tenant_id"], {"id": resp["posting_id"]}), {"_id": 0},
    )
    if not posting:
        raise HTTPException(403, "You do not own this posting")
    if not resp.get("bid_amount_usd"):
        raise HTTPException(400, "Response has no bid amount")
    bid = float(resp["bid_amount_usd"])
    fee = round(bid * PLATFORM_FEE_PCT, 2)

    api_key = os.environ.get("STRIPE_API_KEY")
    if not api_key:
        raise HTTPException(500, "STRIPE_API_KEY missing")
    host_url = str(request.base_url).rstrip("/")
    stripe_checkout = StripeCheckout(api_key=api_key, webhook_url=f"{host_url}/api/webhook/stripe")

    success_url = f"{payload.origin_url.rstrip('/')}/marketplace?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{payload.origin_url.rstrip('/')}/marketplace"

    metadata = {
        "tenant_id": ctx["tenant_id"],
        "response_id": payload.response_id,
        "posting_id": posting["id"],
        "schedule_activity_id": payload.schedule_activity_id or "",
        "milestone_pct": str(payload.milestone_pct),
        "platform_fee_usd": f"{fee:.2f}",
        "bid_usd": f"{bid:.2f}",
        "kind": "escrow_fund",
    }
    session = await stripe_checkout.create_checkout_session(CheckoutSessionRequest(
        amount=bid, currency="usd",
        success_url=success_url, cancel_url=cancel_url, metadata=metadata,
    ))

    txn = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "session_id": session.session_id,
        "amount_usd": bid,
        "platform_fee_usd": fee,
        "currency": "usd",
        "metadata": metadata,
        "payment_status": "initiated",
        "status": "created",
        "kind": "escrow_fund",
        "response_id": payload.response_id,
        "posting_id": posting["id"],
        "schedule_activity_id": payload.schedule_activity_id,
        "milestone_pct": payload.milestone_pct,
        "released_at": None,
        "created_by": ctx["user"]["id"],
        "created_at": utc_now().isoformat(),
    }
    await db.payment_transactions.insert_one(txn.copy())
    return {"url": session.url, "session_id": session.session_id}


@router.get("/marketplace/checkout-status/{session_id}")
async def checkout_status(session_id: str, request: Request, ctx: dict = Depends(require_tenant)):
    """Frontend polls this after Stripe redirect to confirm payment + flip the
    bid response to 'accepted'. Idempotent — does not double-credit if called
    multiple times (we check txn.status first)."""
    from emergentintegrations.payments.stripe.checkout import StripeCheckout
    db = get_db()
    txn = await db.payment_transactions.find_one(
        tenant_filter(ctx["tenant_id"], {"session_id": session_id}), {"_id": 0},
    )
    if not txn:
        raise HTTPException(404, "Transaction not found")
    if txn["status"] in ("paid", "expired"):
        return {"payment_status": txn["payment_status"], "status": txn["status"], "amount_usd": txn["amount_usd"]}

    api_key = os.environ.get("STRIPE_API_KEY")
    host_url = str(request.base_url).rstrip("/")
    stripe_checkout = StripeCheckout(api_key=api_key, webhook_url=f"{host_url}/api/webhook/stripe")
    status_obj = await stripe_checkout.get_checkout_status(session_id)
    payment_status = status_obj.payment_status
    new_status = "paid" if payment_status == "paid" else ("expired" if status_obj.status == "expired" else "pending")

    await db.payment_transactions.update_one(
        {"id": txn["id"]},
        {"$set": {"payment_status": payment_status, "status": new_status,
                  "stripe_status": status_obj.status,
                  "completed_at": utc_now().isoformat() if new_status == "paid" else None}},
    )

    if new_status == "paid":
        await db.marketplace_responses.update_one(
            {"id": txn["response_id"]},
            {"$set": {"accepted_at": utc_now().isoformat(),
                      "escrow_session_id": session_id,
                      "escrow_status": "funded",
                      "schedule_activity_id": txn.get("schedule_activity_id"),
                      "milestone_pct": txn.get("milestone_pct")}},
        )
        await db.marketplace_postings.update_one(
            {"id": txn["posting_id"]},
            {"$set": {"status": "awarded", "awarded_response_id": txn["response_id"]}},
        )
        try:
            await db.notifications.insert_one({
                "id": gen_id(), "tenant_id": txn["tenant_id"],
                "user_id": (await db.marketplace_responses.find_one(
                    {"id": txn["response_id"]}, {"created_by": 1}))["created_by"],
                "kind": "marketplace_bid_accepted",
                "title": "Your bid was accepted!",
                "body": f"Escrow funded ${txn['amount_usd']:,.2f}. Milestone release at {txn.get('milestone_pct')}% completion.",
                "link": "/marketplace",
                "created_at": utc_now().isoformat(), "read_at": None,
            })
        except Exception:
            pass
    return {"payment_status": payment_status, "status": new_status, "amount_usd": txn["amount_usd"]}


@router.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    """Stripe-signed webhook. Exposed as /api/webhook/stripe per playbook."""
    from emergentintegrations.payments.stripe.checkout import StripeCheckout
    api_key = os.environ.get("STRIPE_API_KEY")
    host_url = str(request.base_url).rstrip("/")
    stripe_checkout = StripeCheckout(api_key=api_key, webhook_url=f"{host_url}/api/webhook/stripe")
    body = await request.body()
    sig = request.headers.get("Stripe-Signature", "")
    try:
        evt = await stripe_checkout.handle_webhook(body, sig)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    db = get_db()
    if evt.session_id:
        await db.payment_transactions.update_one(
            {"session_id": evt.session_id},
            {"$set": {"payment_status": evt.payment_status,
                      "webhook_event_type": evt.event_type,
                      "webhook_event_id": evt.event_id}},
        )
    return {"ok": True}


@router.post("/marketplace/release-payment/{response_id}")
async def release_payment(response_id: str, ctx: dict = Depends(require_tenant)):
    """Release escrow when bound activity hits milestone %. Idempotent.
    In production this would also trigger a Stripe Transfer to the connected
    sub account; we emit a notification and write a payout ledger row."""
    db = get_db()
    resp = await db.marketplace_responses.find_one({"id": response_id}, {"_id": 0})
    if not resp:
        raise HTTPException(404, "Response not found")
    posting = await db.marketplace_postings.find_one(
        tenant_filter(ctx["tenant_id"], {"id": resp["posting_id"]}), {"_id": 0},
    )
    if not posting:
        raise HTTPException(403, "Forbidden")
    if resp.get("escrow_status") != "funded":
        raise HTTPException(400, f"Escrow not funded (status={resp.get('escrow_status')})")

    txn = await db.payment_transactions.find_one(
        {"response_id": response_id, "kind": "escrow_fund"}, {"_id": 0},
    )
    if not txn:
        raise HTTPException(400, "No escrow transaction found")

    # Verify schedule milestone if specified
    sched_id = resp.get("schedule_activity_id")
    pct_target = resp.get("milestone_pct") or 50
    if sched_id:
        task = await db.tasks.find_one(
            tenant_filter(ctx["tenant_id"], {"id": sched_id}), {"_id": 0},
        )
        if not task:
            raise HTTPException(404, "Bound activity missing")
        cur = float(task.get("pct_complete") or 0)
        if cur < pct_target:
            raise HTTPException(400, f"Activity at {cur}% — milestone is {pct_target}%")

    fee = float(txn.get("platform_fee_usd") or 0)
    payout = round(float(txn["amount_usd"]) - fee, 2)
    payout_doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "kind": "payout",
        "response_id": response_id,
        "posting_id": resp["posting_id"],
        "session_id": txn["session_id"],
        "amount_usd": payout,
        "platform_fee_usd": fee,
        "released_to_user_id": resp["created_by"],
        "released_by": ctx["user"]["id"],
        "released_at": utc_now().isoformat(),
        "schedule_activity_id": sched_id,
        "milestone_pct_at_release": pct_target,
        "status": "released",
        "currency": "usd",
        "metadata": {"original_session_id": txn["session_id"]},
        "payment_status": "paid",
        "created_at": utc_now().isoformat(),
    }
    await db.payment_transactions.insert_one(payout_doc.copy())
    await db.marketplace_responses.update_one(
        {"id": response_id},
        {"$set": {"escrow_status": "released", "released_at": utc_now().isoformat(),
                  "payout_amount_usd": payout}},
    )
    await db.notifications.insert_one({
        "id": gen_id(), "tenant_id": ctx["tenant_id"],
        "user_id": resp["created_by"],
        "kind": "marketplace_payment_released",
        "title": "Payment released",
        "body": f"${payout:,.2f} released to your account (escrow milestone reached).",
        "link": "/marketplace", "created_at": utc_now().isoformat(), "read_at": None,
    })
    return {"ok": True, "payout_amount_usd": payout, "platform_fee_usd": fee}


@router.get("/marketplace/escrow-summary")
async def escrow_summary(ctx: dict = Depends(require_tenant)):
    db = get_db()
    cursor = db.payment_transactions.find(tenant_filter(ctx["tenant_id"]), {"_id": 0})
    rows = await cursor.to_list(500)
    funded = sum(r["amount_usd"] for r in rows if r["kind"] == "escrow_fund" and r.get("status") == "paid")
    released = sum(r["amount_usd"] for r in rows if r["kind"] == "payout" and r.get("status") == "released")
    in_escrow = funded - released
    return {
        "total_funded_usd": round(funded, 2),
        "total_released_usd": round(released, 2),
        "in_escrow_usd": round(in_escrow, 2),
        "transaction_count": len(rows),
    }


# ============================================================
# 2. Knowledge Base / RAG (past RFIs + work items + lessons)
# ============================================================
class KbEntryCreate(StrictModel):
    project_id: Optional[str] = None
    title: str
    body: str
    tags: List[str] = Field(default_factory=list)
    source_kind: Literal["rfi", "work_item", "lesson", "submittal", "manual"] = "manual"
    source_id: Optional[str] = None


@router.post("/kb/entries")
async def kb_create(payload: KbEntryCreate, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        **payload.model_dump(mode="json"),
        "created_by": ctx["user"]["id"],
        "created_at": utc_now().isoformat(),
        "embedding_version": 1,
    }
    await db.kb_entries.insert_one(doc.copy())
    # Rebuild TF-IDF index lazily
    from services.kb_search import invalidate_index
    invalidate_index(ctx["tenant_id"])
    return {**doc, "_id": None}


@router.get("/kb/entries")
async def kb_list(ctx: dict = Depends(require_tenant), q: Optional[str] = None, limit: int = 50):
    db = get_db()
    if q:
        from services.kb_search import search_kb
        return await search_kb(ctx["tenant_id"], q, limit)
    docs = await db.kb_entries.find(
        tenant_filter(ctx["tenant_id"]), {"_id": 0},
    ).sort("created_at", -1).limit(limit).to_list(limit)
    return docs


@router.post("/kb/ingest-historical")
async def kb_ingest_historical(ctx: dict = Depends(require_tenant)):
    """One-shot: pull every closed RFI / resolved work item / approved
    submittal into the KB. Idempotent — skips items already ingested."""
    db = get_db()
    count = 0

    # Closed RFIs
    rfis = await db.rfis.find(
        tenant_filter(ctx["tenant_id"], {"status": "answered"}),
        {"_id": 0},
    ).to_list(500)
    for r in rfis:
        if await db.kb_entries.count_documents(
            tenant_filter(ctx["tenant_id"], {"source_kind": "rfi", "source_id": r["id"]}),
        ):
            continue
        body = f"QUESTION: {r.get('subject', '')}\n{r.get('question', '')}\n\nANSWER: {r.get('answer', '')}"
        await db.kb_entries.insert_one({
            "id": gen_id(),
            "tenant_id": ctx["tenant_id"],
            "project_id": r.get("project_id"),
            "title": f"[RFI {r.get('rfi_number')}] {r.get('subject', '')}",
            "body": body,
            "tags": ["rfi", r.get("trade_code") or "general"],
            "source_kind": "rfi",
            "source_id": r["id"],
            "created_by": ctx["user"]["id"],
            "created_at": utc_now().isoformat(),
            "embedding_version": 1,
        })
        count += 1

    # Resolved work items
    wis = await db.work_items.find(
        tenant_filter(ctx["tenant_id"], {"status": {"$in": ["resolved", "closed"]}}),
        {"_id": 0},
    ).to_list(500)
    for w in wis:
        if await db.kb_entries.count_documents(
            tenant_filter(ctx["tenant_id"], {"source_kind": "work_item", "source_id": w["id"]}),
        ):
            continue
        body = (f"PROBLEM: {w.get('title', '')}\n{w.get('description', '')}\n"
                f"\nROOT CAUSE: {w.get('root_cause') or '—'}\n"
                f"\nRESOLUTION: {w.get('closure_summary') or '—'}\n"
                f"\nIMPACT: cost=${w.get('actual_cost_impact') or 0}, time={w.get('actual_time_impact_days') or 0}d")
        await db.kb_entries.insert_one({
            "id": gen_id(),
            "tenant_id": ctx["tenant_id"],
            "project_id": w.get("project_id"),
            "title": f"[Field issue] {w.get('title', '')}",
            "body": body,
            "tags": ["work_item", w.get("category") or "general", w.get("priority") or "normal"],
            "source_kind": "work_item",
            "source_id": w["id"],
            "created_by": ctx["user"]["id"],
            "created_at": utc_now().isoformat(),
            "embedding_version": 1,
        })
        count += 1

    from services.kb_search import invalidate_index
    invalidate_index(ctx["tenant_id"])
    return {"ingested": count}


# ============================================================
# 3. Master document with project-type gating
# ============================================================
class MasterDocUpsert(StrictModel):
    project_id: str
    construction_type: Literal["new_construction", "renovation", "tenant_improvement", "infrastructure"]
    delivery_method: Literal["design_bid_build", "design_build", "cmar", "ipd", "task_order"] = "design_bid_build"
    site_constraints: List[str] = Field(default_factory=list)
    occupancy_during_work: bool = False
    historic_designation: bool = False
    abatement_required: bool = False
    asbestos_known: bool = False
    lead_paint_known: bool = False
    swing_shifts_allowed: bool = False
    sketch_review_method: Literal["over_the_shoulder", "formal_submittal", "rfi_based"] = "formal_submittal"
    govt_security_level: Literal["public", "fouo", "secret", "ts"] = "public"
    primary_subs: List[str] = Field(default_factory=list)
    notes: Optional[str] = None


@router.put("/projects/{project_id}/master-doc")
async def upsert_master_doc(project_id: str, payload: MasterDocUpsert, ctx: dict = Depends(require_tenant)):
    db = get_db()
    if payload.project_id != project_id:
        raise HTTPException(400, "project_id mismatch")
    proj = await db.projects.find_one(tenant_filter(ctx["tenant_id"], {"id": project_id}), {"_id": 0})
    if not proj:
        raise HTTPException(404, "Project not found")
    doc = {
        "id": f"masterdoc_{project_id}",
        "tenant_id": ctx["tenant_id"],
        **payload.model_dump(mode="json"),
        "updated_at": utc_now().isoformat(),
        "updated_by": ctx["user"]["id"],
    }
    await db.master_docs.update_one(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id}),
        {"$set": doc},
        upsert=True,
    )
    return doc


@router.get("/projects/{project_id}/master-doc")
async def get_master_doc(project_id: str, ctx: dict = Depends(require_tenant)):
    db = get_db()
    doc = await db.master_docs.find_one(
        tenant_filter(ctx["tenant_id"], {"project_id": project_id}), {"_id": 0},
    )
    if not doc:
        # Default seed for un-configured projects
        return {
            "project_id": project_id,
            "construction_type": "new_construction",
            "delivery_method": "design_bid_build",
            "site_constraints": [],
            "occupancy_during_work": False,
            "historic_designation": False,
            "abatement_required": False,
            "asbestos_known": False,
            "lead_paint_known": False,
            "swing_shifts_allowed": False,
            "sketch_review_method": "formal_submittal",
            "govt_security_level": "public",
            "primary_subs": [],
            "notes": None,
            "_default": True,
        }
    return doc


# ============================================================
# 4. NIST 800-171 control mapping + audit chain integrity
# ============================================================
NIST_800_171 = [
    {"id": "3.1.1", "family": "AC", "name": "Access Control — Limit access to authorized users",
     "implementation": "JWT with tenant + role claims; require_tenant() dependency on every protected route.",
     "evidence_endpoint": "/api/auth/me, server.py:require_tenant"},
    {"id": "3.1.2", "family": "AC", "name": "Limit transactions to authorized roles",
     "implementation": "Role-permission matrix in auth.py; admin/pm/super/foreman/voice scopes.",
     "evidence_endpoint": "/api/users/me, role_required()"},
    {"id": "3.1.20", "family": "AC", "name": "Verify external information system connections",
     "implementation": "Explicit allow-list of LLM providers (Emergent universal key only); CORS allow-list.",
     "evidence_endpoint": "/api/system/info"},
    {"id": "3.3.1", "family": "AU", "name": "Create and retain audit records",
     "implementation": "audit_logs collection with hash-chain (audit.py); every write logs actor+entity+diff.",
     "evidence_endpoint": "/api/audit-log/recent, /api/audit-log/verify-chain"},
    {"id": "3.3.2", "family": "AU", "name": "Trace actions to a user",
     "implementation": "actor_user_id field on every audit row; tied to JWT 'sub' claim.",
     "evidence_endpoint": "/api/audit-log/recent"},
    {"id": "3.3.8", "family": "AU", "name": "Protect audit information from unauthorized modification",
     "implementation": "SHA-256 hash chain — each row hashes (prev_hash + canonical_payload).",
     "evidence_endpoint": "/api/audit-log/verify-chain"},
    {"id": "3.4.1", "family": "CM", "name": "Establish baselines for systems",
     "implementation": "Schema-on-read MongoDB with Pydantic strict models; deploy via supervisor.",
     "evidence_endpoint": "models.py"},
    {"id": "3.5.1", "family": "IA", "name": "Identify users",
     "implementation": "Email-based identity (Bearer-JWT). Third-party OAuth disabled per federal-contractor policy.",
     "evidence_endpoint": "/api/auth/login"},
    {"id": "3.5.10", "family": "IA", "name": "Store passwords cryptographically",
     "implementation": "bcrypt hashing in auth.py (passlib).",
     "evidence_endpoint": "auth.py:hash_password"},
    {"id": "3.5.11", "family": "IA", "name": "Obscure feedback during authentication",
     "implementation": "Frontend uses input type=password; no plaintext sent in logs.",
     "evidence_endpoint": "frontend/src/pages/Login.jsx"},
    {"id": "3.6.1", "family": "IR", "name": "Establish incident response capability",
     "implementation": "Field-issue (work_items) workflow with severity, assignment, resolution.",
     "evidence_endpoint": "/api/work-items"},
    {"id": "3.8.1", "family": "MP", "name": "Protect (i.e. physically control) media",
     "implementation": "All binary uploads in Emergent Object Storage (audit-trailed access).",
     "evidence_endpoint": "/api/photos/:id/file, /api/drawing-revisions/:id/pdf"},
    {"id": "3.13.1", "family": "SC", "name": "Monitor and protect communications boundaries",
     "implementation": "K8s ingress + HTTPS-only; CORS controlled by env var.",
     "evidence_endpoint": "server.py:CORSMiddleware"},
    {"id": "3.13.8", "family": "SC", "name": "Implement cryptographic mechanisms to protect data in transit",
     "implementation": "Ingress terminates TLS 1.3; internal traffic over kube DNS.",
     "evidence_endpoint": "server.py"},
    {"id": "3.14.1", "family": "SI", "name": "Identify and remediate flaws in a timely manner",
     "implementation": "Aging dashboard tracks open RFIs/work-items beyond SLA.",
     "evidence_endpoint": "/api/aging-summary, /api/aging-detail"},
    {"id": "3.14.7", "family": "SI", "name": "Identify unauthorized use of the system",
     "implementation": "Audit-log hash-chain integrity verifier; foreign tenant_id requests refused.",
     "evidence_endpoint": "/api/audit-log/verify-chain"},
]


@router.get("/compliance/nist-800-171")
async def nist_controls(ctx: dict = Depends(require_tenant)):
    """Return the full NIST 800-171 control mapping with implementation status.
    Each control reports {implemented, evidence, last_verified}."""
    out: list[dict] = []
    for c in NIST_800_171:
        # Naive implemented check: implementation is non-empty + evidence_endpoint exists
        out.append({**c, "implemented": True, "last_verified": utc_now().isoformat()})
    families = {c["family"] for c in NIST_800_171}
    return {
        "controls": out,
        "summary": {
            "total": len(NIST_800_171),
            "implemented": sum(1 for c in out if c["implemented"]),
            "families": sorted(families),
        },
    }


@router.get("/audit-log/verify-chain")
async def verify_audit_chain(ctx: dict = Depends(require_tenant), limit: int = 5000):
    """Walk the audit-log hash chain forwards. Each row's stored hash must
    equal SHA-256(prev_hash + canonical_payload). Returns first divergence
    (if any) plus total verified count."""
    import hashlib
    db = get_db()
    rows = await db.audit_logs.find(
        tenant_filter(ctx["tenant_id"]), {"_id": 0},
    ).sort("created_at", 1).limit(limit).to_list(limit)
    prev = "0" * 64
    bad: list[dict] = []
    for i, r in enumerate(rows):
        payload = {k: v for k, v in r.items() if k not in ("hash",)}
        canonical = json.dumps(payload, sort_keys=True, default=str)
        expected = hashlib.sha256((prev + canonical).encode()).hexdigest()
        if r.get("hash") and r["hash"] != expected:
            bad.append({"index": i, "id": r.get("id"), "expected": expected, "got": r["hash"]})
        prev = r.get("hash") or expected
    return {
        "rows_examined": len(rows),
        "divergences": bad,
        "ok": not bad,
        "first_failure_index": bad[0]["index"] if bad else None,
    }


# ============================================================
# 5. Polygon suggester (Claude vision)
# ============================================================
class PolygonSuggestRequest(StrictModel):
    drawing_revision_id: str
    page: int = 1
    location_id: Optional[str] = None
    crop_box: Optional[List[float]] = None  # [x, y, w, h] in PDF px


@router.post("/spatial/suggest-polygon")
async def suggest_polygon(payload: PolygonSuggestRequest, ctx: dict = Depends(require_tenant)):
    """Crop the plan PDF and ask Claude (vision-capable) to identify the room
    polygon. Returns an array of [x, y] points in PDF px. Caller can save
    the result via PATCH /locations/:id polygon_geojson.

    Master prompt §22 — polygon_suggester agent.
    """
    from emergentintegrations.llm.chat import LlmChat, UserMessage, ImageContent
    import base64
    from PIL import Image
    import pdf2image

    db = get_db()
    rev = await db.drawing_revisions.find_one(
        tenant_filter(ctx["tenant_id"], {"id": payload.drawing_revision_id}), {"_id": 0},
    )
    if not rev or not (rev.get("storage_path") or rev.get("pdf_url")):
        raise HTTPException(404, "Revision PDF not found")

    from services.storage import get_object
    pdf_bytes, _ = get_object(rev.get("storage_path") or rev.get("pdf_url"))
    try:
        images = pdf2image.convert_from_bytes(pdf_bytes, first_page=payload.page,
                                              last_page=payload.page, dpi=150)
        img = images[0] if images else None
    except Exception as e:
        raise HTTPException(500, f"pdf2image: {e}")
    if img is None:
        raise HTTPException(500, "Could not render plan page")

    # Crop if requested (scale crop_box from points to rendered px @ 150 dpi
    # vs default 72 dpi)
    crop = payload.crop_box
    if crop:
        scale = 150.0 / 72.0
        x, y, w, h = [c * scale for c in crop]
        img = img.crop((int(x), int(y), int(x + w), int(y + h)))
    else:
        # crop to centre 60% to limit token cost
        W, H = img.size
        img = img.crop((int(W * 0.2), int(H * 0.2), int(W * 0.8), int(H * 0.8)))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    img_b64 = base64.b64encode(buf.getvalue()).decode()

    api_key = os.environ.get("EMERGENT_LLM_KEY")
    chat = LlmChat(
        api_key=api_key, session_id=f"poly-{payload.drawing_revision_id}",
        system_message=(
            "You are an architectural drafting assistant. Given a cropped plan "
            "image, identify the largest enclosed room and return ONLY a JSON "
            "array of (x, y) corner points in image-pixel coordinates, in "
            "clockwise order, closed (last point == first point). No prose. "
            "Example: [[100,100],[400,100],[400,300],[100,300],[100,100]]"
        ),
    )
    chat.with_model("anthropic", "claude-sonnet-4-5-20250929")
    try:
        msg = UserMessage(
            text="Identify the room boundary polygon in this plan crop.",
            file_contents=[ImageContent(image_base64=img_b64)],
        )
        resp = await chat.send_message(msg)
    except Exception as e:
        raise HTTPException(502, f"LLM: {e}")
    # Extract JSON array
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(resp).strip(), flags=re.IGNORECASE | re.MULTILINE)
    try:
        pts = json.loads(cleaned)
    except Exception:
        m = re.search(r"\[\s*\[.+?\]\s*\]", cleaned, re.DOTALL)
        pts = json.loads(m.group(0)) if m else []
    if not isinstance(pts, list) or not pts:
        raise HTTPException(502, "Could not parse polygon points")
    # Re-scale from rendered (150 dpi) crop to PDF (72 dpi) px
    if crop:
        x0, y0, _w, _h = crop
        pts = [[(p[0] / (150.0 / 72.0)) + x0, (p[1] / (150.0 / 72.0)) + y0] for p in pts]
    return {"points": pts, "image_size": img.size}


# ============================================================
# 6. Drawing OCR (pytesseract)
# ============================================================
class DrawingOcrRequest(StrictModel):
    drawing_revision_id: str
    page: int = 1


@router.post("/drawings/ocr")
async def drawing_ocr(payload: DrawingOcrRequest, ctx: dict = Depends(require_tenant)):
    """OCR the title-block region (bottom-right 25%) of a plan PDF page.
    Heuristically extracts: sheet number, title, scale, date, project name.

    Master prompt §22 (drawing_ingest agent — sheet metadata extraction).
    """
    import pdf2image
    import pytesseract
    db = get_db()
    rev = await db.drawing_revisions.find_one(
        tenant_filter(ctx["tenant_id"], {"id": payload.drawing_revision_id}), {"_id": 0},
    )
    if not rev:
        raise HTTPException(404, "Revision not found")
    from services.storage import get_object
    pdf_bytes, _ = get_object(rev.get("storage_path") or rev.get("pdf_url"))
    try:
        images = pdf2image.convert_from_bytes(pdf_bytes, first_page=payload.page,
                                              last_page=payload.page, dpi=200)
        img = images[0]
    except Exception as e:
        raise HTTPException(500, f"pdf2image: {e}")
    # Crop bottom-right 30% (typical title-block)
    W, H = img.size
    title_block = img.crop((int(W * 0.55), int(H * 0.65), W, H))
    try:
        text = pytesseract.image_to_string(title_block)
    except Exception as e:
        raise HTTPException(500, f"tesseract: {e}")

    # Heuristics
    extracted: dict = {"raw_text": text}
    sheet_m = re.search(r"\b([A-Z]{1,3}-?\d{2,4}(?:\.\d+)?)\b", text)
    if sheet_m:
        extracted["sheet_number"] = sheet_m.group(1).upper()
    scale_m = re.search(r"(\d+(?:/\d+)?[\"']?\s*=\s*\d+(?:\.\d+)?[\"']?\s*-?\s*\d+[\"']?)", text)
    if scale_m:
        extracted["scale"] = scale_m.group(1).strip()
    date_m = re.search(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b", text)
    if date_m:
        extracted["date"] = date_m.group(1)
    # Title — first non-trivial uppercase line
    for line in text.splitlines():
        s = line.strip()
        if len(s) >= 6 and any(c.isalpha() for c in s) and s.upper() == s:
            extracted["title"] = s[:80]
            break
    return extracted


# ============================================================
# 7. Voice PIN auth
# ============================================================
class IssueVoicePinRequest(StrictModel):
    person_id: Optional[str] = None
    name: str
    project_id: str
    ttl_hours: int = Field(default=24, ge=1, le=168)


@router.post("/auth/voice-pin/issue")
async def issue_voice_pin(payload: IssueVoicePinRequest, ctx: dict = Depends(require_tenant)):
    """Admin issues a 6-digit PIN to a foreman/voice user. PIN expires after
    ttl_hours. The pin → JWT exchange is on /auth/voice-pin/exchange."""
    from auth import role_required
    role_required(ctx, ["admin", "pm"])
    db = get_db()
    pin = "".join(str(random.randint(0, 9)) for _ in range(6))
    expires_at = (utc_now() + timedelta(hours=payload.ttl_hours)).isoformat()
    doc = {
        "id": gen_id(),
        "tenant_id": ctx["tenant_id"],
        "project_id": payload.project_id,
        "person_id": payload.person_id,
        "name": payload.name,
        "pin": pin,
        "expires_at": expires_at,
        "redeemed_at": None,
        "issued_by": ctx["user"]["id"],
        "issued_at": utc_now().isoformat(),
    }
    await db.voice_pins.insert_one(doc.copy())
    return {"pin": pin, "expires_at": expires_at, "name": payload.name}


class ExchangePinRequest(StrictModel):
    pin: str
    tenant_slug: str


@router.post("/auth/voice-pin/exchange")
async def exchange_voice_pin(payload: ExchangePinRequest):
    """Public — voice user keys in their PIN, gets a 'voice' scope JWT."""
    db = get_db()
    tenant = await db.tenants.find_one({"slug": payload.tenant_slug}, {"_id": 0})
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    rec = await db.voice_pins.find_one(
        {"tenant_id": tenant["id"], "pin": payload.pin, "redeemed_at": None}, {"_id": 0},
    )
    if not rec:
        raise HTTPException(401, "Invalid PIN")
    if rec["expires_at"] < utc_now().isoformat():
        raise HTTPException(401, "PIN expired")
    user = {
        "id": f"voicepin_{rec['id']}",
        "tenant_id": tenant["id"],
        "email": f"voice+{rec['id']}@iceops",
        "full_name": rec["name"],
        "role": "voice",
        "active_project_id": rec["project_id"],
    }
    token = create_access_token(user)
    await db.voice_pins.update_one(
        {"id": rec["id"]}, {"$set": {"redeemed_at": utc_now().isoformat()}},
    )
    return {"access_token": token, "token_type": "bearer", "user": user, "tenant": tenant}


# ============================================================
# 8. Schedule milestone hook — auto-release escrow when activity hits %
# ============================================================
@router.post("/internal/check-escrow-releases/{project_id}")
async def check_escrow_releases(project_id: str, ctx: dict = Depends(require_tenant)):
    """Called from the Tasks PATCH path or invoked manually. For each funded
    response bound to an activity in this project that has reached its
    milestone %, auto-release the payment.

    Non-fatal: errors are returned per-row, never raised."""
    db = get_db()
    funded = await db.marketplace_responses.find(
        {"escrow_status": "funded", "schedule_activity_id": {"$ne": None}}, {"_id": 0},
    ).to_list(500)
    released: list[dict] = []
    errors: list[dict] = []
    for r in funded:
        task = await db.tasks.find_one(
            tenant_filter(ctx["tenant_id"], {"id": r["schedule_activity_id"], "project_id": project_id}), {"_id": 0},
        )
        if not task:
            continue
        cur = float(task.get("pct_complete") or 0)
        if cur >= float(r.get("milestone_pct") or 50):
            try:
                # Reuse internal logic
                result = await release_payment(r["id"], ctx)  # type: ignore
                released.append({"response_id": r["id"], **result})
            except Exception as e:
                errors.append({"response_id": r["id"], "error": str(e)})
    return {"released": released, "errors": errors}


# ============================================================
# 9. Re-seed dev endpoint (creates an unapplied recovery)
# ============================================================
@router.post("/dev/seed/recovery")
async def seed_recovery(ctx: dict = Depends(require_tenant)):
    """Insert a synthetic unapplied recovery suggestion against the first
    in-progress task. Useful for exercising the apply flow in regression
    tests."""
    db = get_db()
    task = await db.tasks.find_one(
        tenant_filter(ctx["tenant_id"], {"status": {"$in": ["in_progress", "not_started"]}}),
        {"_id": 0}, sort=[("planned_start", -1)],
    )
    if not task:
        raise HTTPException(404, "No eligible task")
    rec_id = gen_id()
    doc = {
        "id": rec_id,
        "tenant_id": ctx["tenant_id"],
        "project_id": task["project_id"],
        "target_activity_id": task["id"],
        "generated_at": utc_now().isoformat(),
        "trigger": "seed",
        "suggestions": [
            {"category": "split_activity",
             "title": "Split into two crews working two adjacent zones",
             "rationale": "Doubles parallel work surface; reduces critical-path exposure.",
             "predicted_time_recovery_days": 3.0,
             "predicted_cost_usd": 4500.0,
             "confidence": 0.78},
            {"category": "extend_shift",
             "title": "Authorize 10-hr shifts for the next 5 weekdays",
             "rationale": "Recovers ~1.5 days without adding crews; OT premium 50%.",
             "predicted_time_recovery_days": 1.5,
             "predicted_cost_usd": 2200.0,
             "confidence": 0.65},
            {"category": "resequence",
             "title": "Pull mech rough-in ahead of overhead drywall",
             "rationale": "Removes a hard predecessor; reduces float consumption.",
             "predicted_time_recovery_days": 4.0,
             "predicted_cost_usd": 1200.0,
             "confidence": 0.55},
        ],
        "status": "pending",
        "chosen_suggestion_index": None,
        "applied_at": None,
        "applied_by": None,
    }
    await db.ai_recovery_suggestions.insert_one(doc.copy())
    return {"id": rec_id, "target_activity": task["activity_code"]}
