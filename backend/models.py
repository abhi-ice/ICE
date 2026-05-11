"""Pydantic models — StrictModel pattern (extra='forbid') everywhere.

Per master prompt section 3:
> All schemas use model_config = ConfigDict(extra='forbid'). This caught a
> real production bug in the Railway codebase (prev_pct slipping into
> TaskCreate when it shouldn't have).
"""
from __future__ import annotations
from datetime import datetime, date, timezone
from typing import Optional, List, Literal, Any
from pydantic import BaseModel, ConfigDict, Field, EmailStr, field_validator
from uuid import uuid4


def gen_id() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    """Base for all create/update payloads. Rejects extra fields."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReadModel(BaseModel):
    """Base for response models. Ignores extras (e.g. mongo _id)."""
    model_config = ConfigDict(extra="ignore")


# ============================================================
# Bucket D Corrective (2026-05-08) — master-schedule task linkage
# ============================================================
# `linked_task_ids` is a list of tasks.id (the master schedule rows),
# NOT per-DR WBS row IDs. Same scope as delay's existing `linked_task_
# ids` field, which was always the canonical linkage; the per-DR-scope
# field that briefly existed (`linked_dr_activity_ids`) was a wrong
# build and is migrated away by 007_dr_activity_to_task.
#
# Every event collection (rfis, qc_issues, safety_events,
# coordination_events, deliveries) carries this field. Delays already
# had it (with per-task impact + CPM cascade integration); the others
# now carry it as plain task linkage (no impact-days input -- only
# delays drive cascade math). Per-endpoint require-min-1 lives in the
# router so AI/voice/copilot creation paths (which bypass the HTTP
# layer via direct db.X.insert_one) aren't broken by Pydantic-level
# enforcement.
#
# Supplement fields: when a DR is locked (status=submitted) and a new
# fact arrives, the platform files the new event with is_supplement=
# true, daily_report_id=<today's container DR>, parent_daily_report_id
# =<the locked DR being supplemented>. The supplement flow does NOT
# change linkage scope -- linked_task_ids still references master
# tasks regardless of which DR the event sits on.
LINKAGE_MIN_ERR = (
    "At least one linked schedule task is required. "
    "Pick the master-schedule task(s) this event affects."
)


# ---------- Tenants & users ----------
TenantType = Literal["general_contractor", "subcontractor", "owner", "architect_engineer", "agency"]
TenantStatus = Literal["active", "trial", "suspended", "archived"]
UserRole = Literal["px", "pm", "super", "pe", "foreman", "admin", "viewer"]


class TenantCreate(StrictModel):
    legal_name: str
    display_name: str
    slug: str
    type: TenantType = "general_contractor"
    duns_number: Optional[str] = None
    cage_code: Optional[str] = None
    cmmc_level: Literal["none", "1", "2", "3"] = "none"


class Tenant(ReadModel):
    id: str
    legal_name: str
    display_name: str
    slug: str
    type: TenantType
    status: TenantStatus = "active"
    cmmc_level: str = "none"
    work_item_label: str = "Field Issue"
    created_at: datetime
    # Bucket F Phase 1 (2026-05-08) -- AI usage gate + monthly budget.
    # ai_enabled is a tenant-wide kill switch (admin can turn off all
    # photo AI for the tenant). budget tracking lets ops admins cap
    # spend per month with auto-pause + warning at 80%.
    ai_enabled: bool = True
    ai_monthly_budget_usd: float = 100.0
    ai_current_month_spend_usd: float = 0.0
    ai_current_month_resets_at: Optional[datetime] = None


class TenantAiBudgetUpdate(StrictModel):
    """Admin-only patch payload for tenant AI budget + kill switch.
    Both fields optional so partial updates are allowed."""
    ai_enabled: Optional[bool] = None
    ai_monthly_budget_usd: Optional[float] = Field(default=None, ge=0)


class UserRegister(StrictModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str
    organization_name: str = Field(min_length=2, max_length=80)


class InviteCreate(StrictModel):
    email: EmailStr
    role: Literal["admin", "pm", "super", "foreman", "viewer"] = "pm"


class InviteAccept(StrictModel):
    code: str = Field(min_length=8, max_length=24)
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str


class UserLogin(StrictModel):
    email: EmailStr
    password: str


class User(ReadModel):
    id: str
    email: EmailStr
    full_name: str
    tenant_id: str
    role: UserRole = "pm"
    created_at: datetime


class TokenResponse(ReadModel):
    access_token: str
    token_type: str = "bearer"
    user: User
    tenant: Tenant


# ---------- Projects ----------
ProjectStatus = Literal["planning", "mobilization", "active", "closeout", "complete", "cancelled"]
ContractType = Literal["lump_sum", "cost_plus", "gmp", "unit_price", "tm"]
# Followup-fix 6 (2026-05-07): basis for the project schedule clock.
# Picked once at project setup, surfaced in the TIA header (Section 11
# Calendar Reconciliation already cross-references this).
CalendarBasis = Literal["calendar_days", "working_days"]


class ProjectCreate(StrictModel):
    name: str
    contract_number: Optional[str] = None
    client_name: Optional[str] = None
    owner_name: Optional[str] = None
    project_type: Optional[str] = None
    naics_code: Optional[str] = None
    contract_value_usd: Optional[float] = None
    address_line1: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    start_date: Optional[date] = None
    planned_completion_date: Optional[date] = None
    status: ProjectStatus = "planning"
    prevailing_wage_required: bool = False
    section_3_required: bool = False
    buy_america_required: bool = False
    em_385_required: bool = False
    contract_type: ContractType = "lump_sum"
    # Followup-fix 6 (2026-05-07): TIA header inputs. All optional --
    # legacy projects render as null in TIA Section 1 until edited.
    agency: Optional[str] = None
    ntp_date: Optional[date] = None
    original_substantial_completion: Optional[date] = None
    current_substantial_completion: Optional[date] = None
    contract_days: Optional[int] = None
    calendar_basis: CalendarBasis = "calendar_days"
    # Bucket F Phase 1 (2026-05-08) -- CUI flag + per-project AI gate.
    # is_cui=true blocks photo AI analysis for compliance; ai_enabled
    # is a per-project off switch independent of the tenant-level
    # ai_enabled. Both default safe: false / true.
    is_cui: bool = False
    ai_enabled: bool = True


class ProjectUpdate(StrictModel):
    name: Optional[str] = None
    status: Optional[ProjectStatus] = None
    contract_value_usd: Optional[float] = None
    planned_completion_date: Optional[date] = None
    actual_completion_date: Optional[date] = None
    contract_number: Optional[str] = None
    client_name: Optional[str] = None
    address_line1: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    # Followup-fix 6 (2026-05-07): editable TIA header inputs.
    agency: Optional[str] = None
    ntp_date: Optional[date] = None
    original_substantial_completion: Optional[date] = None
    current_substantial_completion: Optional[date] = None
    contract_days: Optional[int] = None
    calendar_basis: Optional[CalendarBasis] = None
    # Bucket F Phase 1 -- editable AI gate.
    is_cui: Optional[bool] = None
    ai_enabled: Optional[bool] = None


class Project(ReadModel):
    id: str
    tenant_id: str
    name: str
    contract_number: Optional[str] = None
    client_name: Optional[str] = None
    owner_name: Optional[str] = None
    contract_value_usd: Optional[float] = None
    address_line1: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    start_date: Optional[date] = None
    planned_completion_date: Optional[date] = None
    actual_completion_date: Optional[date] = None
    status: ProjectStatus = "planning"
    prevailing_wage_required: bool = False
    em_385_required: bool = False
    contract_type: ContractType = "lump_sum"
    created_at: datetime
    # Followup-fix 6 (2026-05-07): TIA header fields surfaced on read.
    agency: Optional[str] = None
    ntp_date: Optional[date] = None
    original_substantial_completion: Optional[date] = None
    current_substantial_completion: Optional[date] = None
    contract_days: Optional[int] = None
    calendar_basis: Optional[str] = "calendar_days"
    # Bucket F Phase 1 (2026-05-08) -- CUI flag + per-project AI gate.
    is_cui: bool = False
    ai_enabled: bool = True


# ---------- Locations / floors ----------
class FloorCreate(StrictModel):
    project_id: str
    floor_number: int
    floor_name: str
    floor_height_ft: float = 10.0
    elevation_z_ft: float = 0.0
    gross_area_sqft: Optional[float] = None


class Floor(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    floor_number: int
    floor_name: str
    floor_height_ft: float
    elevation_z_ft: float
    gross_area_sqft: Optional[float] = None
    created_at: datetime


class LocationCreate(StrictModel):
    project_id: str
    floor_id: str
    name: str
    code: Optional[str] = None
    location_type: str = "room"
    polygon_geojson: Optional[dict] = None
    centroid_x: Optional[float] = None
    centroid_y: Optional[float] = None
    area_sqft: Optional[float] = None


class Location(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    floor_id: str
    name: str
    code: Optional[str] = None
    location_type: str = "room"
    polygon_geojson: Optional[dict] = None
    centroid_x: Optional[float] = None
    centroid_y: Optional[float] = None
    area_sqft: Optional[float] = None
    created_at: datetime


# ---------- Tasks (activities) ----------
TaskStatus = Literal["not_started", "in_progress", "blocked", "complete", "deleted"]
LookaheadStatus = Literal["not_in_window", "in_window", "proposed_new", "modified_from_master"]


class TaskCreate(StrictModel):
    """SOURCE OF TRUTH for proposed task payloads.

    CRITICAL (master prompt §3 + §20.1): Do NOT include `prev_pct` here.
    `prev_pct_complete` is set post-creation by the applier. The Railway
    codebase had a real bug where prev_pct slipped into TaskCreate via a
    diverged proposal helper. We pin this with extra='forbid' + tests.
    """
    project_id: str
    activity_code: str
    description: str
    wbs: Optional[str] = None
    trade_code: Optional[str] = None
    location_id: Optional[str] = None
    crew_id: Optional[str] = None
    planned_start: Optional[date] = None
    planned_finish: Optional[date] = None
    duration_days: Optional[float] = None
    budgeted_hours: Optional[float] = None
    budgeted_quantity: Optional[float] = None
    quantity_unit: Optional[str] = None
    is_milestone: bool = False
    predecessors: List[str] = Field(default_factory=list)
    source: str = "master_schedule"
    # Source-file ordering — preserves the row index from the imported MPP/XER/CSV
    # so the Tasks page can list activities in the same sequence the project
    # will be executed (e.g., 1.1, 1.2, ..., 1.21 instead of lexicographic
    # 1.1, 1.11, 1.12, ..., 1.2). Set when applying a schedule proposal.
    sequence: Optional[int] = None
    resource_names: List[str] = Field(default_factory=list)
    # Field-created (Daily Report) flags — populated by superintendent
    # creating an unplanned activity. Master/PM still owns master schedule.
    created_in_field: bool = False
    requires_pm_review: bool = False
    created_from_daily_report_id: Optional[str] = None
    # Bucket H Phase 1 (2026-05-08) -- resource-leveling demand fields.
    # required_trade names the trade that must staff this activity
    # (e.g., "Plumber"). required_headcount is how many of that trade
    # are needed concurrently. required_hours is total trade-hours for
    # the activity (used by the leveling engine to compute daily
    # demand = required_headcount when day in [planned_start..
    # planned_finish], or required_hours / duration_days for finer
    # granularity). All three are optional; activities without them
    # produce no demand (the leveling engine skips them rather than
    # fabricating numbers).
    required_trade: Optional[str] = None
    required_headcount: Optional[int] = None
    required_hours: Optional[float] = None


class TaskUpdate(StrictModel):
    description: Optional[str] = None
    status: Optional[TaskStatus] = None
    planned_start: Optional[date] = None
    planned_finish: Optional[date] = None
    actual_start: Optional[date] = None
    actual_finish: Optional[date] = None
    pct_complete: Optional[float] = Field(default=None, ge=0, le=100)
    actual_hours: Optional[float] = None
    actual_quantity: Optional[float] = None
    location_id: Optional[str] = None
    crew_id: Optional[str] = None
    trade_code: Optional[str] = None
    lookahead_status: Optional[LookaheadStatus] = None
    duration_days: Optional[float] = None
    predecessors: Optional[List[str]] = None
    is_milestone: Optional[bool] = None
    wbs: Optional[str] = None
    trade_code: Optional[str] = None
    crew_id: Optional[str] = None
    location_id: Optional[str] = None
    # Bucket H Phase 1 -- patchable demand fields. Changing any of
    # these triggers a demand recompute on the affected date range
    # (Phase 4 wires the trigger).
    required_trade: Optional[str] = None
    required_headcount: Optional[int] = None
    required_hours: Optional[float] = None


class CrewAssignmentEntry(ReadModel):
    """Bucket C1 Phase 1 — one entry per crew change recorded against a task
    via the daily-report grid sync write-back. Append-only history for later
    productivity analysis; the current crew lives on task.current_crew_id."""
    crew_id: Optional[str] = None
    daily_report_id: Optional[str] = None
    assigned_date: Optional[date] = None


class LastSyncedFromDR(ReadModel):
    """Bucket C1 Phase 1 — denormalized 'last write-back from a daily
    report' marker. sync_state flips provisional → confirmed when the
    parent daily report is submitted."""
    daily_report_id: Optional[str] = None
    daily_report_date: Optional[date] = None
    synced_at: Optional[datetime] = None
    sync_state: Literal["provisional", "confirmed"] = "provisional"


class Task(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    activity_code: str
    wbs: Optional[str] = None
    description: str
    trade_code: Optional[str] = None
    location_id: Optional[str] = None
    crew_id: Optional[str] = None
    status: TaskStatus = "not_started"
    planned_start: Optional[date] = None
    planned_finish: Optional[date] = None
    actual_start: Optional[date] = None
    actual_finish: Optional[date] = None
    duration_days: Optional[float] = None
    budgeted_hours: Optional[float] = None
    actual_hours: float = 0
    budgeted_quantity: Optional[float] = None
    actual_quantity: float = 0
    quantity_unit: Optional[str] = None
    pct_complete: float = 0
    prev_pct_complete: float = 0
    is_milestone: bool = False
    is_critical_path: bool = False
    total_float_days: Optional[int] = None
    lookahead_status: LookaheadStatus = "not_in_window"
    predecessors: List[str] = Field(default_factory=list)
    sequence: Optional[int] = None
    resource_names: List[str] = Field(default_factory=list)
    # Field-created (Daily Report) flags
    created_in_field: bool = False
    requires_pm_review: bool = False
    created_from_daily_report_id: Optional[str] = None
    created_at: datetime
    # Bucket C1 Phase 1 — sync layer state. Optional / default-None so
    # records persisted before the sync layer landed continue to deserialize.
    updated_at: Optional[datetime] = None
    actual_hours_to_date: float = 0
    current_crew_id: Optional[str] = None
    crew_assignment_history: List[CrewAssignmentEntry] = Field(default_factory=list)
    last_synced_from_dr: Optional[LastSyncedFromDR] = None
    external_modified_after_sync: bool = False
    # Bucket C1 Phase 2 — computed/derived status fields. Persisted so
    # other endpoints can filter on them; recomputed on every event that
    # could move them (delay open/close, EOD% change, list_tasks read).
    computed_status: Literal["on_track", "behind", "blocked", "complete"] = "on_track"
    has_open_blocker: bool = False
    open_blocker_count: int = 0
    open_blocker_summary: List[dict] = Field(default_factory=list)
    # Bucket C1 Phase 4 — CPM walk-forward result. None until first
    # delay open / status compute writes it.
    projected_finish_date: Optional[date] = None
    # Bucket H Phase 1 -- resource-leveling demand fields. Optional;
    # tasks without these produce no demand (leveling engine skips).
    required_trade: Optional[str] = None
    required_headcount: Optional[int] = None
    required_hours: Optional[float] = None


# ---------- Daily reports ----------
class DailyReportCreate(StrictModel):
    project_id: str
    report_date: date
    weather_temp_high_f: Optional[float] = None
    weather_temp_low_f: Optional[float] = None
    weather_conditions: Optional[str] = None
    weather_precipitation_in: Optional[float] = None
    notes: Optional[str] = None

    # Addition 1 -- future-date guard. Pre-staging tomorrow's DR the
    # night before is a real super workflow, so we allow today + 1
    # day. Anything beyond that corrupts calendar reconciliation,
    # TIA chronology, and CPM projections (a DR dated 30 days out
    # would tag itself as "yesterday's DR" once the calendar caught
    # up, breaking idempotent today-resolver logic).
    @field_validator("report_date")
    @classmethod
    def _no_future_date(cls, v: date) -> date:
        from datetime import date as _date, timedelta as _td
        max_allowed = _date.today() + _td(days=1)
        if v > max_allowed:
            raise ValueError(
                "Daily report date cannot be more than 1 day in the future."
            )
        return v


# Addition 3 -- no-work-day acknowledgment vocabulary. Empty DR submit
# requires is_no_work_day + a typed reason so legal sees explicit
# "this was a planned no-work day" rather than "looks like the super
# forgot to log anything." Reasons map 1-1 to the modal dropdown.
NoWorkReason = Literal[
    "weather",                # storm / extreme cold / snow / etc.
    "holiday",                # observed holiday
    "scheduled_non_work",     # planned non-work day (e.g. Sunday)
    "crew_unavailable",       # crew didn't show
    "tenant_restriction",     # tenant or owner blocked access
    "other",                  # free-form, requires no_work_note
]


class DailyReport(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    report_date: date
    weather_temp_high_f: Optional[float] = None
    weather_temp_low_f: Optional[float] = None
    weather_conditions: Optional[str] = None
    weather_precipitation_in: Optional[float] = None
    weather_wind_mph: Optional[float] = None
    weather_fetched_at: Optional[datetime] = None
    notes: Optional[str] = None
    status: str = "draft"
    prepared_by: Optional[str] = None
    submitted_by: Optional[str] = None
    submitted_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    # Renovation-specific fields (rendered for project_type in renovation/tenant_improvement)
    occupancy_active: Optional[bool] = None
    occupied_areas: Optional[str] = None
    dust_controls_in_place: Optional[bool] = None
    noise_complaints_count: int = 0
    dust_violations_count: int = 0
    after_hours_work: Optional[bool] = None
    created_at: datetime
    # Addition 3 -- no-work-day metadata. Set on submit when the DR has
    # no activities AND no events; the backend enforces a typed reason
    # so legal review sees an explicit acknowledgment rather than an
    # empty record.
    is_no_work_day: bool = False
    no_work_reason: Optional[NoWorkReason] = None
    no_work_note: Optional[str] = None


ConstraintType = Literal[
    "not_constrained",
    "awaiting_rfi_response",
    "awaiting_submittal_return",
    "awaiting_bulletin_proposal",
    "awaiting_bulletin_approval",
    "awaiting_ae_direction",
    "awaiting_shutdown_window",
    "awaiting_tenant_access",
    "awaiting_clearance",
    "awaiting_predecessor_completion",
    "concealed_condition_under_investigation",
    "weather_impact",
    "labor_unavailability",
    "material_not_on_site",
    "other",
]


class DailyReportActivityUpdate(StrictModel):
    pct_complete_end_of_day: Optional[float] = Field(default=None, ge=0, le=100)
    crew_count: Optional[int] = None
    crew_id: Optional[str] = None
    # Phase E — FK to a daily_presence row picked from the activity grid
    # dropdown. Distinct from `crew_id` (which is the task's pre-assigned
    # crew). A super can pick any trade present on site for any activity.
    linked_crew_id: Optional[str] = None
    hours_worked: Optional[float] = None
    delay_minutes: Optional[int] = None
    delay_reason: Optional[str] = None
    weather_impact_minutes: Optional[int] = None
    notes: Optional[str] = None
    status_at_end_of_day: Optional[TaskStatus] = None
    # Bucket C1 Phase 1 — optimistic-lock token. The grid load sends the
    # task's updated_at it observed; the PATCH refuses with 409 if the
    # task has been modified externally in the meantime. Optional so old
    # frontends without the token fall through to unconditional write
    # (back-compat) and so brand-new tasks (no updated_at yet) accept
    # any token.
    task_last_modified_at: Optional[datetime] = None
    # Bucket C3a Phase 2 — constraint capture. Validated server-side:
    # if constraint_type is set and != not_constrained / != other,
    # constraint_reference_entity_id must point to an existing
    # gating_entities.id. If = other, constraint_other_description must
    # be non-empty.
    constraint_type: Optional[ConstraintType] = None
    constraint_reference_entity_type: Optional[str] = None
    constraint_reference_entity_id: Optional[str] = None
    constraint_other_description: Optional[str] = None
    production_impact_hours: Optional[float] = Field(default=None, ge=0)
    # Addition 4 -- required when the parent DR is locked.
    edit_reason: Optional[str] = Field(default=None, max_length=500)


class DailyReportActivity(ReadModel):
    id: str
    daily_report_id: str
    schedule_activity_id: str
    position: int
    pct_complete_start_of_day: float = 0
    pct_complete_end_of_day: float = 0
    crew_count: int = 0
    crew_id: Optional[str] = None
    # Phase E — FK to daily_presence row picked from the grid dropdown.
    linked_crew_id: Optional[str] = None
    hours_worked: float = 0
    delay_minutes: int = 0
    delay_reason: Optional[str] = None
    weather_impact_minutes: int = 0
    notes: Optional[str] = None
    status_at_end_of_day: TaskStatus = "not_started"
    # denormalized for grid display
    activity_code: Optional[str] = None
    activity_description: Optional[str] = None
    location_name: Optional[str] = None
    trade_code: Optional[str] = None
    # Bucket C1 Phase 1 — sync state for the corresponding write-back
    # against the linked task. Default provisional (frontend showed an
    # in-progress edit); flipped confirmed when the parent DR submits.
    sync_state: Literal["provisional", "confirmed"] = "provisional"
    last_sync_at: Optional[datetime] = None
    task_external_modified_warning: bool = False
    # Denormalized at /context read time (NOT persisted to mongo) — gives
    # the frontend the optimistic-lock token for the linked task without
    # forcing a second round-trip to /tasks.
    task_updated_at: Optional[datetime] = None
    # Bucket C3a Phase 2 — constraint capture (controlled vocab + ref to
    # the gating entity that's blocking this row, or free-text "other").
    # constraint_auto_filled marks rows where the system pre-populated
    # the constraint from an open gating entity (Phase 3).
    constraint_type: Optional[ConstraintType] = None
    constraint_reference_entity_type: Optional[str] = None
    constraint_reference_entity_id: Optional[str] = None
    constraint_other_description: Optional[str] = None
    production_impact_hours: Optional[float] = None
    constraint_auto_filled: bool = False
    # Bucket D Phase 1 — supplement metadata. Lets a supplement DR add
    # an activity row keyed to the parent DR's WBS, then surface it on
    # the parent's "Supplements" appendix in Phase 4.
    is_supplement: bool = False
    parent_daily_report_id: Optional[str] = None
    supplement_filed_at: Optional[datetime] = None
    supplement_filed_by: Optional[str] = None
    supplement_reason: Optional[str] = None
    # Bucket D Task 2 -- denormalized parent DR date so the per-row
    # supplement badge renders without a per-row fetch. Server-enriched
    # in /context bundle and /photos list (extend other lists if needed).
    parent_daily_report_date: Optional[str] = None


class DailyReportUpdate(StrictModel):
    """Patchable fields on DailyReport doc (weather + renovation + notes)."""
    weather_temp_high_f: Optional[float] = None
    weather_temp_low_f: Optional[float] = None
    weather_conditions: Optional[str] = None
    weather_precipitation_in: Optional[float] = None
    weather_wind_mph: Optional[float] = None
    notes: Optional[str] = None
    occupancy_active: Optional[bool] = None
    occupied_areas: Optional[str] = None
    dust_controls_in_place: Optional[bool] = None
    noise_complaints_count: Optional[int] = None
    dust_violations_count: Optional[int] = None
    after_hours_work: Optional[bool] = None


PresenceShift = Literal["first", "second", "third", "weekend", "holiday", "off_hours"]


class DailyPresenceCreate(StrictModel):
    daily_report_id: str
    trade: str
    sub_id: Optional[str] = None  # crew_id or external sub identifier
    company_name: Optional[str] = None
    headcount: int = 0
    hours_per_person: float = 8.0
    notes: Optional[str] = None
    # Bucket C3b Phase 3 — granular fields. Older callers (existing
    # PresencePanel) just send trade + headcount + hours; that still
    # works and lands as an aggregate row with shift=first by default.
    zone_id: Optional[str] = None       # FK to locations
    zone_name: Optional[str] = None     # denormalized (or free-text fallback)
    shift: Optional[PresenceShift] = "first"
    schedule_activity_id: Optional[str] = None  # link to specific task
    sub_name: Optional[str] = None      # alias the spec uses; we'll fill company_name from this if provided


class DailyPresenceUpdate(StrictModel):
    trade: Optional[str] = None
    sub_id: Optional[str] = None
    company_name: Optional[str] = None
    headcount: Optional[int] = None
    hours_per_person: Optional[float] = None
    notes: Optional[str] = None
    zone_id: Optional[str] = None
    zone_name: Optional[str] = None
    shift: Optional[PresenceShift] = None
    schedule_activity_id: Optional[str] = None
    sub_name: Optional[str] = None


class DailyPresence(ReadModel):
    id: str
    tenant_id: str
    daily_report_id: str
    project_id: Optional[str] = None  # auto-filled on write (Phase 3)
    trade: str
    sub_id: Optional[str] = None
    company_name: Optional[str] = None
    headcount: int = 0
    hours_per_person: float = 8.0
    notes: Optional[str] = None
    # Phase 3 — granular layer.
    zone_id: Optional[str] = None
    zone_name: Optional[str] = None
    shift: Optional[PresenceShift] = "first"
    schedule_activity_id: Optional[str] = None
    sub_name: Optional[str] = None
    total_hours: float = 0.0  # computed at write time (headcount * hours_per_person)
    created_by: Optional[str] = None
    created_at: datetime


class UnplannedActivityCreate(StrictModel):
    """Superintendent-created activity from the field. Marks
    requires_pm_review=true so the PM can promote/merge into master."""
    description: str
    trade_code: Optional[str] = None
    location_id: Optional[str] = None
    crew_id: Optional[str] = None
    duration_days: Optional[float] = 1
    notes: Optional[str] = None


# ---------- Resources ----------
class CrewCreate(StrictModel):
    crew_code: str
    name: str
    trade: str
    foreman_person_id: Optional[str] = None
    home_yard_address: Optional[str] = None
    hourly_burdened_cost: Optional[float] = None
    target_size: int = 4
    notes: Optional[str] = None
    # Bucket H Phase 1 (2026-05-08) -- resource-leveling supply fields.
    # default_headcount = the baseline supply pool this crew contributes
    # to the cross-project capacity model (separate from target_size,
    # which is a planning-target attribute). company_name lets the
    # leveling UI group crews by external vendor for sub-contracted
    # work. home_project_id marks pre-commitments so the leveling
    # engine can prefer that crew to that project when feasible.
    default_headcount: Optional[int] = None
    company_name: Optional[str] = None
    home_project_id: Optional[str] = None


class CrewUpdate(StrictModel):
    """Bucket H Phase 1 -- crew update payload. All optional; PATCH
    flows through this so the resource-leveling fields stay editable
    post-create. trade is patchable but a change triggers capacity
    recomputation downstream (Phase 4)."""
    crew_code: Optional[str] = None
    name: Optional[str] = None
    trade: Optional[str] = None
    foreman_person_id: Optional[str] = None
    home_yard_address: Optional[str] = None
    hourly_burdened_cost: Optional[float] = None
    target_size: Optional[int] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    default_headcount: Optional[int] = None
    company_name: Optional[str] = None
    home_project_id: Optional[str] = None


class Crew(ReadModel):
    id: str
    tenant_id: str
    crew_code: str
    name: str
    trade: str
    foreman_person_id: Optional[str] = None
    home_yard_address: Optional[str] = None
    hourly_burdened_cost: Optional[float] = None
    target_size: int = 4
    status: str = "active"
    notes: Optional[str] = None
    created_at: datetime
    # Bucket H Phase 1 -- resource-leveling supply fields.
    default_headcount: Optional[int] = None
    company_name: Optional[str] = None
    home_project_id: Optional[str] = None


# ============================================================
# Bucket H Phase 1 (2026-05-08) -- Resource Leveling Engine
# ============================================================
# Two new collections backing the cross-project capacity / demand /
# gap model:
#
#   crew_capacity  -- per (crew, date) supply row. Total headcount,
#                     how many are committed elsewhere, what's left.
#   crew_demands   -- per (project, trade, date) demand row, derived
#                     from schedule_activities with required_trade +
#                     required_headcount populated. Each demand row
#                     lists the contributing task ids so the
#                     dashboard can drill back to the source.
#
# Gap = sum(supply.available_headcount over trade,date) - sum(demand
# .demanded_headcount over trade,date). Gap is NOT persisted in
# Phase 1 -- computed on read by the service. Phase 5 may cache if
# read-time scan becomes a bottleneck.
#
# computed_from enum distinguishes baseline rows (manual_entry or
# migration backfill) from rows reconciled to actuals via daily_
# presence (presence_recorded) or from a fresh schedule import
# (schedule_derived). Lets the UI flag "this is a guess" vs "this
# is reality."

CrewCapacitySource = Literal["manual_entry", "schedule_derived",
                              "presence_recorded", "migration_backfill"]


class CrewCapacityCreate(StrictModel):
    """Phase 2 will own the upload endpoint that produces these rows;
    Phase 1 just defines the shape so the migration can backfill."""
    crew_id: str
    date: date
    total_headcount: int = Field(..., ge=0)
    committed_headcount: int = Field(default=0, ge=0)
    hours_per_person: float = Field(default=8.0, ge=0)
    is_holiday: bool = False
    notes: Optional[str] = None
    computed_from: CrewCapacitySource = "manual_entry"


class CrewCapacityUpdate(StrictModel):
    """Phase 2 capacity override flow. total_headcount + is_holiday +
    notes are patchable; the rest is recomputed by the service."""
    total_headcount: Optional[int] = Field(default=None, ge=0)
    committed_headcount: Optional[int] = Field(default=None, ge=0)
    hours_per_person: Optional[float] = Field(default=None, ge=0)
    is_holiday: Optional[bool] = None
    notes: Optional[str] = None


class CrewCapacity(ReadModel):
    id: str
    tenant_id: str
    crew_id: str
    # Denormalized for display + filtering -- avoids per-row joins on
    # the dashboard. Kept in sync by the recompute triggers (Phase 4).
    trade: str
    company_name: Optional[str] = None
    date: date
    total_headcount: int
    committed_headcount: int = 0
    available_headcount: int = 0   # = total - committed; computed
    hours_per_person: float = 8.0
    is_holiday: bool = False
    notes: Optional[str] = None
    computed_at: datetime
    computed_from: CrewCapacitySource = "manual_entry"


class CrewDemandCreate(StrictModel):
    """Phase 2 will own the recompute endpoint; Phase 1 just defines
    the shape so the migration can backfill from schedule_activities."""
    project_id: str
    date: date
    trade: str
    demanded_headcount: int = Field(..., ge=0)
    source_task_ids: List[str] = Field(default_factory=list)
    hours_demanded: float = Field(default=0.0, ge=0)
    is_critical_path: bool = False


class CrewDemand(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    date: date
    trade: str
    demanded_headcount: int
    source_task_ids: List[str] = Field(default_factory=list)
    hours_demanded: float = 0.0
    is_critical_path: bool = False
    computed_at: datetime


# Bucket H Phase 3 (2026-05-08) -- per-project membership table.
# Backs the RBAC scoping rules in resource_leveling_router (super
# sees only projects where they're a member with role pm/super/
# foreman; PM sees projects where role_on_project=pm; etc.). Also
# powers the new Project Access management page.
ProjectMemberRole = Literal["pm", "super", "foreman", "viewer"]


class ProjectMemberCreate(StrictModel):
    project_id: str
    user_id: str
    role_on_project: ProjectMemberRole = "viewer"


class ProjectMemberUpdate(StrictModel):
    """Only role_on_project is mutable. project_id + user_id changes
    are explicit add+remove flows."""
    role_on_project: ProjectMemberRole


class ProjectMember(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    user_id: str
    role_on_project: ProjectMemberRole
    added_at: datetime
    added_by: Optional[str] = None


# Gap rows are not persisted in Phase 1. The service computes them
# on read via supply - demand aggregates. If read-time scan becomes
# a bottleneck (50+ crews × 90 days × N trades), Phase 5 may add a
# materialized crew_gaps collection.
GapSeverity = Literal["none", "surplus", "minor", "moderate", "major"]


class GapRow(ReadModel):
    """Phase 2 service return shape. Not persisted in Phase 1."""
    trade: str
    date: date
    supply: int
    demand: int
    gap: int                            # demand - supply (positive = shortage)
    severity: GapSeverity
    is_critical_path: bool = False      # any contributing task on CP
    contributing_project_ids: List[str] = Field(default_factory=list)
    contributing_crew_ids: List[str] = Field(default_factory=list)


class EquipmentCreate(StrictModel):
    asset_tag: str
    type: str
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    year: Optional[int] = None
    ownership: str = "owned"
    hourly_rate: Optional[float] = None
    daily_rate: Optional[float] = None
    monthly_rate: Optional[float] = None


class Equipment(ReadModel):
    id: str
    tenant_id: str
    asset_tag: str
    type: str
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    year: Optional[int] = None
    ownership: str = "owned"
    hourly_rate: Optional[float] = None
    daily_rate: Optional[float] = None
    monthly_rate: Optional[float] = None
    status: str = "available"
    created_at: datetime


class PersonCreate(StrictModel):
    employee_number: Optional[str] = None
    first_name: str
    last_name: str
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    role: Optional[str] = None
    trade: Optional[str] = None
    employment_type: str = "ft"
    union_local: Optional[str] = None
    prevailing_wage_classification: Optional[str] = None
    hourly_rate: Optional[float] = None


class Person(ReadModel):
    id: str
    tenant_id: str
    employee_number: Optional[str] = None
    first_name: str
    last_name: str
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    role: Optional[str] = None
    trade: Optional[str] = None
    employment_type: str = "ft"
    union_local: Optional[str] = None
    prevailing_wage_classification: Optional[str] = None
    hourly_rate: Optional[float] = None
    e_verify_status: str = "pending"
    i9_on_file: bool = False
    cleared_for_federal: bool = False
    status: str = "active"
    created_at: datetime


class CertificationCreate(StrictModel):
    person_id: str
    certification_type: str
    issued_date: date
    expires_date: Optional[date] = None
    issuing_authority: Optional[str] = None
    certificate_number: Optional[str] = None


class Certification(ReadModel):
    id: str
    tenant_id: str
    person_id: str
    certification_type: str
    issued_date: date
    expires_date: Optional[date] = None
    issuing_authority: Optional[str] = None
    certificate_number: Optional[str] = None
    verified: bool = False
    created_at: datetime


# ---------- RFIs / change orders / submittals ----------
RfiStatus = Literal["draft", "sent", "in_review", "answered", "closed", "voided"]
Priority = Literal["low", "normal", "high", "urgent"]


class RfiCreate(StrictModel):
    """Followup-fix (2026-05-07) — the frontend RFI create form sends
    `question` / `trade_code` / `answer` / `status`, but the backend
    schema only had `description` and rejected the rest as extras
    (StrictModel = extra=forbid). Result: every Create returned a
    generic 422 'Field required' the user couldn't recover from.

    Accept the frontend's field names via Pydantic aliases (so either
    `description` or `question` works, either `response_text` or
    `answer` works) and persist `trade_code`. `status` is accepted but
    ignored on create -- the backend continues to force initial status
    to 'draft' per the existing state-machine contract; Bucket B will
    revisit the state machine."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True,
                                populate_by_name=True)

    project_id: str
    subject: str
    description: str = Field(alias="question")
    trade_code: Optional[str] = None
    response_text: Optional[str] = Field(default=None, alias="answer")
    # Accepted-but-ignored on create (kept so frontend forms can submit
    # without a 422). The router force-sets status='draft' regardless.
    status: Optional[str] = None
    to_party: Optional[str] = None
    from_party: Optional[str] = None
    schedule_activity_id: Optional[str] = None
    location_id: Optional[str] = None
    daily_report_id: Optional[str] = None
    spec_section_refs: List[str] = Field(default_factory=list)
    drawing_refs: List[dict] = Field(default_factory=list)
    date_required: Optional[date] = None
    priority: Priority = "normal"
    # Bucket D Corrective (2026-05-08) -- master-schedule task linkage.
    # References tasks.id (the master schedule), not daily_report_
    # activities.id. Required min-1 enforced in the router (POST /rfis);
    # copilot/voice paths bypass via insert_one. Coexists with the C3a
    # gating_entities back-pointer (gating_entity_id is set after RFI
    # insert by the create_for_entity hook) -- the gating row drives the
    # cascade dashboard, this list drives TIA rollup + gallery filtering
    # + form validation.
    linked_task_ids: List[str] = Field(default_factory=list)
    is_supplement: bool = False
    parent_daily_report_id: Optional[str] = None
    supplement_reason: Optional[str] = None


