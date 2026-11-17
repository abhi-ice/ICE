"""Search chunkers for specs and company records ("docs_company" group).

Covers: spec sections, projects, the vendor directory (subcontractors and
suppliers), crews, equipment and locations. Every other collection in this
topic area is listed with a query_only or exclude decision and a reason.

Each chunker turns ONE source document into chunks (see
services.search.types). It never writes to the database and never calls the
embedding service. Rows that are deleted, soft-deleted or orphaned (their
parent project or spec is gone) return a ChunkResult with no chunks, so the
worker removes anything indexed for them earlier.

Who can see these records today (none of the read endpoints check roles, so
allowed_roles stays None on every chunk):
  * spec sections: any company user, GET /specs/{spec_id}/sections
    (routers/docs_router.py:754-760); project limited by the spec's project.
  * projects: any company user, GET /projects (routers/projects_router.py:175).
  * subcontractors / suppliers: any company user
    (routers/vendors_router.py:148, :172).
  * crews / equipment: any company user (routers/resources_router.py:61, :82).
  * locations: any company user with access to the project
    (routers/projects_router.py:518-525).
"""
from __future__ import annotations

import re
from typing import Any, Optional

from services.search.types import Chunk, ChunkContext, ChunkResult, Entry, clean, iso_date

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Contact details and links can be typed into free-text notes. Strip them
# before anything is embedded.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
# US phone numbers in any common form: (908) 555-0142, 908-555-0142,
# 908.555.0142, 908 555 0142, 9085550142, +1 908 555 0142. Spec numbers such
# as "08 11 13" or "ASTM E 119-12" have too few digits to match.
_PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\b\d{3})[\s.-]?\d{3}[\s.-]?\d{4}\b")


def scrub(text: Any) -> str:
    """Free text with emails, links and phone numbers removed, whitespace tidied."""
    if text is None:
        return ""
    s = str(text)
    # Hidden, not deleted: a marker tells the reader a value is on file.
    s = _EMAIL_RE.sub("[email on file, hidden from search]", s)
    s = _URL_RE.sub(" ", s)
    s = _PHONE_RE.sub("[phone on file, hidden from search]", s)
    return s


def _one_line(text: Any) -> str:
    return clean(scrub(text))


def _is_gone(doc: dict) -> bool:
    """True for deleted or soft-deleted rows (status 'deleted', a deleted_at
    stamp, or an is_deleted / deleted flag)."""
    if str(doc.get("status") or "").lower() == "deleted":
        return True
    return bool(doc.get("deleted_at") or doc.get("is_deleted") or doc.get("deleted") is True)


def _empty(doc: dict, tenant_id: Optional[str] = None, project_id: Optional[str] = None) -> ChunkResult:
    """'Index nothing for this row' (the worker then drops any old chunks)."""
    return ChunkResult(
        tenant_id=tenant_id if tenant_id is not None else doc.get("tenant_id"),
        project_id=project_id,
        source_id=doc.get("id"),
        chunks=[],
    )


async def _project(ctx: ChunkContext, project_id: Optional[str], tenant_id: Optional[str]) -> Optional[dict]:
    """The parent project row (name only), or None when it no longer exists
    or belongs to another company."""
    if not project_id or not tenant_id:
        return None
    return await ctx.one("projects", {"id": project_id, "tenant_id": tenant_id}, {"name": 1, "number": 1})


def _project_label(p: Optional[dict]) -> str:
    if not p:
        return ""
    return clean(p.get("name") or p.get("number") or "")


def _str_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return [clean(x) for x in v if clean(x)]
    s = clean(v)
    return [s] if s else []


def _header(*parts: Any) -> str:
    return " | ".join(p for p in (clean(x) for x in parts) if p)


COMPANY = "Company directory"  # header label for records that belong to no project


# ---------------------------------------------------------------------------
# spec_sections
# ---------------------------------------------------------------------------

# Long sections are split into pieces of about 400 tokens (about 1,600
# characters at roughly 4 characters per token), with about 15% overlap when
# a piece has to be cut in the middle of an article.
SPEC_PIECE_CHARS = 1600
SPEC_OVERLAP_CHARS = 240

