"""Pydantic models — StrictModel pattern (extra='forbid') everywhere.

Per master prompt section 3:
> All schemas use model_config = ConfigDict(extra='forbid'). This caught a
> real production bug in the Railway codebase (prev_pct slipping into
> TaskCreate when it shouldn't have).
"""
from __future__ import annotations
from datetime import datetime, date, timezone
from typing import Optional, List, Literal, Any
from pydantic import BaseModel, ConfigDict, Field, EmailStr
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


class ProjectUpdate(StrictModel):
    name: Optional[str] = None
    status: Optional[ProjectStatus] = None
    contract_value_usd: Optional[float] = None
    planned_completion_date: Optional[date] = None
    actual_completion_date: Optional[date] = None


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


# ---------- Daily reports ----------
class DailyReportCreate(StrictModel):
    project_id: str
    report_date: date
    weather_temp_high_f: Optional[float] = None
    weather_temp_low_f: Optional[float] = None
    weather_conditions: Optional[str] = None
    weather_precipitation_in: Optional[float] = None
    notes: Optional[str] = None


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


class DailyReportActivityUpdate(StrictModel):
    pct_complete_end_of_day: Optional[float] = Field(default=None, ge=0, le=100)
    crew_count: Optional[int] = None
    crew_id: Optional[str] = None
    hours_worked: Optional[float] = None
    delay_minutes: Optional[int] = None
    delay_reason: Optional[str] = None
    weather_impact_minutes: Optional[int] = None
    notes: Optional[str] = None
    status_at_end_of_day: Optional[TaskStatus] = None


class DailyReportActivity(ReadModel):
    id: str
    daily_report_id: str
    schedule_activity_id: str
    position: int
    pct_complete_start_of_day: float = 0
    pct_complete_end_of_day: float = 0
    crew_count: int = 0
    crew_id: Optional[str] = None
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


class DailyPresenceCreate(StrictModel):
    daily_report_id: str
    trade: str
    sub_id: Optional[str] = None  # crew_id or external sub identifier
    company_name: Optional[str] = None
    headcount: int = 0
    hours_per_person: float = 8.0
    notes: Optional[str] = None


class DailyPresenceUpdate(StrictModel):
    trade: Optional[str] = None
    sub_id: Optional[str] = None
    company_name: Optional[str] = None
    headcount: Optional[int] = None
    hours_per_person: Optional[float] = None
    notes: Optional[str] = None


class DailyPresence(ReadModel):
    id: str
    tenant_id: str
    daily_report_id: str
    trade: str
    sub_id: Optional[str] = None
    company_name: Optional[str] = None
    headcount: int = 0
    hours_per_person: float = 8.0
    notes: Optional[str] = None
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
    project_id: str
    subject: str
    description: str
    to_party: Optional[str] = None
    from_party: Optional[str] = None
    schedule_activity_id: Optional[str] = None
    location_id: Optional[str] = None
    daily_report_id: Optional[str] = None
    spec_section_refs: List[str] = Field(default_factory=list)
    drawing_refs: List[dict] = Field(default_factory=list)
    date_required: Optional[date] = None
    priority: Priority = "normal"


class RfiUpdate(StrictModel):
    subject: Optional[str] = None
    description: Optional[str] = None
    response_text: Optional[str] = None
    status: Optional[RfiStatus] = None
    date_responded: Optional[date] = None
    cost_impact_estimated_usd: Optional[float] = None
    time_impact_estimated_days: Optional[int] = None


class Rfi(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    rfi_number: str
    subject: str
    description: str
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


class DelayCreate(StrictModel):
    project_id: str
    schedule_activity_id: Optional[str] = None
    location_id: Optional[str] = None
    daily_report_id: Optional[str] = None
    date: date
    description: str
    severity: Severity = "minor"
    photo_urls: List[str] = Field(default_factory=list)
    delay_days: Optional[float] = None
    reason_category: Optional[str] = None


class Delay(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    schedule_activity_id: Optional[str] = None
    location_id: Optional[str] = None
    daily_report_id: Optional[str] = None
    date: date
    description: str
    severity: Severity
    photo_urls: List[str] = Field(default_factory=list)
    delay_days: Optional[float] = None
    reason_category: Optional[str] = None
    status: str = "open"
    reported_by: Optional[str] = None
    reported_at: datetime
    created_at: datetime


class SafetyEventCreate(StrictModel):
    project_id: str
    schedule_activity_id: Optional[str] = None
    location_id: Optional[str] = None
    daily_report_id: Optional[str] = None
    date: date
    description: str
    severity: Severity = "minor"
    event_type: str = "near_miss"
    photo_urls: List[str] = Field(default_factory=list)
    osha_recordable: bool = False


class SafetyEvent(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    schedule_activity_id: Optional[str] = None
    location_id: Optional[str] = None
    daily_report_id: Optional[str] = None
    date: date
    description: str
    severity: Severity
    event_type: str
    photo_urls: List[str] = Field(default_factory=list)
    osha_recordable: bool = False
    status: str = "open"
    reported_by: Optional[str] = None
    reported_at: datetime
    created_at: datetime


class QcIssueCreate(StrictModel):
    project_id: str
    schedule_activity_id: Optional[str] = None
    location_id: Optional[str] = None
    daily_report_id: Optional[str] = None
    date: date
    description: str
    severity: Severity = "minor"
    spec_section_ref: Optional[str] = None
    photo_urls: List[str] = Field(default_factory=list)


class QcIssue(ReadModel):
    id: str
    tenant_id: str
    project_id: str
    schedule_activity_id: Optional[str] = None
    location_id: Optional[str] = None
    daily_report_id: Optional[str] = None
    date: date
    description: str
    severity: Severity
    spec_section_ref: Optional[str] = None
    photo_urls: List[str] = Field(default_factory=list)
    status: str = "open"
    reported_by: Optional[str] = None
    reported_at: datetime
    resolved_at: Optional[datetime] = None
    resolution_notes: Optional[str] = None
    created_at: datetime


# ---------- Photos ----------
class PhotoCreate(StrictModel):
    project_id: str
    location_id: Optional[str] = None
    schedule_activity_id: Optional[str] = None
    daily_report_id: Optional[str] = None
    storage_path: str
    caption: Optional[str] = None
    tags: List[str] = Field(default_factory=list)


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