class RfiUpdate(StrictModel):
    subject: Optional[str] = None
    description: Optional[str] = None
    response_text: Optional[str] = None
    status: Optional[RfiStatus] = None
    date_responded: Optional[date] = None
    cost_impact_estimated_usd: Optional[float] = None
    time_impact_estimated_days: Optional[int] = None
    # Bucket D Corrective -- re-pickable post-create. Re-validated
    # against tasks (master schedule) when present in the patch.
    linked_task_ids: Optional[List[str]] = None
    # Addition 4 -- required when the parent DR is locked. Server
    # rejects locked-DR patches with a missing or short reason.
    edit_reason: Optional[str] = Field(default=None, max_length=500)


class Rfi(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    rfi_number: str
    subject: str
    description: str
    # Followup-fix (2026-05-07) -- surfaces the trade_code now persisted
    # by RfiCreate. The list view's "Trade" column was always pointed
    # at this key but the read model didn't expose it (extra=ignore
    # silently dropped it).
    trade_code: Optional[str] = None
    to_party: Optional[str] = None
    from_party: Optional[str] = None
    schedule_activity_id: Optional[str] = None
    location_id: Optional[str] = None
    daily_report_id: Optional[str] = None
    spec_section_refs: List[str] = Field(default_factory=list)
    date_initiated: date
    date_required: Optional[date] = None
    date_responded: Optional[date] = None
    response_text: Optional[str] = None
    status: RfiStatus = "draft"
    priority: Priority = "normal"
    cost_impact_estimated_usd: Optional[float] = None
    time_impact_estimated_days: Optional[int] = None
    created_by: Optional[str] = None
    created_at: datetime
    # Bucket C3a Phase 1 — back-pointer to the gating_entities row that
    # represents this RFI as a scheduleable CPM node. Auto-populated on
    # POST /rfis. Older records get this via the 002 backfill.
    gating_entity_id: Optional[str] = None
    # Bucket D Corrective (2026-05-08) -- master-schedule linkage +
    # supplement metadata. linked_task_ids references tasks.id, not the
    # per-DR daily_report_activities row.
    linked_task_ids: List[str] = Field(default_factory=list)
    is_supplement: bool = False
    parent_daily_report_id: Optional[str] = None
    supplement_filed_at: Optional[datetime] = None
    supplement_filed_by: Optional[str] = None
    supplement_reason: Optional[str] = None
    # Bucket D Task 2 -- denormalized parent DR date so the per-row
    # supplement badge renders without a per-row fetch. Server-enriched
    # in /context bundle and /photos list (extend other lists if needed).
    parent_daily_report_date: Optional[str] = None


class ChangeOrderCreate(StrictModel):
    project_id: str
    title: str
    description: str
    type: Literal["additive", "deductive", "no_change"] = "additive"
    originating_rfi_id: Optional[str] = None
    schedule_activity_id: Optional[str] = None
    amount_usd: float = 0
    time_extension_days: int = 0


class ChangeOrder(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    co_number: str
    title: str
    description: str
    type: str
    originating_rfi_id: Optional[str] = None
    schedule_activity_id: Optional[str] = None
    amount_usd: float
    time_extension_days: int
    status: str = "proposed"
    proposed_at: datetime
    approved_at: Optional[datetime] = None
    created_at: datetime


# ---------- Field events ----------
Severity = Literal["minor", "moderate", "major", "critical"]
# Bucket D Phase 5 -- QC severity superset. observation/minor/major are
# scored gradations, rework_required triggers the synthetic-delay
# cascade, stop_work pauses the affected activities. Severity stays
# the canonical type for delays/safety; QC rows can carry either
# (legacy Severity values) or one of the QC-specific ones below.
QcSeverity = Literal["observation", "minor", "moderate", "major",
                       "critical", "rework_required", "stop_work"]


DelayStatus = Literal["open", "in_progress", "resolved"]


class TaskImpactInput(StrictModel):
    """Per-linked-task impact entered on the delay form."""
    task_id: str
    projected_impact_days: float = 0


class TaskImpact(ReadModel):
    """Per-linked-task impact stored on the delay record. Float snapshot
    is captured at the moment the impact is logged so the analysis is
    reproducible even after CPM recomputes change task float."""
    task_id: str
    projected_impact_days: float = 0
    float_at_logging_days: Optional[float] = None
    consumes_float_only: bool = False
    pushes_critical_path: bool = False


class DelayCreate(StrictModel):
    project_id: str
    # Legacy single-task linkage — kept for API back-compat (tests, voice
    # pipeline). New form sends linked_task_ids/per_task_impacts instead.
    schedule_activity_id: Optional[str] = None
    location_id: Optional[str] = None
    daily_report_id: Optional[str] = None
    date: date
    description: str
    severity: Severity = "minor"
    photo_urls: List[str] = Field(default_factory=list)
    delay_days: Optional[float] = None
    reason_category: Optional[str] = None
    # New (Phase A): multi-task linkage with per-task impact entries.
    linked_task_ids: List[str] = Field(default_factory=list)
    per_task_impacts: List[TaskImpactInput] = Field(default_factory=list)
    status: DelayStatus = "open"
    # Followup-fix 7 (2026-05-07): owner attribution + cause class for
    # TIA Section 4 (Owner Attribution Summary). Same vocabulary as
    # GatingEntity so cross-entity rollups stay consistent.
    owner_attribution: Optional[str] = "none"  # OwnerAttribution literal
    cause_class: Optional[str] = "other"        # CauseClass literal
    # Bucket D Corrective (2026-05-08) -- the DR-activity linkage that
    # used to live here was a redundant second scope. Delays already
    # carry linked_task_ids (master schedule scope) above with per-task
    # impact + CPM cascade integration -- that's the canonical linkage.
    # Supplement fields stay (they describe DR-as-parent semantics, not
    # activity scope).
    is_supplement: bool = False
    parent_daily_report_id: Optional[str] = None
    supplement_reason: Optional[str] = None


class DelayUpdate(StrictModel):
    description: Optional[str] = None
    severity: Optional[Severity] = None
    reason_category: Optional[str] = None
    photo_urls: Optional[List[str]] = None
    linked_task_ids: Optional[List[str]] = None
    per_task_impacts: Optional[List[TaskImpactInput]] = None
    status: Optional[DelayStatus] = None
    resolved_date: Optional[date] = None
    resolved_note: Optional[str] = None
    # AI report (set by /generate-ai-report endpoint, not the form)
    ai_report: Optional[str] = None
    # Followup-fix 7 (2026-05-07): editable post-create via Delay detail.
    owner_attribution: Optional[str] = None
    cause_class: Optional[str] = None
    # Bucket D Corrective (2026-05-08) -- linked_dr_activity_ids removed
    # from DelayUpdate; the canonical linkage is linked_task_ids above
    # (already editable post-create, drives CPM cascade).
    # Addition 4 -- required when the parent DR is locked.
    edit_reason: Optional[str] = Field(default=None, max_length=500)


class Delay(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    schedule_activity_id: Optional[str] = None  # legacy
    location_id: Optional[str] = None
    daily_report_id: Optional[str] = None
    date: date
    description: str
    severity: Severity
    photo_urls: List[str] = Field(default_factory=list)
    delay_days: Optional[float] = None  # legacy
    reason_category: Optional[str] = None
    status: DelayStatus = "open"
    reported_by: Optional[str] = None
    reported_at: datetime
    # Followup-fix 7 (2026-05-07): expose owner_attribution + cause_class
    # so list reads + TIA generator can render them. Default to "none" /
    # "other" so legacy rows that didn't carry these fields surface as
    # "no attribution".
    owner_attribution: Optional[str] = "none"
    cause_class: Optional[str] = "other"
    # Bucket D Corrective (2026-05-08) -- linked_dr_activity_ids dropped
    # (was a redundant second scope). Canonical linkage is
    # linked_task_ids below; supplement fields here describe DR-as-parent
    # semantics, not activity scope.
    is_supplement: bool = False
    parent_daily_report_id: Optional[str] = None
    supplement_filed_at: Optional[datetime] = None
    supplement_filed_by: Optional[str] = None
    supplement_reason: Optional[str] = None
    # Bucket D Task 2 -- denormalized parent DR date so the per-row
    # supplement badge renders without a per-row fetch. Server-enriched
    # in /context bundle and /photos list (extend other lists if needed).
    parent_daily_report_date: Optional[str] = None
    # Master-schedule linkage (Phase A). Default empty so old records
    # continue to deserialize until the migration runs.
    linked_task_ids: List[str] = Field(default_factory=list)
    per_task_impacts: List[TaskImpact] = Field(default_factory=list)
    logged_date: Optional[date] = None
    resolved_date: Optional[date] = None
    resolved_note: Optional[str] = None
    resolved_by_user_id: Optional[str] = None
    duration_days: Optional[int] = None  # computed at read time
    # AI report (Phase B) — saved between views so regen is opt-in.
    ai_report: Optional[str] = None
    ai_report_generated_at: Optional[datetime] = None
    # Bucket C1 Phase 5 — populated when status transitions to resolved.
    # cpm_impact_summary entries: {task_id, projected_impact_days,
    # actual_days_open, absorbed_days, restored_float_days,
    # projected_finish_shift_days}.
    cpm_impact_summary: List[dict] = Field(default_factory=list)
    released_float_on_resolution: List[str] = Field(default_factory=list)
    # Bucket C2 Phase 1 — AI mitigation scenarios. active_scenarios holds
    # the current 3 mitigations.id values (A/B/C). scenario_history is a
    # log of past generations (each entry: {generated_at, scenario_ids: [3 ids]})
    # so the user can compare how AI thinking evolved across regenerations.
    # chosen_mitigation_id stays null until Phase 2's /choose endpoint runs.
    active_scenarios: List[str] = Field(default_factory=list)
    scenario_history: List[dict] = Field(default_factory=list)
    chosen_mitigation_id: Optional[str] = None
    scenarios_last_generated_at: Optional[datetime] = None
    # Bucket C2 Phase 2 — snapshot of per_task_impacts at the moment a
    # scenario was chosen, so /unchoose can restore the original split.
    # Populated only while chosen_mitigation_id is set; cleared on unchoose.
    per_task_impacts_pre_choose: Optional[List[dict]] = None
    created_at: datetime


SafetyEventType = Literal["near_miss", "first_aid", "recordable", "lost_time", "fatality", "other"]


class SafetyEventCreate(StrictModel):
    """Phase C — stripped to description + type + severity + audit. Old
    fields (schedule_activity_id, location_id, photo_urls, osha_recordable)
    intentionally removed; rely on ReadModel.extra='ignore' for legacy
    Mongo docs that still carry them."""
    project_id: str
    daily_report_id: Optional[str] = None
    date: date
    description: str
    severity: Severity = "minor"
    event_type: SafetyEventType = "near_miss"
    # Bucket D Corrective (2026-05-08) -- master-schedule task linkage.
    # OPTIONAL (some safety events are area-wide and don't tie to a
    # specific scheduled task).
    linked_task_ids: List[str] = Field(default_factory=list)
    is_supplement: bool = False
    parent_daily_report_id: Optional[str] = None
    supplement_reason: Optional[str] = None


class SafetyEventUpdate(StrictModel):
    """Addition 4 -- supports edit-in-place on locked DRs (and
    correction edits on draft DRs). Required-on-locked: edit_reason."""
    description: Optional[str] = None
    severity: Optional[Severity] = None
    event_type: Optional[SafetyEventType] = None
    linked_task_ids: Optional[List[str]] = None
    edit_reason: Optional[str] = Field(default=None, max_length=500)


class SafetyEvent(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    daily_report_id: Optional[str] = None
    date: date
    description: str
    severity: Severity
    event_type: str
    status: str = "open"
    reported_by: Optional[str] = None
    reported_at: datetime
    created_at: datetime
    # Bucket D Corrective (2026-05-08) -- master-schedule linkage +
    # supplement metadata.
    linked_task_ids: List[str] = Field(default_factory=list)
    is_supplement: bool = False
    parent_daily_report_id: Optional[str] = None
    supplement_filed_at: Optional[datetime] = None
    supplement_filed_by: Optional[str] = None
    supplement_reason: Optional[str] = None
    # Bucket D Task 2 -- denormalized parent DR date so the per-row
    # supplement badge renders without a per-row fetch. Server-enriched
    # in /context bundle and /photos list (extend other lists if needed).
    parent_daily_report_date: Optional[str] = None


class QcIssueCreate(StrictModel):
    """Phase C — stripped to description + severity + audit. Old fields
    (schedule_activity_id, location_id, photo_urls, spec_section_ref)
    removed."""
    project_id: str
    daily_report_id: Optional[str] = None
    date: date
    description: str
    severity: str = "minor"  # see QcSeverity for Phase 5 vocabulary
    # Bucket D Corrective (2026-05-08) -- master-schedule task linkage.
    # Required min-1 for QC (drives the rework cascade); copilot/voice
    # paths bypass via insert_one.
    linked_task_ids: List[str] = Field(default_factory=list)
    is_supplement: bool = False
    parent_daily_report_id: Optional[str] = None
    supplement_reason: Optional[str] = None
    # Bucket D Phase 5 -- super-entered rework duration estimate when
    # severity=rework_required. Required by router-side validation in
    # that case. Basis is free text explaining the estimate; persisted
    # for calibration RAG (Phase C2 pattern).
    rework_duration_hours: Optional[float] = None
    rework_duration_basis: Optional[str] = None


class QcIssueUpdate(StrictModel):
    """Bucket D Phase 1 — added so PATCH paths can flow through Pydantic
    instead of raw dict updates. All fields optional, including the
    new linkage list and resolution fields used by C2/C3 cascade flows.
    Phase 5 -- adds rework outcome fields used by /qc-issues/{id}/
    record-rework-outcome to capture actuals."""
    description: Optional[str] = None
    severity: Optional[str] = None
    status: Optional[str] = None
    resolution_notes: Optional[str] = None
    linked_task_ids: Optional[List[str]] = None
    # Phase 5 -- rework estimate fields (editable post-create until
    # outcome is recorded).
    rework_duration_hours: Optional[float] = None
    rework_duration_basis: Optional[str] = None
    # Phase 5 -- outcome fields (separate endpoint preferred, but
    # PATCH is allowed too for admin corrections).
    rework_actual_hours: Optional[float] = None
    rework_outcome_notes: Optional[str] = None
    # Addition 4 -- required when the parent DR is locked.
    edit_reason: Optional[str] = Field(default=None, max_length=500)


class QcReworkOutcome(StrictModel):
    """Bucket D Phase 5 -- outcome capture when rework finishes. Mirrors
    the C2 mitigation outcome pattern; the synthetic delay's actual_days
    is updated to match and the cascade is re-run."""
    rework_actual_hours: float = Field(..., ge=0)
    rework_outcome_notes: Optional[str] = None


class QcIssue(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    daily_report_id: Optional[str] = None
    date: date
    description: str
    severity: str
    status: str = "open"
    reported_by: Optional[str] = None
    reported_at: datetime
    resolved_at: Optional[datetime] = None
    resolution_notes: Optional[str] = None
    created_at: datetime
    # Bucket D Corrective (2026-05-08) -- master-schedule linkage +
    # supplement metadata.
    linked_task_ids: List[str] = Field(default_factory=list)
    is_supplement: bool = False
    parent_daily_report_id: Optional[str] = None
    supplement_filed_at: Optional[datetime] = None
    supplement_filed_by: Optional[str] = None
    supplement_reason: Optional[str] = None
    # Bucket D Task 2 -- denormalized parent DR date so the per-row
    # supplement badge renders without a per-row fetch. Server-enriched
    # in /context bundle and /photos list (extend other lists if needed).
    parent_daily_report_date: Optional[str] = None
    # Bucket D Phase 5 -- rework cascade fields.
    rework_duration_hours: Optional[float] = None
    rework_duration_basis: Optional[str] = None
    rework_estimated_at: Optional[datetime] = None
    rework_estimated_by: Optional[str] = None
    ai_benchmark_hours: Optional[float] = None
    ai_benchmark_basis: Optional[str] = None
    rework_actual_hours: Optional[float] = None
    rework_outcome_notes: Optional[str] = None
    rework_completed_at: Optional[datetime] = None
    rework_outcome_recorded_by: Optional[str] = None
    linked_synthetic_delay_id: Optional[str] = None


# ---------- Photos ----------
PhotoClass = Literal["progress", "discovery", "damage", "quality", "safety", "coordination", "closeout"]
EVIDENTIARY_CLASSES = ("discovery", "damage", "quality")


class PhotoCreate(StrictModel):
    project_id: str
    location_id: Optional[str] = None
    schedule_activity_id: Optional[str] = None
    daily_report_id: Optional[str] = None
    storage_path: str
    caption: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    # Bucket C3b Phase 4 — required class tag (default progress for back-
    # compat with old upload UIs). Optional drawing pin at upload time.
    photo_class: PhotoClass = "progress"
    drawing_pin_id: Optional[str] = None
    pin_drawing_id: Optional[str] = None
    pin_x: Optional[float] = None
    pin_y: Optional[float] = None


class PhotoUpdate(StrictModel):
    """Phase 4 — caption + class editable; file is NOT replaceable
    (tamper-evidence). Class change writes its own audit entry so the
    chain shows when an evidentiary class flipped to/from progress."""
    caption: Optional[str] = None
    photo_class: Optional[PhotoClass] = None
    tags: Optional[List[str]] = None
    # Addition 4 -- required when the parent DR is locked.
    edit_reason: Optional[str] = Field(default=None, max_length=500)


class PhotoPinPayload(StrictModel):
    drawing_pin_id: Optional[str] = None
    pin_drawing_id: Optional[str] = None
    pin_x: Optional[float] = None
    pin_y: Optional[float] = None


class Photo(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    location_id: Optional[str] = None
    schedule_activity_id: Optional[str] = None
    daily_report_id: Optional[str] = None
    storage_path: str
    thumbnail_path: Optional[str] = None
    captured_at: datetime
    captured_by: Optional[str] = None
    caption: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    visual_fingerprint_match_id: Optional[str] = None
    visual_fingerprint_match_confidence: Optional[float] = None
    visual_fingerprint_overridden: bool = False
    # EXIF + perceptual hash (master prompt §13)
    phash: Optional[str] = None
    captured_at_exif: Optional[str] = None
    gps_lat: Optional[float] = None
    gps_lng: Optional[float] = None
    camera_make: Optional[str] = None
    camera_model: Optional[str] = None
    created_at: datetime
    # Bucket C3b Phase 4 — evidentiary discipline. sha256_hash is computed
    # at upload from file bytes and never changes; mismatch_flag is true
    # when device clock vs server clock skew exceeds 60 seconds.
    sha256_hash: Optional[str] = None
    device_timestamp: Optional[datetime] = None
    server_received_timestamp: Optional[datetime] = None
    timestamp_mismatch_flag: bool = False
    exif_preserved: bool = True
    photo_class: PhotoClass = "progress"
    evidentiary_flag: bool = False  # auto: class in evidentiary set AND pin present
    drawing_pin_id: Optional[str] = None
    pin_drawing_id: Optional[str] = None
    pin_x: Optional[float] = None
    pin_y: Optional[float] = None
    # Bucket D Phase 1 — Photo reframed as the tagging junction. Each
    # row is now ONE tag connecting a tray photo to ONE event (XOR rule
    # enforced at the Phase 2 tagging endpoint, not here -- legacy rows
    # carry no event-ref and the migration backfills tray_photo_id only).
    tray_photo_id: Optional[str] = None
    daily_report_activity_id: Optional[str] = None  # tag to a DR row
    delay_id: Optional[str] = None
    rfi_id: Optional[str] = None
    qc_issue_id: Optional[str] = None
    safety_event_id: Optional[str] = None
    coordination_event_id: Optional[str] = None
    delivery_id: Optional[str] = None
    tagged_at: Optional[datetime] = None
    tagged_by: Optional[str] = None
    tag_caption: Optional[str] = None
    is_supplement: bool = False
    parent_daily_report_id: Optional[str] = None
    supplement_filed_at: Optional[datetime] = None
    supplement_filed_by: Optional[str] = None
    supplement_reason: Optional[str] = None
    # Bucket D Task 2 -- denormalized parent DR date so the per-row
    # supplement badge renders without a per-row fetch. Server-enriched
    # in /context bundle and /photos list (extend other lists if needed).
    parent_daily_report_date: Optional[str] = None
    # Bucket F Phase 1 (2026-05-08) -- AI suggestion lineage on the
    # tag row. Stamped by the tag-create endpoint when the user
    # accepts an AI suggestion from the tray. created_via_ai_
    # suggestion=true means the user clicked "Accept all" or saved
    # without changing AI-supplied fields; false means manual tag.
    created_via_ai_suggestion: bool = False
    ai_suggestion_id: Optional[str] = None
    ai_confidence_at_tag_time: Optional[float] = Field(default=None, ge=0, le=1)
    # Bucket F Phase 5 (2026-05-11) -- pull-forward from Phase 3 Risk #1.
    # Snapshots tray.ai_user_overridden AT the moment of tag creation so
    # the gallery indicator stays accurate even if the tray row is later
    # archived or its override state changes. Without this snapshot the
    # "AI badge" on a tagged photo can drift from the truth as users
    # re-touch suggestions.
    user_overridden_at_tag_time: bool = False


# ============================================================
# Bucket D Phase 1 — Photo tray (staging area before tagging)
# Bucket F Phase 1 (2026-05-08) — AI analysis fields
# ============================================================
PhotoCaptureSource = Literal["mobile_camera", "manual_upload", "drawing_pin"]
PhotoTrayStatus = Literal["untagged", "partially_tagged", "fully_tagged", "archived"]

# Bucket F Phase 1 -- AI analysis lifecycle.
#   pending          -- upload landed, queued for analysis
#   analyzing        -- service is mid-call to Claude Vision
#   complete         -- analysis returned + persisted
#   failed           -- service errored (see ai_failure_reason)
#   skipped          -- CUI project / AI disabled / tenant ai_enabled=false
#                       / pre-existing tray rows from migration
#   rejected_by_user -- user explicitly rejected the suggestion
#   budget_paused    -- tenant budget hit; admin must raise cap
PhotoAiAnalysisStatus = Literal[
    "pending", "analyzing", "complete", "failed", "skipped",
    "rejected_by_user", "budget_paused",
]

# Conservative class subset the AI suggests. Tagging UI can still
# pick any value from the broader PhotoClass enum -- AI just stays
# in the high-signal lane.
PhotoAiSuggestedClass = Literal[
    "progress", "discovery", "damage", "quality", "other",
]

PhotoAiSuggestedEventType = Literal[
    "delay", "rfi", "qc_issue", "safety_event",
    "coordination_event", "delivery", "none",
]


class PhotoTrayCreate(StrictModel):
    """Phase 2 will own the upload endpoint that produces these rows;
    Phase 1 just defines the shape so the migration can backfill from
    existing photos."""
    project_id: str
    capture_source: PhotoCaptureSource = "manual_upload"
    source_path: Optional[str] = None
    storage_key: str
    phash: Optional[str] = None
    sha256: Optional[str] = None
    exif_taken_at: Optional[datetime] = None
    exif_gps_lat: Optional[float] = None
    exif_gps_lng: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    file_size_bytes: Optional[int] = None


class PhotoTray(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    uploaded_by: Optional[str] = None
    uploaded_at: datetime
    capture_source: PhotoCaptureSource = "manual_upload"
    source_path: Optional[str] = None
    storage_key: str
    phash: Optional[str] = None
    sha256: Optional[str] = None
    exif_taken_at: Optional[datetime] = None
    exif_gps_lat: Optional[float] = None
    exif_gps_lng: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    file_size_bytes: Optional[int] = None
    status: PhotoTrayStatus = "untagged"
    # Bucket F Phase 1 -- AI analysis fields. Default "pending" on
    # fresh upload; migration 010 stamps "skipped" on legacy rows so
    # we never accidentally bill for analyzing pre-AI photos. Every
    # AI write goes through services/photo_ai.py.
    ai_analysis_status: PhotoAiAnalysisStatus = "pending"
    ai_analysis_started_at: Optional[datetime] = None
    ai_analysis_completed_at: Optional[datetime] = None
    ai_suggested_class: Optional[PhotoAiSuggestedClass] = None
    ai_suggested_caption: Optional[str] = None
    ai_suggested_event_type: Optional[PhotoAiSuggestedEventType] = None
    ai_suggested_trade: Optional[str] = None
    ai_confidence_score: Optional[float] = Field(default=None, ge=0, le=1)
    ai_raw_response: Optional[dict] = None
    ai_model_version: Optional[str] = None
    ai_user_overridden: bool = False
    ai_user_override_reason: Optional[str] = None
    ai_safety_concerns_detected: List[str] = Field(default_factory=list)
    ai_failure_reason: Optional[str] = None
    ai_cost_tokens_input: Optional[int] = None
    ai_cost_tokens_output: Optional[int] = None
    ai_reused_from_phash: bool = False


class PhotoTagCreate(StrictModel):
    """Phase 2 endpoint payload. Exactly one of the event-ref fields
    must be non-null; XOR enforced router-side."""
    tray_photo_id: str
    daily_report_id: str
    daily_report_activity_id: Optional[str] = None
    delay_id: Optional[str] = None
    rfi_id: Optional[str] = None
    qc_issue_id: Optional[str] = None
    safety_event_id: Optional[str] = None
    coordination_event_id: Optional[str] = None
    delivery_id: Optional[str] = None
    drawing_pin_id: Optional[str] = None
    photo_class: PhotoClass = "progress"
    tag_caption: Optional[str] = None
    # Bucket F Phase 1 -- AI lineage on the tag itself. Phase 2's tag
    # endpoint stamps these from the tray row when the user accepts
    # an AI suggestion. Lets TIA Section 8 distinguish AI-suggested
    # tags from user-authored ones + lets Calibration RAG roll up
    # override patterns by suggestion ancestry.
    created_via_ai_suggestion: bool = False
    ai_suggestion_id: Optional[str] = None
    ai_confidence_at_tag_time: Optional[float] = Field(default=None, ge=0, le=1)
    # Bucket F Phase 5 -- snapshot of tray.ai_user_overridden at tag
    # time. Sets the user_overridden_at_tag_time field on Photo (above).
    user_overridden_at_tag_time: bool = False


# ---------- Work items / field issues ----------
WorkItemStatus = Literal["open", "investigating", "in_progress", "blocked", "resolved", "closed"]


class WorkItemCreate(StrictModel):
    project_id: str
    location_id: Optional[str] = None
    title: str
    description: str
    priority: Priority = "normal"
    root_cause: Optional[str] = None
    estimated_cost_impact: Optional[float] = None
    estimated_time_impact_days: Optional[float] = None
    target_resolution_date: Optional[date] = None


class WorkItem(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    location_id: Optional[str] = None
    title: str
    description: str
    status: WorkItemStatus = "open"
    priority: Priority = "normal"
    root_cause: Optional[str] = None
    estimated_cost_impact: Optional[float] = None
    actual_cost_impact: Optional[float] = None
    estimated_time_impact_days: Optional[float] = None
    actual_time_impact_days: Optional[float] = None
    opened_by: Optional[str] = None
    opened_at: datetime
    owned_by: Optional[str] = None
    target_resolution_date: Optional[date] = None
    closed_at: Optional[datetime] = None
    closure_summary: Optional[str] = None
    created_at: datetime
    # Bucket C3a Phase 1 — back-pointer to the gating_entities CPM node.
    gating_entity_id: Optional[str] = None


class WorkItemLinkCreate(StrictModel):
    """Polymorphic link pattern (master prompt §3)."""
    work_item_id: str
    entity_type: Literal["task", "rfi", "delay", "safety_event", "qc_issue", "photo", "delivery", "change_order"]
    entity_id: str
    relationship: str = "related"
    notes: Optional[str] = None


class WorkItemLink(ReadModel):
    id: str
    tenant_id: str
    work_item_id: str
    entity_type: str
    entity_id: str
    relationship: str
    notes: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime


# ---------- Approvals ----------
ApprovalKind = Literal["schedule_change", "work_item_closure", "change_order", "daily_report_submit",
                       "spec_change", "drawing_revision", "ai_recovery_action", "marketplace_response"]
ApprovalStatus = Literal["pending", "approved", "rejected", "expired"]


class ApprovalCreate(StrictModel):
    project_id: str
    kind: ApprovalKind
    subject_entity_type: str
    subject_entity_id: str
    proposed_payload: dict = Field(default_factory=dict)
    required_approvers: List[str] = Field(default_factory=list)
    quorum_required: int = 1
    notes: Optional[str] = None


class ApprovalDecision(StrictModel):
    decision: Literal["approved", "rejected"]
    notes: Optional[str] = None


class Approval(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    kind: ApprovalKind
    subject_entity_type: str
    subject_entity_id: str
    proposed_payload: dict
    proposed_by: Optional[str] = None
    proposed_at: datetime
    required_approvers: List[str] = Field(default_factory=list)
    quorum_required: int = 1
    status: ApprovalStatus = "pending"
    decisions: List[dict] = Field(default_factory=list)
    final_decided_at: Optional[datetime] = None
    final_decided_by: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime


# ---------- Drawings & specs ----------
class DrawingCreate(StrictModel):
    project_id: str
    sheet_number: str
    title: str
    discipline: Optional[str] = None
    drawing_set_name: str = "Issued for Construction"


class Drawing(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    drawing_set_name: str
    sheet_number: str
    title: str
    discipline: Optional[str] = None
    revision_count: int = 1
    current_revision_id: Optional[str] = None
    qr_code_url: Optional[str] = None
    status: str = "current"
    created_at: datetime


class SpecCreate(StrictModel):
    project_id: str
    name: str
    revision_label: Optional[str] = None


class ProjectSpec(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    name: str
    revision_label: Optional[str] = None
    pdf_url: Optional[str] = None
    pdf_page_count: Optional[int] = None
    total_sections: int = 0
    processing_status: str = "pending"
    created_at: datetime


class SpecSection(ReadModel):
    id: str
    tenant_id: str
    project_spec_id: str
    division: str
    section_code: str
    section_title: str
    parent_section_id: Optional[str] = None
    hierarchy_path: str
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    text_content: str = ""
    created_at: datetime


# ---------- AI ----------
class CopilotMessage(StrictModel):
    project_id: Optional[str] = None
    daily_report_id: Optional[str] = None
    message: str
    session_id: Optional[str] = None


# ============================================================
# Schedule proposal — incoming MPP/XER/CSV is a proposal until
# a PM/Admin approves it. Federal-contract baseline-change trail.
# ============================================================
class ScheduleProposal(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    filename: str
    kind: str  # "schedule"
    status: str  # "pending_review" | "approved" | "rejected"
    parsed_rows: list[dict] = Field(default_factory=list)
    diff: dict = Field(default_factory=dict)
    ai_review: Optional[dict] = None
    uploaded_by: str
    uploaded_at: datetime
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    review_notes: Optional[str] = None
    applied_result: Optional[dict] = None
    parse_warnings: list[str] = Field(default_factory=list)


class ScheduleProposalDecision(StrictModel):
    notes: Optional[str] = None


class CopilotResponse(ReadModel):
    session_id: str
    response: str
    citations: List[dict] = Field(default_factory=list)
    usage: dict = Field(default_factory=dict)


# ============================================================
# Bucket C2 — AI mitigation scenarios for delays
# ============================================================
MitigationSource = Literal["ai_recovery_engine", "human_proposal", "sub_proposal", "owner_request"]
MitigationDecisionStatus = Literal["proposed", "chosen", "not_chosen", "superseded", "executed", "outcome_recorded"]
MitigationRiskLevel = Literal["low", "medium", "high"]


class MitigationChooseRequest(StrictModel):
    """Phase 2 — payload for POST /mitigations/{id}/choose. Rationale
    optional; we still capture decision_made_by from auth context."""
    decision_rationale: Optional[str] = None


class MitigationOutcomeRequest(StrictModel):
    """Phase 3 — payload for PATCH /mitigations/{id}/record-outcome.
    All three fields optional so a user without dollar/day numbers can
    submit just notes ("looks like the freight expedite worked but
    we don't have invoices yet"). At least one field MUST be provided
    for the call to count — backend rejects all-empty payloads."""
    actual_cost_usd: Optional[float] = None
    actual_time_recovered_days: Optional[float] = None
    outcome_notes: Optional[str] = None


class Mitigation(ReadModel):
    """One AI-generated (or human-proposed) mitigation scenario for a delay.

    Lives only on the sidebar Delay detail page; never surfaced on the
    daily report. AI normally generates 3 at a time labelled A/B/C; the
    user picks one (Phase 2) and records actuals on resolution (Phase 3).
    Old generations stay in this collection with status='superseded' so
    the calibration loop can RAG against historical scenario+outcome
    pairs."""
    id: str
    tenant_id: str
    project_id: str
    linked_delay_id: str
    linked_task_ids: List[str] = Field(default_factory=list)
    source: MitigationSource = "ai_recovery_engine"
    scenario_label: Optional[str] = None  # "A" | "B" | "C" | None for human
    strategy_name: str
    assumption: str
    proposed_action_description: str
    prerequisites: List[str] = Field(default_factory=list)
    estimated_schedule_impact_days: float = 0
    estimated_cost_impact_usd: float = 0
    estimated_risk_level: MitigationRiskLevel = "medium"
    estimated_confidence: float = 0.5
    ai_reasoning: str = ""
    decision_status: MitigationDecisionStatus = "proposed"
    proposed_at: datetime
    proposed_by_user_id: Optional[str] = None
    decision_made_at: Optional[datetime] = None
    decision_made_by_user_id: Optional[str] = None
    decision_rationale: Optional[str] = None
    executed_action_payload: Optional[dict] = None
    actual_cost_usd: Optional[float] = None
    actual_time_recovered_days: Optional[float] = None
    outcome_recorded_at: Optional[datetime] = None
    outcome_notes: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None


class RecoveryRequest(StrictModel):
    project_id: str
    target_activity_id: str
    trigger: str = "manual"


class AiRecoverySuggestion(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    target_activity_id: str
    generated_at: datetime
    trigger: str
    suggestions: List[dict]
    status: str = "pending"
    chosen_suggestion_index: Optional[int] = None
    applied_at: Optional[datetime] = None
    applied_by: Optional[str] = None


class RfiDraftRequest(StrictModel):
    project_id: str
    subject: str
    context: str
    schedule_activity_id: Optional[str] = None


# ============================================================
# Bucket C3a — Gating entities + project calendar
# ============================================================
GatingEntityType = Literal["rfi", "submittal", "bulletin", "field_issue",
                            "coordination_request", "mop", "hot_work_permit"]
GatingEntityStatus = Literal["draft", "submitted", "open", "under_review",
                              "awaiting_clarification", "responded", "closed",
                              "withdrawn", "expired"]
OwnerAttribution = Literal["gc", "a_e", "owner", "sub", "external_authority",
                            "tenant", "none"]
CauseClass = Literal["design_clarification", "design_change",
                      "differing_site_condition", "concealed_condition",
                      "tenant_coordination", "building_coordination",
                      "regulatory", "utility", "procurement",
                      # Bucket D Phase 5 -- synthetic delays from QC
                      # rework cascade. TIA Section 2 surfaces these
                      # as a discrete bucket.
                      "quality_rework",
                      "other"]


class GatingEntity(ReadModel):
    """Polymorphic CPM-node wrapper around RFI / Submittal / WorkItem /
    Bulletin / CoordinationRequest. Phase 1 wires RFI / Submittal /
    WorkItem (the three with existing endpoints); Bulletin and
    CoordinationRequest enums kept for forward compatibility.

    The underlying entity_id row keeps its entity-specific fields
    (rfi_number, subject, response_text, etc.) — this row holds the
    schedule-relevant projection (submitted_at, target_response_at,
    age, SLA flag, gates_task_ids) so the rest of the platform can
    treat all gating entities uniformly."""
    id: str
    tenant_id: str
    project_id: str
    entity_type: GatingEntityType
    entity_id: str  # FK to type-specific collection
    # Denormalized so the dropdown UI doesn't need a per-row join.
    entity_number: Optional[str] = None  # e.g. "RFI-014" / "SUB-008"
    display_label: Optional[str] = None  # e.g. "RFI-014: Cast iron line routing"

    # Schedule fields
    submitted_at: Optional[datetime] = None
    target_response_at: Optional[datetime] = None
    actual_responded_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    sla_working_days: int = 10

    # State
    status: GatingEntityStatus = "open"
    owner_attribution: OwnerAttribution = "none"
    cause_class: CauseClass = "other"

    # Linkage
    gates_task_ids: List[str] = Field(default_factory=list)

    # Computed at read time (not persisted authoritatively — refreshed
    # via services.gating_entities.compute_age_fields when read)
    current_age_calendar_days: Optional[int] = None
    current_age_working_days: Optional[int] = None
    is_over_sla: bool = False

    # Audit
    privileged: bool = False
    created_at: datetime
    updated_at: Optional[datetime] = None


class ProjectCalendarDay(ReadModel):
    """One row per (project, calendar_date). C3a only populates
    is_working_day; weather / closure tracking lands in C3b."""
    id: str
    tenant_id: str
    project_id: str
    calendar_date: date
    is_working_day: bool = True
    is_holiday: bool = False
    is_building_closed: bool = False
    is_restricted_access: bool = False
    notes: Optional[str] = None
    created_at: datetime


# ============================================================
# Bucket C3b — Coordination event log + per-trade-zone presence
# ============================================================
CoordinationChannel = Literal[
    "email", "phone", "in_person", "formal_letter",
    "text_message", "meeting", "system_notification",
]
CoordinationDirection = Literal["inbound", "outbound", "internal"]
CoordinationTopic = Literal[
    "tenant_access", "building_coordination", "a_e_clarification",
    "owner_direction", "sub_coordination", "regulatory", "safety",
    "quality", "scope", "payment", "other",
]
CoordinationOutcome = Literal[
    "agreement", "disagreement", "pending", "informational", "escalation_required",
]


class CoordinationEventCreate(StrictModel):
    """Phase 1 create payload. summary + occurred_at + channel + direction
    + topic + other_party_name are the minimum the platform demands; the
    rest fills in over time as evidence accumulates."""
    project_id: str
    occurred_at: datetime
    channel: CoordinationChannel
    direction: CoordinationDirection
    our_party_user_id: Optional[str] = None
    other_party_name: str
    other_party_org: Optional[str] = None
    other_party_role: Optional[str] = None
    other_party_email: Optional[str] = None
    other_party_phone: Optional[str] = None
    topic: CoordinationTopic
    topic_other_description: Optional[str] = None
    summary: str
    detail_text: Optional[str] = None
    outcome: CoordinationOutcome = "informational"
    outcome_notes: Optional[str] = None
    evidence_attachment_ids: List[str] = Field(default_factory=list)
    linked_rfi_ids: List[str] = Field(default_factory=list)
    linked_field_issue_ids: List[str] = Field(default_factory=list)
    linked_submittal_ids: List[str] = Field(default_factory=list)
    linked_bulletin_ids: List[str] = Field(default_factory=list)
    linked_activity_ids: List[str] = Field(default_factory=list)
    linked_gating_entity_ids: List[str] = Field(default_factory=list)
    linked_daily_report_id: Optional[str] = None
    # Phase 2 marks events created by the voice extractor — UI shows them
    # as a "voice draft" so the user can review before treating as final.
    voice_extracted: bool = False
    # Bucket D Corrective (2026-05-08) -- master-schedule task linkage.
    # OPTIONAL (some coordination events are project-wide). Coexists
    # with the legacy `linked_activity_ids` (kept for C3b back-compat
    # readers) which historically held master-task IDs as well; new
    # form code writes here, validators check this list.
    linked_task_ids: List[str] = Field(default_factory=list)
    is_supplement: bool = False
    parent_daily_report_id: Optional[str] = None
    supplement_reason: Optional[str] = None


class CoordinationEventUpdate(StrictModel):
    """All fields optional — patch what changed. project_id intentionally
    not patchable (would orphan the row across tenants/projects)."""
    occurred_at: Optional[datetime] = None
    channel: Optional[CoordinationChannel] = None
    direction: Optional[CoordinationDirection] = None
    our_party_user_id: Optional[str] = None
    other_party_name: Optional[str] = None
    other_party_org: Optional[str] = None
    other_party_role: Optional[str] = None
    other_party_email: Optional[str] = None
    other_party_phone: Optional[str] = None
    topic: Optional[CoordinationTopic] = None
    topic_other_description: Optional[str] = None
    summary: Optional[str] = None
    detail_text: Optional[str] = None
    outcome: Optional[CoordinationOutcome] = None
    outcome_notes: Optional[str] = None
    evidence_attachment_ids: Optional[List[str]] = None
    linked_rfi_ids: Optional[List[str]] = None
    linked_field_issue_ids: Optional[List[str]] = None
    linked_submittal_ids: Optional[List[str]] = None
    linked_bulletin_ids: Optional[List[str]] = None
    linked_activity_ids: Optional[List[str]] = None
    linked_gating_entity_ids: Optional[List[str]] = None
    linked_daily_report_id: Optional[str] = None
    voice_extracted: Optional[bool] = None
    # Bucket D Corrective -- re-pickable post-create.
    linked_task_ids: Optional[List[str]] = None
    # Addition 4 -- required when the parent DR is locked.
    edit_reason: Optional[str] = Field(default=None, max_length=500)


class CoordinationEvent(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    occurred_at: datetime
    channel: CoordinationChannel
    direction: CoordinationDirection
    our_party_user_id: Optional[str] = None
    other_party_name: str
    other_party_org: Optional[str] = None
    other_party_role: Optional[str] = None
    other_party_email: Optional[str] = None
    other_party_phone: Optional[str] = None
    topic: CoordinationTopic
    topic_other_description: Optional[str] = None
    summary: str
    detail_text: Optional[str] = None
    outcome: CoordinationOutcome = "informational"
    outcome_notes: Optional[str] = None
    evidence_attachment_ids: List[str] = Field(default_factory=list)
    linked_rfi_ids: List[str] = Field(default_factory=list)
    linked_field_issue_ids: List[str] = Field(default_factory=list)
    linked_submittal_ids: List[str] = Field(default_factory=list)
    linked_bulletin_ids: List[str] = Field(default_factory=list)
    linked_activity_ids: List[str] = Field(default_factory=list)
    linked_gating_entity_ids: List[str] = Field(default_factory=list)
    linked_daily_report_id: Optional[str] = None
    voice_extracted: bool = False
    created_at: datetime
    created_by_user_id: Optional[str] = None
    updated_at: Optional[datetime] = None
    # Soft-delete marker — list endpoints filter where this is None.
    deleted_at: Optional[datetime] = None
    deleted_by_user_id: Optional[str] = None
    # Bucket D Corrective -- master-schedule linkage + supplement metadata.
    linked_task_ids: List[str] = Field(default_factory=list)
    is_supplement: bool = False
    parent_daily_report_id: Optional[str] = None
    supplement_filed_at: Optional[datetime] = None
    supplement_filed_by: Optional[str] = None
    supplement_reason: Optional[str] = None
    # Bucket D Task 2 -- denormalized parent DR date so the per-row
    # supplement badge renders without a per-row fetch. Server-enriched
    # in /context bundle and /photos list (extend other lists if needed).
    parent_daily_report_date: Optional[str] = None


# ============================================================
# Bucket C3c — Schedule update diffs (version-to-version)
# ============================================================
class ScheduleUpdateDiff(ReadModel):
    """One row per pair of approved schedule versions. Auto-generated when
    a new schedule_proposal flips to status='approved' and a prior
    approved proposal exists for the same project. Stored as queryable
    JSON (not PDF) so the TIA generator can pull it later."""
    id: str
    tenant_id: str
    project_id: str
    earlier_schedule_version_id: str  # prior approved proposal id
    later_schedule_version_id: str    # new approved proposal id (the trigger)
    computed_at: datetime

    # Aggregate stats
    activities_added_count: int = 0
    activities_removed_count: int = 0
    activities_modified_count: int = 0
    predecessors_added_count: int = 0
    predecessors_removed_count: int = 0
    predecessors_modified_count: int = 0
    cp_changes_count: int = 0
    total_change_percentage: float = 0.0

    # Detailed payload (free-form by design — heterogeneous keys per row)
    activities_added: List[dict] = Field(default_factory=list)
    activities_removed: List[dict] = Field(default_factory=list)
    activities_modified: List[dict] = Field(default_factory=list)
    predecessor_changes: List[dict] = Field(default_factory=list)
    newly_critical_activities: List[str] = Field(default_factory=list)
    removed_critical_activities: List[str] = Field(default_factory=list)
    total_float_distribution_shift: dict = Field(default_factory=dict)

    # AI narrative — generated on demand via /ai-narrative endpoint
    ai_narrative: Optional[str] = None
    ai_narrative_generated_at: Optional[datetime] = None
    ai_narrative_generated_by: Optional[str] = None


# ============================================================
# Bucket C3c — Cascade snapshots (RFI-to-CPM projected slip)
# ============================================================
class CascadeSnapshot(ReadModel):
    """Snapshot of one open gating entity's projected schedule impact at
    multiple N-day horizons. New row per recompute; never overwrite —
    historical record is the point."""
    id: str
    tenant_id: str
    project_id: str
    computed_at: datetime
    gating_entity_id: str
    directly_gated_activity_ids: List[str] = Field(default_factory=list)
    transitively_gated_activity_ids: List[str] = Field(default_factory=list)
    projected_slip_at_n5_days: float = 0.0
    projected_slip_at_n10_days: float = 0.0
    projected_slip_at_n20_days: float = 0.0
    projected_cp_impact_at_n5_days: float = 0.0
    projected_cp_impact_at_n10_days: float = 0.0
    projected_cp_impact_at_n20_days: float = 0.0
    source_schedule_version_id: Optional[str] = None  # latest approved proposal id at compute time
    open_age_working_days: Optional[int] = None
    sla_status: Optional[str] = None  # under_sla | at_sla | over_sla
    severity_band: Optional[str] = None  # green | yellow | orange | red


# ============================================================
# Bucket C3c — TIA source document generations
# ============================================================
TIA_DEFAULT_PRIVILEGE_FLAG = True


class TIAGenerationCreate(StrictModel):
    project_id: str
    date_range_start: date
    date_range_end: date
    cause_class: Optional[str] = None
    zone_id: Optional[str] = None
    gating_entity_id: Optional[str] = None

    # Bucket C3c Phase 6 — guard against malformed ranges before the
    # service spends time gathering data. Pydantic v2 model_validator
    # runs after individual field validation; raise -> 422 from FastAPI.
    def model_post_init(self, __context) -> None:
        if self.date_range_end < self.date_range_start:
            raise ValueError("date_range_end must be >= date_range_start")
        if (self.date_range_end - self.date_range_start).days > 1095:
            raise ValueError("Date range cannot exceed 3 years")


class TIADownloadEntry(ReadModel):
    downloaded_at: datetime
    downloaded_by_user_id: Optional[str] = None
    format: str  # json | word | pdf
    privilege_state_at_download: bool


class TIAGeneration(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    generated_at: datetime
    generated_by_user_id: Optional[str] = None
    # Followup-fix 2 (2026-05-07): pending + running added for the
    # BackgroundTask path. Sync path still goes generating -> complete.
    status: str = "complete"  # pending | running | generating | complete | failed
    error: Optional[str] = None
    failure_reason: Optional[str] = None
    # Surfaces the cheap evidence-item pre-count the router used to
    # decide between sync and BG paths -- helps the frontend show
    # "generating large TIA (~N items)..." copy.
    estimated_evidence_count: Optional[int] = None

    date_range_start: date
    date_range_end: date
    filters_applied: dict = Field(default_factory=dict)

    structured_json_artifact_id: Optional[str] = None
    word_doc_artifact_id: Optional[str] = None
    evidence_index_pdf_artifact_id: Optional[str] = None

    delay_events_count: int = 0
    mitigations_count: int = 0
    coordination_events_count: int = 0
    photos_evidentiary_count: int = 0
    schedule_diffs_count: int = 0
    total_evidence_items: int = 0

    counsel_privileged_work_product: bool = TIA_DEFAULT_PRIVILEGE_FLAG
    privilege_unflagged_at: Optional[datetime] = None
    privilege_unflagged_by: Optional[str] = None
    privilege_unflag_reason: Optional[str] = None

    download_log: List[TIADownloadEntry] = Field(default_factory=list)


class TIAUnflagPayload(StrictModel):
    reason: str = Field(..., min_length=3, max_length=500)


# ============================================================
# Addition 4 -- Edit-in-place on locked DRs (with tiered permissions
# + edit_requests approval queue + per-event audit trail).
# ============================================================
EditableEventType = Literal[
    "delay", "rfi", "qc", "safety", "photo", "coordination",
    "delivery", "activity",
]
EditRequestStatus = Literal["pending", "approved", "denied", "expired"]

# Required min length for the user-supplied edit reason. The 10-char
# floor exists so legal sees substantive justification ("forgot to
# log discovery") rather than empty or single-letter strings.
EDIT_REASON_MIN_LEN = 10


class EditRequestCreate(StrictModel):
    """Filed by a super who needs edit access to an event on a locked
    DR they originally submitted, AFTER the 24-hour self-edit window
    has closed. Admin or PM reviews + approves/denies."""
    daily_report_id: str
    event_type: EditableEventType
    event_id: str
    reason: str = Field(..., min_length=EDIT_REASON_MIN_LEN, max_length=500)


class EditRequestReview(StrictModel):
    """Admin/PM review payload. review_note is optional on approve,
    encouraged on deny."""
    review_note: Optional[str] = Field(default=None, max_length=500)


class EditRequest(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    daily_report_id: str
    event_type: EditableEventType
    event_id: str
    requested_by: str
    requested_at: datetime
    reason: str
    status: EditRequestStatus = "pending"
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    review_note: Optional[str] = None
    # Set on approve: now + 1 hour. After this point the request is
    # treated as expired even if status hasn't been auto-flipped.
    edit_window_expires_at: Optional[datetime] = None


# ---------- Edit metadata mixin (additive defaults on every event row) ----------
# Schema additions on delays / rfis / qc_issues / safety_events / photos /
# coordination_events / deliveries / daily_report_activities. Defaults
# preserve legacy behavior (edit_count=0, post_submit_edited=false) so no
# migration is required -- only newly-edited rows ever set these fields.
#
# We don't bake these into every model class to avoid a 100-line diff;
# the read endpoints surface them via the underlying mongo doc and the
# ReadModel base ignores extras. The PATCH endpoints stamp them on
# write. For documentation, the shape is:
#
#   last_edited_at: datetime | null
#   last_edited_by: user_id | null
#   last_edit_reason: str | null
#   edit_count: int (default 0)
#   post_submit_edited: bool (default false)


class PostSubmitEditPayload(StrictModel):
    """The patch payload accepted by every event PATCH endpoint when
    the parent DR is locked. The shape is intentionally minimal to keep
    the audit chain readable; the caller posts whichever fields they
    want to change as part of `updates` and the server merges them into
    the underlying record after permission/reason checks."""
    edit_reason: str = Field(..., min_length=EDIT_REASON_MIN_LEN, max_length=500)
    updates: dict = Field(..., description="Field name -> new value")