# "PART 1 - GENERAL" or an article heading such as "1.4 SUBMITTALS".
_PART_RE = re.compile(r"^\s*PART\s+\d+\b.*$", re.IGNORECASE)
_ARTICLE_RE = re.compile(r"^\s*\d{1,2}\.\d{1,2}\s+[A-Z][A-Z0-9 ,&/()'\-.]{2,}$")


def _split_long_line(line: str, limit: int) -> list[str]:
    """Cut one very long line into word-aligned parts no longer than limit."""
    out, cur = [], ""
    for word in line.split():
        if cur and len(cur) + 1 + len(word) > limit:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}" if cur else word
    if cur:
        out.append(cur)
    return out


def _tail(lines: list[str], limit: int) -> list[str]:
    """The last lines of a piece, up to about `limit` characters, used as
    overlap at the start of the next piece."""
    out: list[str] = []
    total = 0
    for line in reversed(lines):
        if total + len(line) > limit:
            if not out:  # one long line: keep its trailing words
                words, part = line.split(), []
                for w in reversed(words):
                    if sum(len(x) + 1 for x in part) + len(w) > limit:
                        break
                    part.insert(0, w)
                if part:
                    out.insert(0, " ".join(part))
            break
        out.insert(0, line)
        total += len(line) + 1
    return out


def split_spec_text(text: str, piece_chars: int = SPEC_PIECE_CHARS,
                    overlap_chars: int = SPEC_OVERLAP_CHARS) -> list[dict]:
    """Split spec text into pieces, preferring PART / article headings as
    break points. Returns [{"text", "part", "article"}] in reading order;
    "part" / "article" are the headings in force where the piece starts."""
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        if len(line) > piece_chars:
            lines.extend(_split_long_line(line, piece_chars - overlap_chars))
        else:
            lines.append(line)
    if not lines:
        return []

    pieces: list[dict] = []
    cur: list[str] = []
    cur_len = 0
    new_in_cur = 0          # lines in `cur` that are not overlap from the last piece
    part = article = ""
    start_part = start_article = ""

    def close():
        nonlocal cur, cur_len, new_in_cur
        if cur and new_in_cur:
            pieces.append({"text": "\n".join(cur), "part": start_part, "article": start_article})
        cur, cur_len, new_in_cur = [], 0, 0

    for line in lines:
        is_part = bool(_PART_RE.match(line))
        is_heading = is_part or bool(_ARTICLE_RE.match(line))
        # Break at a heading once the piece already has a fair amount of text.
        if is_heading and cur_len >= piece_chars * 0.4:
            close()
        # Break in the middle of an article when the piece is full; carry an
        # overlap so a sentence cut at the edge still reads in context.
        elif cur and cur_len + len(line) + 1 > piece_chars:
            carry = _tail(cur, overlap_chars)
            close()
            cur = list(carry)
            cur_len = sum(len(x) + 1 for x in cur)
        if is_part:
            part, article = line, ""
        elif is_heading:
            article = line
        if new_in_cur == 0:
            start_part, start_article = part, article
        cur.append(line)
        cur_len += len(line) + 1
        new_in_cur += 1
    close()
    return pieces


