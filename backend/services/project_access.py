"""Per-project access (2026-11-16).

The owner's rules, in short:
  * An admin is either an "all projects" admin (the default; sees every
    project, including future ones) or limited to specific projects.
  * Everyone else sees and changes only the projects they are assigned to
    (any project_members row for that user and project, whatever its
    role_on_project; role_on_project still decides what they may write where
    a route already checks it).
  * Company-wide controls (team, company settings, platform settings, the
    company-wide audit log) stay with all-projects admins.

Rollout switch: FF_PROJECT_ACCESS (Settings page), default OFF.
  OFF = watch only. Nothing is blocked, but every request that WOULD have
        been refused is counted in the project_access_watch collection, so an
        admin can fix assignments before switching it on.
  ON  = enforced. Reads of a project the caller is not assigned to answer
        404 "Project not found"; writes answer 403 "You are not assigned to
        this project". Phones keep a queued save that gets a 403 and retry it
        later (frontend/src/lib/drOfflineSync.js), so a supervisor's offline
        work is never thrown away while an admin sorts out the assignment.
        Never answer 404 to a write here: the phone would DROP the save.

What lives here, for the routers to call:
  is_full_admin(ctx)                      -> bool
  require_full_admin(ctx)                 -> raises 403 unless all-projects admin
  is_enforced()                           -> bool (FF_PROJECT_ACCESS)
  allowed_project_ids(ctx)                -> None (all) or frozenset of ids
  check_project_access(ctx, pid, write=)  -> None or raises 404/403
  resolve_project_id(ctx, kind, id)       -> project id of a child record
  check_record_access(ctx, kind, id, write=) -> resolve + check in one call
  project_scope_filter(ctx, query)        -> query limited to allowed projects
  enforce_project_scope                   -> route-level dependency (server.py)

The always-on checks that already existed in a few routers (project members,
resource leveling, photo tray, field suggestions, submittal timing, recovery
reports, exhibit exports) keep working whatever the switch says.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Iterable, Optional

from fastapi import HTTPException
from starlette.requests import HTTPConnection

from database import get_db

log = logging.getLogger("project_access")

FLAG = "FF_PROJECT_ACCESS"
WATCH_COLLECTION = "project_access_watch"

NOT_ASSIGNED = "You are not assigned to this project"
NOT_FOUND = "Project not found"
FULL_ADMIN_ONLY = "Only an admin with access to all projects can do this."
LAST_FULL_ADMIN = "The company must keep at least one admin with access to all projects"

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


# ---------------------------------------------------------------------------
# Who the caller is
# ---------------------------------------------------------------------------
def _role(ctx: dict) -> str:
    return (((ctx or {}).get("user") or {}).get("role") or (ctx or {}).get("role") or "").strip().lower()


def _scope(ctx: dict) -> str:
    """The caller's scope. A context built before this feature (tests, the
    worker, older helper code) has no access_scope: the role default applies,
    so an admin stays all-projects."""
    from auth import effective_access_scope
    stored = (ctx or {}).get("access_scope") or ((ctx or {}).get("user") or {}).get("access_scope")
    return effective_access_scope(_role(ctx), stored)


def is_full_admin(ctx: dict) -> bool:
    """True for an admin with access to all projects."""
    return _role(ctx) == "admin" and _scope(ctx) == "all"


def require_full_admin(ctx: dict, detail: str = FULL_ADMIN_ONLY) -> None:
    """Refuse (403) unless the caller is an all-projects admin. For company-
    wide controls: team, company settings, platform settings, the company-
    wide audit log, the nightly recompute trigger."""
    if not is_full_admin(ctx):
        raise HTTPException(403, detail)


def is_limited_admin(ctx: Optional[dict]) -> bool:
    """An admin limited to specific projects. This kind of admin did not
    exist before per-project access, so their limit is always enforced,
    switch or no switch: it blocks no existing user."""
    return bool(ctx) and _role(ctx) == "admin" and _scope(ctx) == "assigned"


def is_enforced(ctx: Optional[dict] = None) -> bool:
    """FF_PROJECT_ACCESS. Off (the default) = watch only, except for an
    admin limited to specific projects (pass ctx), who is always enforced."""
    from services import feature_flags
    return feature_flags.project_access_enabled() or is_limited_admin(ctx)


def role_on_project_for(role: Optional[str]) -> str:
    """The project_members.role_on_project to give someone with this company
    role when they are assigned to a project. Same mapping as migration 009,
    plus admin -> pm (a project-limited admin manages their projects) and the
    two specialty roles keeping their own names."""
    r = (role or "").strip().lower()
    return {
        "admin": "pm", "pm": "pm", "px": "pm",
        "super": "super", "pe": "super",
        "foreman": "foreman", "viewer": "viewer",
        "submittal_tech": "submittal_tech",
        "scheduling_engineer": "scheduling_engineer",
    }.get(r, "viewer")


# ---------------------------------------------------------------------------
# Which projects the caller may use
# ---------------------------------------------------------------------------
async def allowed_project_ids(ctx: dict) -> Optional[frozenset]:
    """None = every project in the company (all-projects admin). Otherwise
    the set of project ids the caller has ANY project_members row for.
    Worked out once per request and kept on the context."""
    if is_full_admin(ctx):
        return None
    if "_pa_allowed" in ctx:
        return ctx["_pa_allowed"]
    user_id = ((ctx.get("user") or {}).get("id"))
    tenant_id = ctx.get("tenant_id")
    if not user_id or not tenant_id:
        ctx["_pa_allowed"] = frozenset()
        return ctx["_pa_allowed"]
    try:
        rows = await get_db().project_members.find(
            {"tenant_id": tenant_id, "user_id": user_id},
            {"_id": 0, "project_id": 1},
        ).to_list(length=None)
        ids = frozenset(r.get("project_id") for r in rows if r.get("project_id"))
    except Exception as e:
        # A database hiccup must not turn into a wrongful refusal of a save.
        log.warning("project access: could not read assignments (%s); treating as watch only", e)
        ctx["_pa_unknown"] = True
        ids = frozenset()
    ctx["_pa_allowed"] = ids
    return ids


async def _record_watch(ctx: dict, project_id: str, *, write: bool, route: str,
                        blocked: bool) -> None:
    """Count one would-be (or actual) refusal. One row per (company, user,
    project, route, method), upserted. Never raises.

    The phone's background offline warm-up (frontend lib/drPrefetch.js)
    reads every project in the list, and while the switch is off that list
    is every project in the company. Those reads are marked with the
    X-ICE-Prefetch header. They are counted apart (prefetch_count, not
    count), so the report puts what people actually opened and saved first.
    They are still recorded: any client can send that header, so it must
    not be a way to leave no trace."""
    warmup = (not write) and bool(ctx.get("_pa_prefetch"))
    try:
        method = (ctx.get("_pa_method") or ("POST" if write else "GET")).upper()
        now = datetime.now(timezone.utc).isoformat()
        user = ctx.get("user") or {}
        inc = {"prefetch_count": 1} if warmup else {"count": 1}
        if blocked:
            inc["blocked_count"] = 1
        await get_db()[WATCH_COLLECTION].update_one(
            {"tenant_id": ctx.get("tenant_id"), "user_id": user.get("id"),
             "project_id": project_id, "route": route or ctx.get("_pa_route") or "",
             "method": method},
            {"$inc": inc,
             "$set": {"last_at": now, "role": _role(ctx), "write": bool(write),
                      "user_email": user.get("email")},
             "$setOnInsert": {"first_at": now}},
            upsert=True,
        )
    except Exception as e:
        log.warning("project access watch could not record (%s)", e)


async def check_project_access(ctx: dict, project_id, *, write: bool,
                               route: str = "") -> None:
    """Allow, or refuse, the caller's use of one project.

    * all-projects admin, or assigned to the project: returns.
    * the project is not in the caller's company (or does not exist):
      returns, so the caller keeps its own behavior (usually 404).
    * not assigned, switch OFF: records it in project_access_watch, returns.
    * not assigned, switch ON: records it, then 404 "Project not found" for a
      read, 403 "You are not assigned to this project" for a write.
    """
    if not isinstance(project_id, str) or not project_id.strip() or not ctx:
        return
    allowed = await allowed_project_ids(ctx)
    if allowed is None or project_id in allowed:
        return
    memo = ctx.setdefault("_pa_seen", {})
    key = (project_id, bool(write))
    if key not in memo:
        try:
            exists = await get_db().projects.find_one(
                {"tenant_id": ctx.get("tenant_id"), "id": project_id},
                {"_id": 0, "id": 1},
            )
        except Exception:
            exists = None
        if not exists:
            memo[key] = "unknown"
        else:
            blocked = is_enforced(ctx) and not ctx.get("_pa_unknown")
            await _record_watch(ctx, project_id, write=write, route=route, blocked=blocked)
            memo[key] = "deny" if blocked else "watch"
    if memo[key] != "deny":
        return
    if write:
        raise HTTPException(403, NOT_ASSIGNED)
    raise HTTPException(404, NOT_FOUND)


def _is_write(ctx: dict, default: bool = True) -> bool:
    method = (ctx or {}).get("_pa_method")
    if not method:
        return default
    return method.upper() not in _SAFE_METHODS


async def check_project_for_request(ctx: dict, project_id) -> None:
    """check_project_access with write/read taken from the request method
    (a GET counts as a read); write when the method is not known."""
    await check_project_access(ctx, project_id, write=_is_write(ctx))


# ---------------------------------------------------------------------------
# Child records -> project id
# ---------------------------------------------------------------------------
# kind -> (collection, links). Each link is (field, parent kind); the parent
# kind "project" means the field already holds a project id. A record's own
# project_id is always tried first. "@entity" links read an (entity type,
# entity id) pair stored on the record.
_KINDS: dict[str, tuple[str, tuple]] = {
    "project":                  ("projects", ()),
    "daily_report":             ("daily_reports", ()),
    "daily_report_activity":    ("daily_report_activities", (("daily_report_id", "daily_report"),)),
    "daily_presence":           ("daily_presence", (("daily_report_id", "daily_report"),)),
    "daily_report_attendance":  ("daily_report_attendance", (("daily_report_id", "daily_report"),)),
    "photo_review":             ("photo_reviews", (("daily_report_id", "daily_report"),)),
    "delay":                    ("delays", ()),
    "rfi":                      ("rfis", ()),
    "qc_issue":                 ("qc_issues", ()),
    "safety_event":             ("safety_events", ()),
    "coordination_event":       ("coordination_events", ()),
    "delivery":                 ("deliveries", ()),
    "photo":                    ("photos", (("daily_report_id", "daily_report"),)),
    "photo_tray":               ("photo_tray", (("daily_report_id", "daily_report"),)),
    "submittal":                ("submittals", ()),
    "submittal_review_event":   ("submittal_review_events", (("submittal_id", "submittal"),)),
    "task":                     ("tasks", ()),
    "work_item":                ("work_items", ()),
    "work_item_link":           ("work_item_links", (("work_item_id", "work_item"),)),
    "work_item_history":        ("work_item_history", (("work_item_id", "work_item"),)),
    "change_order":             ("change_orders", ()),
    "drawing":                  ("drawings", ()),
    "drawing_revision":         ("drawing_revisions", (("drawing_id", "drawing"),)),
    "drawing_markup":           ("drawing_markups", (("drawing_revision_id", "drawing_revision"),)),
    "project_spec":             ("project_specs", ()),
    "spec":                     ("specs", ()),
    "spec_section":             ("spec_sections", (("project_spec_id", "project_spec"),
                                                   ("spec_id", "project_spec"))),
    "voice_annotation":         ("voice_annotations", (("@entity", "linked_entity_type", "linked_entity_id"),)),
    "notification":             ("notifications", (("@entity", "entity_type", "entity_id"),)),
    "field_suggestion":         ("field_suggestions", (("@entity", "entity_type", "entity_id"),)),
    "approval":                 ("approvals", (("@entity", "entity_type", "entity_id"),)),
    "edit_request":             ("edit_requests", (("daily_report_id", "daily_report"),)),
    "mitigation":               ("mitigations", (("linked_delay_id", "delay"), ("delay_id", "delay"))),
    "gating_entity":            ("gating_entities", ()),
    "cascade_snapshot":         ("cascade_snapshots", (("gating_entity_id", "gating_entity"),)),
    "schedule_diff":            ("schedule_update_diffs", ()),
    "schedule_proposal":        ("schedule_proposals", ()),
    "schedule_import":          ("schedule_imports", ()),
    "logic_proposal":           ("logic_proposals", (("task_id", "task"),)),
    "activity_name_suggestion": ("activity_name_suggestions", (("task_id", "task"),)),
    "tia_generation":           ("tia_generations", (("delay_id", "delay"),)),
    "recovery_report":          ("recovery_reports", ()),
    "ai_recovery_suggestion":   ("ai_recovery_suggestions", (("task_id", "task"), ("delay_id", "delay"))),
    "marketplace_posting":      ("marketplace_postings", ()),
    "marketplace_response":     ("marketplace_responses", (("posting_id", "marketplace_posting"),)),
    "location":                 ("locations", ()),
    "floor":                    ("floors", ()),
    "crew":                     ("crews", (("home_project_id", "project"),)),
    "exhibit_export":           ("exhibit_exports", ()),
}

# Other names the same kinds go by (entity_type values stored on rows, URL
# words, collection names).
_ALIASES: dict[str, str] = {
    "schedule_activity": "task", "activity": "task", "photo_tag": "photo",
    "dr": "daily_report", "report": "daily_report",
    "dr_activity": "daily_report_activity", "presence": "daily_presence",
    "attendance": "daily_report_attendance", "field_issue": "work_item",
    "coordination": "coordination_event", "qc": "qc_issue", "safety": "safety_event",
    "markup": "drawing_markup", "revision": "drawing_revision",
    "tia": "tia_generation", "proposal": "schedule_proposal",
}
for _k, (_coll, _l) in list(_KINDS.items()):
    _ALIASES.setdefault(_coll, _k)


def normalize_kind(kind: Optional[str]) -> Optional[str]:
    """Map any spelling of a kind ("daily-reports", "DailyReport",
    "daily_reports", "schedule_activity") to a registry key, or None."""
    if not kind or not isinstance(kind, str):
        return None
    k = re.sub(r"(?<!^)(?=[A-Z])", "_", kind.strip()).lower().replace("-", "_")
    if k in _KINDS:
        return k
    if k in _ALIASES:
        return _ALIASES[k]
    if k.endswith("s") and k[:-1] in _KINDS:
        return k[:-1]
    return None


async def resolve_project_id(ctx: dict, kind: str, record_id, *, _depth: int = 0) -> Optional[str]:
    """The project a record belongs to, following up to three parent links
    (daily report activity -> daily report -> project; drawing markup ->
    revision -> drawing -> project). None when the record, or its project,
    cannot be found, or the kind is unknown.

    The lookup is by id only; the project check that follows is limited to
    the caller's company, so a record from another company never grants
    anything (it simply resolves to a project the caller does not have,
    and check_project_access then leaves the caller's own 404 in place)."""
    k = normalize_kind(kind)
    if not k or not isinstance(record_id, str) or not record_id or _depth > 3:
        return None
    if k == "project":
        return record_id
    cache = ctx.setdefault("_pa_resolved", {}) if isinstance(ctx, dict) else {}
    ckey = (k, record_id)
    if ckey in cache:
        return cache[ckey]
    coll, links = _KINDS[k]
    fields = {"_id": 0, "project_id": 1}
    for link in links:
        if link[0] == "@entity":
            fields[link[1]] = 1
            fields[link[2]] = 1
        else:
            fields[link[0]] = 1
    pid = None
    try:
        row = await get_db()[coll].find_one({"id": record_id}, fields)
    except Exception:
        row = None
    if row:
        pid = row.get("project_id") if isinstance(row.get("project_id"), str) else None
        for link in links:
            if pid:
                break
            if link[0] == "@entity":
                pid = await resolve_project_id(ctx, row.get(link[1]), row.get(link[2]),
                                               _depth=_depth + 1)
            else:
                parent_id = row.get(link[0])
                if link[1] == "project":
                    pid = parent_id if isinstance(parent_id, str) and parent_id else None
                else:
                    pid = await resolve_project_id(ctx, link[1], parent_id, _depth=_depth + 1)
    cache[ckey] = pid
    return pid


async def check_record_access(ctx: dict, kind: str, record_id, *, write: bool,
                              route: str = "") -> Optional[str]:
    """Resolve a child record's project and check it. Returns the project id
    (or None when it could not be resolved; then nothing is checked)."""
    if is_full_admin(ctx):
        return None
    pid = await resolve_project_id(ctx, kind, record_id)
    if pid:
        await check_project_access(ctx, pid, write=write, route=route)
    return pid


# ---------------------------------------------------------------------------
# List filters
# ---------------------------------------------------------------------------
async def project_scope_filter(ctx: dict, query: Optional[dict] = None,
                               field: str = "project_id", *,
                               include_company_level: bool = False) -> dict:
    """Return `query` limited to the caller's projects when the switch is ON
    and the caller is not an all-projects admin. Unchanged otherwise (lists
    are never recorded in the watch log while the switch is OFF).

    Records with no project (project_id missing or null) are hidden by the
    filter unless include_company_level=True; pass True only where such
    records were company-wide before (for example notifications or KB
    entries with no project) and say so at the call site."""
    q = dict(query or {})
    if not is_enforced(ctx):
        return q
    allowed = await allowed_project_ids(ctx)
    if allowed is None:
        return q
    ids: list = sorted(allowed)
    if include_company_level:
        ids = ids + [None]
    cond = {"$in": ids}
    if field in q:
        return {"$and": [q, {field: cond}]}
    q[field] = cond
    return q


async def filter_rows(ctx: dict, rows: Iterable[dict], field: str = "project_id", *,
                      include_company_level: bool = False) -> list:
    """Python-side version of project_scope_filter for rows already loaded
    (aggregations, merged lists). Same switch and admin rules."""
    rows = list(rows or [])
    if not is_enforced(ctx):
        return rows
    allowed = await allowed_project_ids(ctx)
    if allowed is None:
        return rows
    out = []
    for r in rows:
        pid = (r or {}).get(field)
        if pid in allowed or (include_company_level and not pid):
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# Route-level dependency
# ---------------------------------------------------------------------------
# Paths that are never checked here: the MS Project worker, secret-protected
# cron calls, the Stripe webhook, public sign-in, health checks, company-
# level settings and the team page (which has its own admin checks).
EXEMPT_PREFIXES = (
    "/api/auth/", "/api/health", "/api/internal/", "/api/cron/",
    "/api/webhook/", "/api/tenants/current", "/api/voice/config",
    "/api/daily-reports/voice/config", "/api/admin/runtime-settings",
    "/api/admin/msp-worker", "/api/team/", "/api/ws",
)

# Path parameter names that hold a project id.
PROJECT_PARAMS = ("project_id", "pid")

# The word in the URL just before an id parameter -> the kind of record.
SEGMENT_KINDS: dict[str, str] = {
    "daily-reports": "daily_report",
    "daily-report-activities": "daily_report_activity",
    "daily-presence": "daily_presence",
    "daily-report-attendance": "daily_report_attendance",
    "photo-reviews": "photo_review",
    "coordination-events": "coordination_event",
    "delays": "delay",
    "safety-events": "safety_event",
    "qc-issues": "qc_issue",
    "photos": "photo",
    "photo-tags": "photo",
    "photo-tray": "photo_tray",
    "deliveries": "delivery",
    "rfis": "rfi",
    "submittals": "submittal",
    "tasks": "task",
    "work-items": "work_item",
    "change-orders": "change_order",
    "specs": "project_spec",
    "drawings": "drawing",
    "drawing-revisions": "drawing_revision",
    "drawing-markups": "drawing_markup",
    "voice-annotations": "voice_annotation",
    "audio": "voice_annotation",
    "notifications": "notification",
    "edit-requests": "edit_request",
    "approvals": "approval",
    "mitigations": "mitigation",
    "gating-entities": "gating_entity",
    "schedule-diffs": "schedule_diff",
    "schedule-proposals": "schedule_proposal",
    "logic-proposals": "logic_proposal",
    "activity-name-suggestions": "activity_name_suggestion",
    "field-suggestions": "field_suggestion",
    "recovery-reports": "recovery_report",
    "generations": "tia_generation",
    "recovery": "ai_recovery_suggestion",
    "locations": "location",
    "floors": "floor",
    "postings": "marketplace_posting",
    "release-payment": "marketplace_response",
}

_PARAM_RE = re.compile(r"\{([^}:]+)(?::[^}]*)?\}")


def targets_for_route(template: str, path_params: dict, query_params=None) -> list[tuple[str, str]]:
    """The (kind, id) pairs a request names: project id path parameters,
    known child-record path parameters, and a project_id query parameter.
    Generic routes are understood too: /entities/{entity_type}/{entity_id}
    and /crud/{entity}/{id}."""
    out: list[tuple[str, str]] = []
    params = dict(path_params or {})
    segments = [s for s in (template or "").split("/") if s]
    for i, seg in enumerate(segments):
        m = _PARAM_RE.fullmatch(seg)
        if not m:
            continue
        name = m.group(1)
        value = params.get(name)
        if not isinstance(value, str) or not value:
            continue
        if name in PROJECT_PARAMS:
            out.append(("project", value))
            continue
        prev = segments[i - 1] if i > 0 else ""
        prev_param = _PARAM_RE.fullmatch(prev)
        if prev_param:
            # /entities/{entity_type}/{entity_id}, /crud/{entity}/{id}
            kind = normalize_kind(SEGMENT_KINDS.get(str(params.get(prev_param.group(1)) or ""))
                                  or params.get(prev_param.group(1)))
            if kind:
                out.append((kind, value))
            continue
        kind = SEGMENT_KINDS.get(prev)
        if kind:
            out.append((kind, value))
    if query_params is not None:
        try:
            qpid = query_params.get("project_id")
        except Exception:
            qpid = None
        if isinstance(qpid, str) and qpid and "," not in qpid:
            out.append(("project", qpid))
    # de-duplicate, keep order
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def is_exempt(path: str) -> bool:
    return any((path or "").startswith(p) for p in EXEMPT_PREFIXES)


async def enforce_project_scope(conn: HTTPConnection) -> None:
    """Route-level dependency on every /api route (wired in server.py).

    For a signed-in request that names a project (path or project_id query
    parameter) or a record that belongs to one, check the caller may use it:
    a write is a request whose method is not GET/HEAD/OPTIONS. Unsigned
    requests, exempt paths and WebSocket connections pass straight through;
    the endpoint's own checks still run afterwards. The sign-in is resolved
    once and shared with the endpoint (auth.context_for_connection)."""
    if conn.scope.get("type") != "http":
        return
    method = (conn.scope.get("method") or "GET").upper()
    if method == "OPTIONS":
        return
    route = conn.scope.get("route")
    template = getattr(route, "path", None) or conn.url.path
    if is_exempt(template) or is_exempt(conn.url.path):
        return
    targets = targets_for_route(template, conn.path_params, conn.query_params)
    if not targets:
        return
    from auth import context_for_connection
    ctx = await context_for_connection(conn)
    if not ctx or not ctx.get("tenant_id") or is_full_admin(ctx):
        return
    write = method not in _SAFE_METHODS

    async def _pid(kind: str, rid: str) -> Optional[str]:
        return rid if kind == "project" else await resolve_project_id(ctx, kind, rid)

    # Read the caller's assignments and look up the named records at the
    # same time, so a daily-report save waits for one database round trip
    # here, not two (both lookups are indexed and never raise).
    results = await asyncio.gather(allowed_project_ids(ctx),
                                   *[_pid(k, r) for k, r in targets])
    for pid in results[1:]:
        if pid:
            await check_project_access(ctx, pid, write=write, route=template)


# ---------------------------------------------------------------------------
# Admin helpers shared by the team and project pages
# ---------------------------------------------------------------------------
async def company_admins(db, tenant_id: str) -> dict[str, str]:
    """{user_id: access scope} for every active admin of the company.
    Home-company users take their role from users.role (the same rule as
    sign-in); members from other companies from their tenant_users row."""
    from auth import effective_access_scope
    out: dict[str, str] = {}
    memberships = await db.tenant_users.find(
        {"tenant_id": tenant_id}, {"_id": 0},
    ).to_list(length=None)
    by_user = {m.get("user_id"): m for m in memberships if m.get("user_id")}
    home_users = await db.users.find(
        {"tenant_id": tenant_id},
        {"_id": 0, "id": 1, "role": 1, "access_scope": 1, "removal_pending": 1},
    ).to_list(length=None)
    home_ids = set()
    for u in home_users:
        uid = u.get("id")
        home_ids.add(uid)
        m = by_user.get(uid) or {}
        if m and (m.get("status") or "active") != "active":
            continue
        # Being removed right now (see remove_member): already not counted,
        # so two removals at the same moment cannot both pass the check.
        if u.get("removal_pending"):
            continue
        role = (u.get("role") or "").strip().lower()
        if role != "admin":
            continue
        out[uid] = effective_access_scope(role, m.get("access_scope") or u.get("access_scope"))
    for uid, m in by_user.items():
        if uid in home_ids or (m.get("status") or "") != "active":
            continue
        role = (m.get("role") or "").strip().lower()
        if role == "admin":
            out[uid] = effective_access_scope(role, m.get("access_scope"))
    return out


async def ensure_keeps_full_admin(db, tenant_id: str, user_id: str, *,
                                  new_role: Optional[str] = None,
                                  new_scope: Optional[str] = None,
                                  removing: bool = False) -> bool:
    """Refuse (409) a change that would leave the company with no active
    all-projects admin: limiting, demoting or removing the last one.
    Returns True when the member is an all-projects admin right now (the
    caller then re-checks after saving; see has_full_admin)."""
    from auth import effective_access_scope
    admins = await company_admins(db, tenant_id)
    full = {uid for uid, sc in admins.items() if sc == "all"}
    if user_id not in full:
        return False
    if removing:
        still_full = False
    else:
        role_after = (new_role or "admin").strip().lower()
        scope_after = effective_access_scope(role_after, new_scope or admins.get(user_id))
        still_full = role_after == "admin" and scope_after == "all"
    if still_full:
        return True
    if not (full - {user_id}):
        raise HTTPException(409, LAST_FULL_ADMIN)
    return True


async def has_full_admin(db, tenant_id: str) -> bool:
    """True when the company still has at least one active all-projects
    admin. Called again AFTER a change is saved: two owners limiting or
    removing each other at the same moment both pass the check made before
    saving, so each one re-checks afterwards and undoes its own change (409)
    if nobody is left. Undoing only ever adds an admin back, so at least
    one all-projects admin always remains."""
    admins = await company_admins(db, tenant_id)
    return any(sc == "all" for sc in admins.values())