async def chunk_spec_section(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    tenant_id = doc.get("tenant_id")
    spec_id = doc.get("project_spec_id")
    if not tenant_id or not spec_id or _is_gone(doc):
        return _empty(doc)
    # Sections carry no project_id; it comes from the parent spec, the same
    # way routers/iter5_router.py:763-779 (_spec_section_scope) does it.
    spec = await ctx.one("project_specs", {"id": spec_id, "tenant_id": tenant_id},
                         {"name": 1, "revision_label": 1, "project_id": 1})
    if not spec or not spec.get("project_id"):
        return _empty(doc)
    project_id = spec["project_id"]
    proj = await _project(ctx, project_id, tenant_id)
    if not proj:  # project deleted; its specs are not in the delete cascade
        return _empty(doc, project_id=project_id)

    code = clean(doc.get("section_code"))
    title = clean(doc.get("section_title"))
    text = doc.get("text_content") or ""
    if not clean(text) and not title:
        return _empty(doc, project_id=project_id)

    division = clean(doc.get("division"))
    div_name = ""
    if division:
        tax = await ctx.one("trade_taxonomy", {"code": division, "tenant_id": None}, {"name": 1})
        div_name = clean((tax or {}).get("name"))
    spec_label = clean(spec.get("name"), spec.get("revision_label"))
    p0, p1 = doc.get("page_start"), doc.get("page_end")
    pages = ""
    if p0 is not None:
        pages = f"pages {p0}-{p1}" if p1 is not None and p1 != p0 else f"page {p0}"

    pieces = split_spec_text(scrub(text)) or [{"text": title, "part": "", "article": ""}]
    sec_label = clean("Spec", code, title)
    date = iso_date(doc.get("created_at"))
    chunks = []
    for piece in pieces:
        where = piece["article"] or piece["part"]
        # No date in the header: the only date on a section is when it was
        # uploaded, which says nothing about its content.
        header = _header(
            _project_label(proj),
            sec_label,
            clean("Division", division, div_name) if division else "",
            clean(spec_label, pages),
            where,
        )
        chunks.append(Chunk(
            kind="spec",
            header=header,
            body=piece["text"],
            date=date,
            trade_code=[division] if division else [],
            parent_id=spec_id,
            title=f"{sec_label} ({where})" if where else sec_label,
            url=f"/specs?openId={spec_id}&sectionId={doc.get('id')}",
        ))
    return ChunkResult(tenant_id=tenant_id, project_id=project_id, source_id=doc.get("id"), chunks=chunks)


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------

async def chunk_project(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    tenant_id = doc.get("tenant_id")
    pid = doc.get("id")
    if not tenant_id or not pid or _is_gone(doc):
        return _empty(doc, project_id=pid)
    name = _one_line(doc.get("name") or doc.get("number"))
    if not name:
        return _empty(doc, project_id=pid)

    contract = _one_line(doc.get("contract_number"))
    status = clean(doc.get("status")) or None
    ntp = iso_date(doc.get("ntp_date"))
    start = iso_date(doc.get("start_date"))
    created = iso_date(doc.get("created_at"))
    date = ntp or start or created
    when = f"NTP {ntp}" if ntp else (f"start {start}" if start else (f"created {created}" if created else ""))

    address = ", ".join(x for x in (
        _one_line(doc.get("address_line1")), _one_line(doc.get("city")),
        clean(_one_line(doc.get("state")), _one_line(doc.get("postal_code"))),
    ) if x)
    sc = iso_date(doc.get("current_substantial_completion")) or iso_date(doc.get("planned_completion_date"))
    lines = [
        f"Project {name}",
        f"Contract number: {contract}" if contract else "",
        f"Client: {_one_line(doc.get('client_name'))}" if _one_line(doc.get("client_name")) else "",
        f"Owner: {_one_line(doc.get('owner_name'))}" if _one_line(doc.get("owner_name")) else "",
        f"Contractor: {_one_line(doc.get('contractor_name'))}" if _one_line(doc.get("contractor_name")) else "",
        f"Agency: {_one_line(doc.get('agency'))}" if _one_line(doc.get("agency")) else "",
        f"Station: {_one_line(doc.get('station_id'))}" if _one_line(doc.get("station_id")) else "",
        f"Project type: {_one_line(doc.get('project_type'))}" if _one_line(doc.get("project_type")) else "",
        f"Contract type: {_one_line(doc.get('contract_type'))}" if _one_line(doc.get("contract_type")) else "",
        f"Address: {address}" if address else "",
        f"Notice to proceed: {ntp}" if ntp else "",
        f"Substantial completion: {sc}" if sc else "",
    ]
    # Never contract_value_usd, file pointers (dr_template_file_url,
    # ms_project_file_*) or schedule forecast numbers.
    body = "\n".join(x for x in lines if x)
    chunk = Chunk(
        kind="project",
        header=_header(name, clean("Project", f"contract {contract}" if contract else "", when),
                       f"status {status}" if status else ""),
        body=body,
        date=date,
        status=status,
        title=f"Project {name}",
        url=f"/projects?openId={pid}",
    )
    # A rename changes the header of every record in the project. Refresh the
    # small company-group records that show the project name; other groups'
    # records are refreshed by the nightly reconcile.
    requeue = [
        ("locations", {"tenant_id": tenant_id, "project_id": pid}),
        ("crews", {"tenant_id": tenant_id, "home_project_id": pid}),
    ]
    return ChunkResult(tenant_id=tenant_id, project_id=pid, source_id=pid, chunks=[chunk], requeue=requeue)


# ---------------------------------------------------------------------------
# subcontractors / suppliers (company vendor directory)
# ---------------------------------------------------------------------------

def _vendor_chunker(label: str, tab: str):
    async def chunk_vendor(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
        tenant_id = doc.get("tenant_id")
        if not tenant_id or not doc.get("id") or _is_gone(doc):
            return _empty(doc)
        name = _one_line(doc.get("name"))
        if not name:
            return _empty(doc)
        trades = [_one_line(t) for t in _str_list(doc.get("trades")) if _one_line(t)]
        contact = _one_line(doc.get("contact_name"))
        notes = _one_line(doc.get("notes"))
        # Never contact_email or contact_phone.
        body = "\n".join(x for x in (
            f"{label} {name}",
            f"Trades: {', '.join(trades)}" if trades else "",
            f"Contact: {contact}" if contact else "",
            f"Notes: {notes}" if notes else "",
        ) if x)
        chunk = Chunk(
            kind="vendor",
            header=_header(COMPANY, f"{label} {name}", f"trades {', '.join(trades)}" if trades else ""),
            body=body,
            date=iso_date(doc.get("created_at")),
            trade_code=trades,
            title=f"{label} {name}",
            url=f"/vendors?tab={tab}&openId={doc.get('id')}",
        )
        return ChunkResult(tenant_id=tenant_id, project_id=None, source_id=doc.get("id"), chunks=[chunk])
    chunk_vendor.__name__ = f"chunk_{tab}"
    return chunk_vendor


chunk_subcontractor = _vendor_chunker("Subcontractor", "subs")
chunk_supplier = _vendor_chunker("Supplier", "suppliers")


# ---------------------------------------------------------------------------
# crews
# ---------------------------------------------------------------------------

async def chunk_crew(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    tenant_id = doc.get("tenant_id")
    if not tenant_id or not doc.get("id") or _is_gone(doc):
        return _empty(doc)
    name = _one_line(doc.get("name"))
    code = _one_line(doc.get("crew_code"))
    if not name and not code:
        return _empty(doc)
    # Crews are company records: GET /crews (routers/resources_router.py:61)
    # lists every crew to every company user, and the assistant treats them as
    # company-level. So the chunk is never scoped to a project; the home
    # project's name only appears in the text. (Scoping it to the home project
    # would hide the crew for good once that project is hard-deleted, because
    # nothing re-runs this chunker after a project delete.)
    proj = await _project(ctx, doc.get("home_project_id"), tenant_id)
    trade = _one_line(doc.get("trade"))
    company = _one_line(doc.get("company_name"))
    notes = _one_line(doc.get("notes"))
    status = clean(doc.get("status")) or None
    headcount = doc.get("default_headcount")
    label = clean(f"Crew {name or code}", f"({code})" if code and name else "")
    # Never hourly_burdened_cost, home_yard_address or the foreman's personal record.
    body = "\n".join(x for x in (
        label,
        f"Trade: {trade}" if trade else "",
        f"Company: {company}" if company else "",
        f"Default headcount: {headcount}" if isinstance(headcount, int) and not isinstance(headcount, bool) else "",
        f"Home project: {_project_label(proj)}" if proj else "",
        f"Notes: {notes}" if notes else "",
    ) if x)
    chunk = Chunk(
        kind="crew",
        header=_header(_project_label(proj) or COMPANY, label,
                       clean(f"trade {trade}" if trade else "", f"status {status}" if status else "")),
        body=body,
        date=iso_date(doc.get("created_at")),
        trade_code=[trade] if trade else [],
        status=status,
        title=label,
        url=f"/resources/crews?openId={doc.get('id')}",
    )
    return ChunkResult(tenant_id=tenant_id, project_id=None, source_id=doc.get("id"), chunks=[chunk])


# ---------------------------------------------------------------------------
# equipment
# ---------------------------------------------------------------------------

async def chunk_equipment(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    tenant_id = doc.get("tenant_id")
    if not tenant_id or not doc.get("id") or _is_gone(doc):
        return _empty(doc)
    tag = _one_line(doc.get("asset_tag"))
    etype = _one_line(doc.get("type"))
    if not tag and not etype:
        return _empty(doc)
    make = clean(_one_line(doc.get("manufacturer")), _one_line(doc.get("model")),
                 doc.get("year") if isinstance(doc.get("year"), int) else "")
    ownership = _one_line(doc.get("ownership"))
    notes = _one_line(doc.get("notes"))
    status = clean(doc.get("status")) or None
    label = clean("Equipment", tag, etype)
    # Never hourly_rate, daily_rate or monthly_rate.
    body = "\n".join(x for x in (
        label,
        f"Make and model: {make}" if make else "",
        f"Ownership: {ownership}" if ownership else "",
        f"Notes: {notes}" if notes else "",
    ) if x)
    chunk = Chunk(
        kind="equipment",
        header=_header(COMPANY, label, f"status {status}" if status else ""),
        body=body,
        date=iso_date(doc.get("created_at")),
        status=status,
        title=label,
        url=f"/resources/equipment?openId={doc.get('id')}",
    )
    return ChunkResult(tenant_id=tenant_id, project_id=None, source_id=doc.get("id"), chunks=[chunk])


# ---------------------------------------------------------------------------
# locations
# ---------------------------------------------------------------------------

async def chunk_location(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    tenant_id = doc.get("tenant_id")
    project_id = doc.get("project_id")
    # The assistant's delete tool soft-deletes with status 'deleted'
    # (services/copilot_tools.py:1380) even though the model has no status.
    if not tenant_id or not doc.get("id") or _is_gone(doc):
        return _empty(doc, project_id=project_id)
    proj = await _project(ctx, project_id, tenant_id)
    if not proj:
        return _empty(doc, project_id=project_id)
    name = _one_line(doc.get("name"))
    code = _one_line(doc.get("code"))
    if not name and not code:
        return _empty(doc, project_id=project_id)
    floor_name = ""
    if doc.get("floor_id"):
        fl = await ctx.one("floors", {"id": doc["floor_id"], "tenant_id": tenant_id},
                           {"floor_name": 1, "floor_number": 1})
        if fl:
            floor_name = _one_line(fl.get("floor_name")) or (
                f"Floor {fl['floor_number']}" if fl.get("floor_number") is not None else "")
    ltype = _one_line(doc.get("location_type"))
    label = clean(f"Location {name or code}", f"({code})" if code and name else "")
    # Never polygon_geojson or the centroid coordinates.
    body = "\n".join(x for x in (
        label,
        f"Type: {ltype}" if ltype else "",
        f"Floor: {floor_name}" if floor_name else "",
    ) if x)
    chunk = Chunk(
        kind="location",
        header=_header(_project_label(proj), label, clean(ltype, f"floor {floor_name}" if floor_name else "")),
        body=body,
        date=iso_date(doc.get("created_at")),
        # No parent_id: deleting a floor must not wipe the chunks of locations
        # that still exist (the worker deletes children by parent_id).
        parent_id=None,
        title=label,
        url=f"/locations?openId={doc.get('id')}",
    )
    return ChunkResult(tenant_id=tenant_id, project_id=project_id, source_id=doc.get("id"), chunks=[chunk])


# ---------------------------------------------------------------------------
# floors (no chunks of their own)
# ---------------------------------------------------------------------------

async def chunk_floor(ctx: ChunkContext, doc: dict) -> Optional[ChunkResult]:
    """A floor has no chunk of its own, but its name is shown on every
    location on it. When a floor changes (for example a rename through
    PATCH /crud/floors), ask for its locations to be re-indexed."""
    tenant_id = doc.get("tenant_id")
    floor_id = doc.get("id")
    requeue = []
    if tenant_id and floor_id:
        requeue.append(("locations", {"tenant_id": tenant_id, "floor_id": floor_id}))
    return ChunkResult(tenant_id=tenant_id, project_id=doc.get("project_id"),
                       source_id=floor_id, chunks=[], requeue=requeue)


# ---------------------------------------------------------------------------
# Registry entries for this group
# ---------------------------------------------------------------------------

ENTRIES: list[Entry] = [
    # --- indexed ---
    Entry("spec_sections", "index",
          "Full specification text that people search by topic; split at PART / article headings "
          "into pieces of about 400 tokens. Project comes from the parent spec.",
          chunk_spec_section),
    Entry("projects", "index",
          "People search projects by name, contract number, client, owner, agency and address. "
          "Contract value and file pointers stay out.",
          chunk_project),
    Entry("subcontractors", "index",
          "Company vendor directory with names, trades and notes (for example who does abatement). "
          "Contact email and phone stay out.",
          chunk_subcontractor),
    Entry("suppliers", "index",
          "Company supplier directory with names, material trades and notes. "
          "Contact email and phone stay out.",
          chunk_supplier),
    Entry("crews", "index",
          "Crew names, codes, trades and notes, so search can answer which crew does what. "
          "The burdened cost stays out.",
          chunk_crew),
    Entry("equipment", "index",
          "Asset register (tag, type, make, model) that people look up by name. Rates stay out.",
          chunk_equipment),
    Entry("locations", "index",
          "Room and area names and codes per project, so search can answer 'where' questions.",
          chunk_location),
    Entry("floors", "index",
          "No chunks of its own; a floor change re-indexes the locations on that floor so "
          "their floor name stays current.",
          chunk_floor),

    # --- read only (no chunks) ---
    Entry("project_specs", "query_only",
          "Spec name, revision and processing status only; read when building spec section "
          "headers and to find each section's project. The text lives in spec_sections."),
    Entry("cost_rate_categories", "query_only",
          "Company rate table looked up by code or trade; mostly numbers, and the rates are "
          "commercially sensitive, so it is never embedded."),
    Entry("trade_taxonomy", "query_only",
          "Fixed list of construction divisions, used to name a spec section's division; "
          "not a search result on its own."),
    Entry("files", "query_only",
          "Storage register for uploaded files (no readable content, only storage paths); the "
          "records the files belong to are indexed through their own collections."),
    Entry("project_members", "query_only",
          "Who may see which project. Read by the search permission check, never embedded."),
    Entry("tenant_users", "query_only",
          "Company membership and roles. Read by the search permission check, never embedded."),
    Entry("users", "query_only",
          "Holds password hashes, emails and reset tokens. Only a person's display name is read "
          "when building a header; the collection itself is never embedded."),
    Entry("tenants", "query_only",
          "Company settings and AI budget counters. Only read for the company name and AI "
          "setting; never embedded."),

    # --- excluded ---
    Entry("crew_capacity", "exclude",
          "Computed per-day headcount numbers with no human text, rebuilt in bulk by the "
          "resource-leveling recompute."),
    Entry("crew_demands", "exclude",
          "Computed per-day demand numbers, deleted and rebuilt in bulk on every recompute."),
    Entry("productivity_observations", "exclude",
          "System-made numbers (hours, percent change) for each end-of-day update; the daily "
          "report activity they come from is indexed elsewhere."),
    Entry("people", "exclude",
          "Personal data: names, email, phone, pay rate and work-authorization status."),
    Entry("certifications", "exclude",
          "Personal data tied to individual workers."),
    Entry("crew_assignments", "exclude",
          "Join rows linking crews to activities and dates; no text of their own."),
    Entry("equipment_assignments", "exclude",
          "Join rows linking equipment to activities and dates; no text of their own."),
    Entry("calendars", "exclude",
          "Working-day calendars (numbers and dates), used by the scheduling engines."),
]
