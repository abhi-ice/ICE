"""Guards for the "Search everything" collection registry.

The registry (services.search.registry.get_registry) decides, for every
MongoDB collection the app uses, whether search indexes it ("index"), only
reads it at question time ("query_only") or ignores it ("exclude"). These
tests keep that list honest. No database is needed.

1. Every collection name the backend code touches must be classified in the
   registry, so a new collection can never slip past search unreviewed.
2. Indexed entries have a chunker; the others do not; every entry says why.
3. Secret and system collections are never indexed.
4. No chunker puts emails, phone numbers, addresses, rates, money amounts,
   GPS coordinates, storage keys / links, tokens, password hashes or worker
   names into the text, title or link of a chunk. Every indexed collection
   must make at least one chunk from the test row (or be a declared
   roll-up whose parent chunk is checked instead), so no chunker escapes.
5. Deleted, soft-deleted and orphan rows make no chunks.
6. A lookup never pulls another company's record into a chunk.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import io
import pathlib
import re
import tokenize
from typing import Optional

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Part 1: find every collection name the code uses
# ---------------------------------------------------------------------------

# db.<name>, self.db.<name>, _db.<name>, get_db().<name>
_ATTR_RE = re.compile(r"(?:\b_?db|\bget_db\(\))\.([A-Za-z_][A-Za-z0-9_]*)")
# db["<name>"] / db['<name>'] (same prefixes)
_ITEM_RE = re.compile(r"(?:\b_?db|\bget_db\(\))\[\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\s*\]")
_COLLECTION_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Names the scanner picks up that are NOT collections we must classify.
IGNORED_NAMES = {
    # --- database methods and attributes, not collections ---
    "command",                # db.command(...) runs a server command
    "list_collection_names",  # lists collections, used by backup / restore
    "create_collection",      # creates a collection (restore script)
    "drop_collection",        # drops a collection (one migration)
    "get_collection",         # returns a collection object by name
    "with_options",           # returns the database with other read/write options
    "watch",                  # opens a change stream on the whole database
    "client",                 # the MongoClient behind the database
    "name",                   # the database's own name (backup manifest)
    "aggregate",              # database-level aggregation
    "codec_options",          # driver setting, not a collection
    "read_preference",        # driver setting, not a collection
    "write_concern",          # driver setting, not a collection
    "read_concern",           # driver setting, not a collection
    "dereference",            # resolves a DBRef
    "validate_collection",    # server-side collection check
    "cursor_command",         # like command, returns a cursor
    # --- ghost names: listed in cascade / wipe constants but no code reads or writes them ---
    "resources",              # cascade lists only; real data lives in crews / equipment / people
    "specs",                  # cascade lists + one onboarding-checklist read (iter8_router); never written, real data is spec_sections / project_specs
    "schedule_imports",       # cascade lists only; schedule imports live in imports / schedule_proposals
    "schedule_revisions",     # cascade lists only; never written anywhere
    "marketplace_listings",   # cascade lists only; the real collection is marketplace_postings
    "memberships",            # tenant-close cascade only; membership data is tenant_users / project_members
    "schedule_activities",    # migration 006 "preserve" list only; activities live in tasks
    "task_predecessors",      # migration 006 "preserve" list only; predecessors live on tasks
    "tenant_settings",        # migration 006 "preserve" list only; never written anywhere
    "project_settings",       # migration 006 "preserve" list only; never written anywhere
    "roles",                  # migration 006 "preserve" list only; roles are fields on tenant_users
    "permissions",            # migration 006 "preserve" list only; never written anywhere
    "polygons",               # migration 006 "preserve" list only; never written anywhere
}


def _strip_comments(source: str) -> str:
    """Blank out comments so 'db.X' written in prose is not counted.
    Falls back to the raw text if the file does not tokenize."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source
    lines = source.splitlines(keepends=True)
    for tok in reversed(tokens):
        if tok.type == tokenize.COMMENT:
            (row, col), (_, end_col) = tok.start, tok.end
            line = lines[row - 1]
            lines[row - 1] = line[:col] + " " * (end_col - col) + line[end_col:]
    return "".join(lines)


def _strings_in(node: ast.AST) -> list[str]:
    """Collection-looking strings in a constant's value: list / tuple / set
    items (first item of a nested tuple), dict values, or a plain string."""
    out: list[str] = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        out.append(node.value)
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for el in node.elts:
            if isinstance(el, ast.Tuple) and el.elts:
                out.extend(_strings_in(el.elts[0]))
            else:
                out.extend(_strings_in(el))
    elif isinstance(node, ast.Dict):
        for v in node.values:
            out.extend(_strings_in(v))
    return [s for s in out if _COLLECTION_NAME_RE.match(s)]


def _constant_collection_names(tree: ast.Module) -> list[str]:
    """Module-level constants whose name mentions COLLECTION, e.g. the
    project / tenant delete cascades (PROJECT_SCOPED_COLLECTIONS,
    TENANT_SCOPED_COLLECTIONS) or HEADS_COLLECTION = "audit_chain_heads".
    Those names are used through db[coll] in a loop, which the regexes miss."""
    names: list[str] = []
    for node in tree.body:
        target, value = None, None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            target, value = node.target.id, node.value
        if target and "COLLECTION" in target.upper():
            names.extend(_strings_in(value))
    return names


def _iter_source_files():
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(BACKEND).as_posix()
        parts = rel.split("/")
        if parts[0] == "tests" or "__pycache__" in parts:
            continue
        if any(p in ("venv", ".venv", "site-packages", "node_modules") for p in parts):
            continue
        yield rel, path


def scan_collection_names() -> dict[str, set[str]]:
    """Return {collection name: {files that use it}} for the backend code
    (tests excluded, migrations and scripts included)."""
    found: dict[str, set[str]] = {}
    for rel, path in _iter_source_files():
        source = path.read_text(encoding="utf-8", errors="replace")
        code = _strip_comments(source)
        names = [m.group(1) for m in _ATTR_RE.finditer(code)]
        names += [m.group(1) for m in _ITEM_RE.finditer(code)]
        try:
            names += _constant_collection_names(ast.parse(source))
        except SyntaxError:
            pass
        for n in names:
            found.setdefault(n, set()).add(rel)
    return found


def _search_own_collections(found: dict[str, set[str]]) -> set[str]:
    """Collections used only by the search system itself (its chunk store,
    its queue). They are search's storage, not source data, so they need no
    registry entry."""
    return {
        name for name, files in found.items()
        if files and all(f.startswith("services/search/") for f in files)
    }


def test_scanner_finds_known_collections():
    """Sanity check that the scanner itself works (no registry needed)."""
    found = scan_collection_names()
    for expected in ("daily_reports", "daily_report_activities", "projects", "tasks",
                     "rfis", "audit_chain_heads", "users"):
        assert expected in found, f"scanner missed {expected!r}"
    # Comments are ignored: models.py mentions "db.X" only in a comment.
    assert "X" not in found


# ---------------------------------------------------------------------------
# Registry loading
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def registry():
    try:
        from services.search.registry import get_registry
    except ImportError as exc:  # pragma: no cover - only before the module exists
        pytest.fail(f"services.search.registry.get_registry is missing: {exc}")
    reg = get_registry()
    assert isinstance(reg, dict) and reg, "get_registry() must return a non-empty dict"
    return reg


def test_every_used_collection_is_classified(registry):
    found = scan_collection_names()
    skip = IGNORED_NAMES | _search_own_collections(found)
    missing = sorted(n for n in found if n not in registry and n not in skip)
    if missing:
        lines = [f"  - {n}  (used in: {', '.join(sorted(found[n])[:4])})" for n in missing]
        pytest.fail(
            "These collections are used by the backend but are not classified in "
            "services/search/registry.py. Add an Entry for each one (index, query_only "
            "or exclude, with a reason). If a name is not really a collection, add it "
            "to IGNORED_NAMES in this test with a one-line reason.\n" + "\n".join(lines)
        )


def test_ignored_names_are_not_registered_as_real(registry):
    """A name we call 'not a collection' should not also be indexed."""
    clash = sorted(n for n in IGNORED_NAMES if n in registry and registry[n].decision == "index")
    assert not clash, f"IGNORED_NAMES are indexed in the registry: {clash}"


# ---------------------------------------------------------------------------
# Part 2: entry shape
# ---------------------------------------------------------------------------

def test_entries_are_well_formed(registry):
    from services.search.types import Entry

    problems = []
    for key, entry in registry.items():
        if not isinstance(entry, Entry):
            problems.append(f"{key}: not an Entry ({type(entry).__name__})")
            continue
        if entry.collection != key:
            problems.append(f"{key}: Entry.collection is {entry.collection!r}")
        if entry.decision not in ("index", "query_only", "exclude"):
            problems.append(f"{key}: unknown decision {entry.decision!r}")
        if not (entry.reason or "").strip():
            problems.append(f"{key}: empty reason")
        if entry.decision == "index" and not callable(entry.chunker):
            problems.append(f"{key}: indexed but has no chunker")
        if entry.decision != "index" and entry.chunker is not None:
            problems.append(f"{key}: {entry.decision} but has a chunker")
    assert not problems, "Registry problems:\n" + "\n".join(problems)


# ---------------------------------------------------------------------------
# Part 3: secrets / system collections are never indexed
# ---------------------------------------------------------------------------

NEVER_INDEXED = (
    "runtime_settings",       # plaintext worker token, flag history
    "team_invites",           # redeemable invite codes and emails
    "audit_log",              # old rows hold secrets in before/after data
    "copilot_messages",       # private assistant conversations
    "users",                  # password hashes, emails, reset tokens
    "tenant_users",           # roles and access data
    "voice_pins",             # secret sign-in PIN
    "auth_attempts",          # sign-in attempts (emails, addresses)
    "people",                 # worker names, phone, hourly_rate, e_verify_status
    "certifications",         # worker certificates tied to people
    "project_members",        # who may see a project: access data, not content
    "payment_transactions",   # billing records
    "tia_generations",        # time impact analyses prepared for counsel (privileged)
    "marketplace_postings",   # shared across companies, cannot be split by tenant_id
    "marketplace_responses",  # shared across companies, cannot be split by tenant_id
    "drawing_markups",        # private "personal" markups (phase 4 at the earliest)
)


@pytest.mark.parametrize("name", NEVER_INDEXED)
def test_sensitive_collections_never_indexed(registry, name):
    entry = registry.get(name)
    if entry is not None:
        assert entry.decision != "index", f"{name} must never be indexed"


# ---------------------------------------------------------------------------
# Part 4: forbidden text never reaches chunk text
# ---------------------------------------------------------------------------

# Marker values. Each is unusual enough that it can only appear in chunk
# text if a chunker copied the field.
EMAIL = "leakcheck.person@forbidden-example.test"
PHONE = "(718) 555-0199"   # a real US format, so the chunkers' phone scrubbers recognise it
TOKEN = "tok_FORBIDDEN_9f3k2q"
PASSWORD_HASH = "$2b$12$FORBIDDENHASHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
STORAGE = "tenants/forbidden-bucket/obj-7731.bin"
URL = "https://files.forbidden-example.test/obj-7731"
ADDRESS = "4417 Quuxwater Boulevard"
PERSON_FIRST, PERSON_LAST = "Zebulon", "Quintavius"
ATTENDANCE_NAME = f"{PERSON_FIRST} {PERSON_LAST}"
MONEY = 913579.17          # rates, costs, amounts
LAT = 40.8571937
LNG = -73.9182645
FORBIDDEN_STRINGS = (EMAIL, TOKEN, "FORBIDDENHASH", STORAGE, URL, "forbidden-example",
                     "forbidden-bucket", "obj-7731", "Quuxwater", PERSON_FIRST, PERSON_LAST,
                     "555-0199")
# Checked after removing commas and spaces and lower-casing, so "$913,579.17",
# "913 579", a rounded "$913.6k" / "$914K" and a rounded "40.8572" are caught.
FORBIDDEN_SQUASHED = ("913579", "914k",                         # money
                      "8571937", "9182645", "40.857", "73.918")  # GPS
# Money rounded or shortened: "$913,579", "$913.6k", "913.58" all start "913" + digit.
_MONEY_PREFIX_RE = re.compile(r"913\.?\d")
# Checked against the digits only, so any phone layout is caught.
FORBIDDEN_DIGIT_RUNS = ("7185550199",)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

SENSITIVE_FIELDS = {
    # contact details and addresses
    "email": EMAIL, "contact_email": EMAIL, "other_party_email": EMAIL,
    "user_email": EMAIL, "created_by_email": EMAIL, "updated_by": EMAIL,
    "phone": PHONE, "contact_phone": PHONE, "other_party_phone": PHONE, "mobile": PHONE,
    # A crew's home yard can be a person's home. (A project's address_line1 is
    # NOT here: the design indexes the job-site address on purpose.)
    "home_yard_address": ADDRESS,
    # pay / cost rates and money
    "rate": MONEY, "hourly_rate": MONEY, "daily_rate": MONEY, "monthly_rate": MONEY,
    "hourly_burdened_cost": MONEY, "base_rate_usd": MONEY, "prevailing_wage_rate_usd": MONEY,
    "cost": MONEY, "cost_usd": MONEY, "cost_impact_estimated_usd": MONEY,
    "actual_cost_impact": MONEY, "estimated_cost_impact": MONEY,
    "actual_cost_usd": MONEY, "estimated_cost_impact_usd": MONEY, "ai_cost_estimate_usd": MONEY,
    "amount": MONEY, "amount_usd": MONEY, "contract_value_usd": MONEY, "unit_price": MONEY,
    # GPS
    "lat": LAT, "lng": LNG, "latitude": LAT, "longitude": LNG,
    "gps_lat": LAT, "gps_lng": LNG, "exif_gps_lat": LAT, "exif_gps_lng": LNG,
    "gps": {"lat": LAT, "lng": LNG},
    # storage keys and links
    "storage_path": STORAGE, "storage_key": STORAGE, "source_path": STORAGE,
    "url": URL, "file_url": URL, "pdf_url": URL, "audio_url": URL, "photo_url": URL,
    "thumbnail_url": URL, "dr_template_file_url": URL, "ms_project_file_url": URL,
    "qr_code_url": URL, "photo_urls": [URL, STORAGE],
    # tokens and password hashes
    "token": TOKEN, "msp_worker_token": TOKEN, "api_key": TOKEN, "invite_code": TOKEN,
    "password_hash": PASSWORD_HASH, "reset_token_hash": PASSWORD_HASH,
}

# Contact details typed into free text. types.py says chunk text never holds
# emails or phone numbers, so every chunker must scrub its free text.
TYPED_CONTACT = f" Contact {EMAIL} or {PHONE}, files at {URL}."

# Ordinary words in the usual text fields, so chunkers have something to emit.
NORMAL_TEXT = {
    "title": "Stair rail anchor spacing",
    "subject": "Stair rail anchor spacing at level 3",
    "name": "Level 3 stair rail",
    "description": "Anchors at the level 3 stair rail are spaced wider than shown on the drawing." + TYPED_CONTACT,
    "notes": "Crew installed formwork for the east footing and poured the slab on grade." + TYPED_CONTACT,
    "question": "Can the anchor spacing be increased to 24 inches?",
    "answer": "Yes, keep the spacing at 24 inches and add one anchor at each end.",
    "body": "Lesson learned: confirm embed locations before the pour." + TYPED_CONTACT,
    "text": "Ductwork rough-in started on level 2.",
    "text_content": "Check the beam pocket here.",
    "summary": "Footing work finished; slab poured.",
    "caption": "East footing after the pour",
    "comments": "Reviewed with the superintendent.",
    "work_performed": "Formwork and concrete placement at the east footing.",
    "company_name": "Acme Concrete",
    "trade": "Concrete",
    "location": "Level 3, east stair",
    "zone_name": "East wing",
    "weather_conditions": "Clear and dry",
    "status": "open",
}

# Per-collection fields a chunker needs before it emits anything. Without
# them the chunker returns no chunks and its text is never checked.
FIXTURE_OVERRIDES = {
    "equipment": {"asset_tag": "EX-12", "type": "excavator"},
    "spec_sections": {"project_spec_id": "spec-1", "section_title": "Cast-in-place concrete",
                      "text_content": "PART 1 GENERAL\nConcrete work for footings and slabs." + TYPED_CONTACT},
    "recovery_reports": {
        "status": "finalized",
        "ai_narrative": "The east footing slipped two days; a second crew recovers it." + TYPED_CONTACT,
        "recovery_actions": [{"action": "Add a second formwork crew", "rationale": "Catch up the footing",
                              "owner": "Superintendent", "cost_usd": MONEY, "cost": MONEY}],
    },
    "mitigations": {"linked_delay_id": "delay-1", "strategy_name": "Add a second crew",
                    "proposed_action_description": "Add a second formwork crew for one week." + TYPED_CONTACT,
                    "linked_task_ids": ["task-1"]},
    "schedule_proposals": {"review_notes": "Reviewed the uploaded schedule." + TYPED_CONTACT,
                           "ai_review": {"model": "claude", "summary": "Logic looks complete.",
                                         "flags": ["Two activities have no successor."]}},
    # Same line the knowledge-base ingest writes (routers/iter7_router.py:557).
    "kb_entries": {"body": NORMAL_TEXT["body"] + "\nIMPACT: cost=$913579.17, time=5d"},
    # Attendance rows hold a worker's personal name and a link to people.
    "daily_report_attendance": {"name": ATTENDANCE_NAME, "person_id": "person-1"},
    "daily_presence": {"person_id": "person-1"},
}

# Indexed collections that never make chunks of their own: they re-queue a
# parent record whose chunk carries their text.
ROLLUP_ONLY = {
    "daily_presence": "rolls up into the daily report's manpower chunk",
    "daily_report_attendance": "rolls up into the daily report's manpower chunk (names never)",
    "photo_tray": "unfiled uploads; AI text rolls into the filed photos made from it",
    "submittal_review_events": "a review note rolls up into its submittal",
    "work_item_links": "the link rolls up into its field issue",
    "floors": "the floor name is shown on, and re-indexed with, each of its locations",
}

# Who must NOT see chunks of these collections (design section 2 / 4.3).
ROLE_EXCLUDED = {
    "recovery_reports": {"submittal_tech", "scheduling_engineer"},
}
# Collections that, if ever indexed, must always carry allowed_roles.
ROLE_RESTRICTED = {"recovery_reports", "logic_proposals"}


def _synthetic_doc(collection: str) -> dict:
    """One generic source row: ids and dates a chunker expects, normal text,
    every sensitive field stuffed with a forbidden marker, and the
    collection's own required fields."""
    try:
        from bson import ObjectId
        oid = ObjectId()
    except Exception:  # pragma: no cover - bson ships with pymongo
        oid = "65f000000000000000000001"
    doc = {
        "_id": oid,
        "id": f"{collection}-row-1",
        "tenant_id": "tenant-1",
        "project_id": "project-1",
        "daily_report_id": "daily-report-1",
        "report_id": "daily-report-1",
        "parent_id": "parent-1",
        "task_id": "task-1",
        "schedule_activity_id": "task-1",
        "submittal_id": "submittals-row-1",
        "work_item_id": "work_items-row-1",
        "activity_code": "A1010",
        "number": 12,
        "rfi_number": 12,
        "date": "2026-10-01",
        "report_date": "2026-10-01",
        "event_date": "2026-10-01",
        "created_at": "2026-10-01T14:00:00+00:00",
        "updated_at": "2026-10-01T15:00:00+00:00",
        "created_by": "user-1",
        "deleted": False,
        "is_deleted": False,
        "deleted_at": None,
        "headcount": 4,
        "trades": ["Concrete"],
        "tags": ["footing", "pour"],
        "visibility": "published",
        "source_kind": "manual",
        "is_cui": False,
        "govt_security_level": "public",
    }
    doc.update(NORMAL_TEXT)
    doc.update(copy.deepcopy(SENSITIVE_FIELDS))
    doc.update(copy.deepcopy(FIXTURE_OVERRIDES.get(collection, {})))
    return doc


_PARENT_FIELDS = {
    "projects": {"id": "project-1", "name": "Harbor Point Garage", "number": "P-100",
                 "status": "active", "is_cui": False},
    "daily_reports": {"id": "daily-report-1", "report_date": "2026-10-01", "status": "submitted"},
    "tasks": {"id": "task-1", "activity_code": "A1010", "name": "Pour east footing",
              "trade": "Concrete"},
    "users": {"id": "user-1", "name": "Pat Builder", "full_name": "Pat Builder", "role": "admin"},
    "tenants": {"id": "tenant-1", "name": "Builder Co"},
    # A person record: a roll-up that turns person_id into a name would leak it.
    "people": {"id": "person-1", "name": ATTENDANCE_NAME, "first_name": PERSON_FIRST,
               "last_name": PERSON_LAST, "full_name": ATTENDANCE_NAME,
               "email": EMAIL, "phone": PHONE},
}


# Collections whose rows belong to one company. A lookup on them must carry
# tenant_id, or the chunker must check the returned row's tenant_id.
COMPANY_SCOPED = {
    "projects", "daily_reports", "tasks", "submittals", "work_items", "delays",
    "project_specs", "rfis", "locations", "floors", "photo_tray", "photos",
    "change_orders", "daily_presence", "daily_report_attendance", "kb_entries",
    "mitigations", "recovery_reports", "coordination_events", "crews", "equipment",
}
# Lookups that are by id only on purpose: (source collection, looked-up collection).
ID_ONLY_BY_DESIGN = {
    # Activity rows carry no company; their report is the only source of it.
    ("daily_report_activities", "daily_reports"),
}

OTHER_TENANT = "tenant-OTHER"
FOREIGN = "Othercorp Hidden Tower"
_FOREIGN_TEXT_KEYS = ("name", "title", "number", "subject", "description", "notes", "summary",
                      "activity_code", "full_name", "company_name", "section_title",
                      "text_content", "spec_title", "trade", "trade_code")


def _fake_row(collection: str) -> dict:
    """A plausible row for any collection a chunker might look up. It still
    carries the forbidden markers, so roll-ups and parent lookups are tested
    too."""
    doc = _synthetic_doc(collection)
    doc.update(copy.deepcopy(_PARENT_FIELDS.get(collection, {})))
    return doc


class _FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def sort(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def skip(self, *a, **k):
        return self

    def batch_size(self, *a, **k):
        return self

    async def to_list(self, length=None):
        return list(self._rows)

    def __aiter__(self):
        self._it = iter(self._rows)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _FakeCollection:
    def __init__(self, db: "_FakeDb", name: str):
        self.db = db
        self.name = name

    def _row(self, filt) -> Optional[dict]:
        filt = filt if isinstance(filt, dict) else {}
        self.db.lookups.append((self.name, dict(filt)))
        if self.name in self.db.missing:
            return None
        row = _fake_row(self.name)
        # The row matches the plain equality parts of the filter.
        for k, v in filt.items():
            if not k.startswith("$") and not isinstance(v, (dict, list)):
                row[k] = v
        if self.db.foreign and self.db.is_unscoped(self.name, filt):
            # Simulate a stray id that points at ANOTHER company's row.
            row["tenant_id"] = OTHER_TENANT
            for k in _FOREIGN_TEXT_KEYS:
                row[k] = FOREIGN
        return row

    async def find_one(self, filt=None, *a, **k):
        return self._row(filt)

    def find(self, filt=None, *a, **k):
        row = self._row(filt)
        return _FakeCursor([row] if row is not None else [])

    def aggregate(self, *a, **k):
        return _FakeCursor([])

    async def count_documents(self, *a, **k):
        return 1

    async def distinct(self, *a, **k):
        return []


class _FakeDb:
    """Fake database.

    lookups:  every (collection, filter) a chunker asked for.
    missing:  collections that return nothing (orphan rows).
    foreign:  lookups on a company-scoped collection WITHOUT tenant_id
              return another company's row, so a chunker that neither filters
              nor checks the company would copy the other company's text."""

    def __init__(self, source: str = "", missing=(), foreign: bool = False):
        self.source = source
        self.missing = set(missing)
        self.foreign = foreign
        self.lookups: list[tuple[str, dict]] = []

    def is_unscoped(self, collection: str, filt: dict) -> bool:
        return (collection in COMPANY_SCOPED and "tenant_id" not in filt
                and (self.source, collection) not in ID_ONLY_BY_DESIGN)

    def __getitem__(self, name):
        return _FakeCollection(self, name)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _FakeCollection(self, name)


def _run(entry, doc: dict, db: _FakeDb):
    from services.search.types import ChunkContext
    return asyncio.run(entry.chunker(ChunkContext(db), doc))


def _leaks(text: str) -> list[str]:
    found = [s for s in FORBIDDEN_STRINGS if s in text]
    found += [f"email-like text {m!r}" for m in _EMAIL_RE.findall(text)]
    squashed = text.replace(",", "").replace(" ", "").lower()
    found += [f"number {d}" for d in FORBIDDEN_SQUASHED if d in squashed]
    found += [f"money {m!r}" for m in _MONEY_PREFIX_RE.findall(squashed)]
    digits = re.sub(r"\D", "", text)
    found += [f"phone digits {d}" for d in FORBIDDEN_DIGIT_RUNS if d in digits]
    return found


def _chunk_problems(name: str, result, label: str = "") -> list[str]:
    """Leaks in a result's chunk text, title and link."""
    problems = []
    for i, chunk in enumerate(result.chunks):
        for field_name in ("text", "title", "url"):
            value = getattr(chunk, field_name, None) or ""
            leaks = _leaks(value)
            if leaks:
                problems.append(f"{name}{label} chunk {i} {field_name}: {', '.join(leaks)}")
        url = chunk.url
        if url is not None and not url.startswith("/"):
            problems.append(f"{name}{label} chunk {i}: url {url!r} is not an app path")
    return problems


def _indexed(registry) -> dict:
    return {k: e for k, e in sorted(registry.items()) if e.decision == "index" and e.chunker}


def _parent_result(registry, coll: str, filt: dict):
    """Run the chunker of a re-queued parent record (filter applied to a fake row)."""
    entry = registry.get(coll)
    if entry is None or entry.decision != "index" or not entry.chunker:
        return None, f"re-queues {coll}, which is not indexed"
    doc = _synthetic_doc(coll)
    for k, v in (filt or {}).items():
        if not k.startswith("$") and not isinstance(v, (dict, list)):
            doc[k] = v
    return _run(entry, doc, _FakeDb(coll)), None


def test_rollup_only_entries_are_indexed(registry):
    """ROLLUP_ONLY must name real indexed collections, so it cannot quietly
    excuse a collection that no longer exists."""
    stale = sorted(n for n in ROLLUP_ONLY if n not in _indexed(registry))
    assert not stale, f"ROLLUP_ONLY names collections that are not indexed: {stale}"


def test_chunks_never_contain_forbidden_text(registry):
    from services.search.types import ChunkResult

    indexed = _indexed(registry)
    assert indexed, "no indexed collections in the registry"

    problems: list[str] = []
    for name, entry in indexed.items():
        doc = _synthetic_doc(name)
        try:
            result = _run(entry, doc, _FakeDb(name))
        except Exception as exc:  # a chunker that crashes would stall the queue
            problems.append(f"{name}: chunker raised {type(exc).__name__}: {exc}")
            continue
        if result is not None and not isinstance(result, ChunkResult):
            problems.append(f"{name}: returned {type(result).__name__}, expected ChunkResult or None")
            continue
        chunks = result.chunks if result else []
        problems += _chunk_problems(name, result) if result else []

        if name in ROLLUP_ONLY:
            if chunks:
                problems.append(f"{name}: listed in ROLLUP_ONLY but made {len(chunks)} chunk(s)")
            if not result or not result.requeue:
                problems.append(f"{name}: roll-up row re-queued no parent record")
                continue
            for coll, filt in result.requeue:
                try:
                    parent, why = _parent_result(registry, coll, filt)
                except Exception as exc:
                    problems.append(f"{name}: parent {coll} chunker raised {type(exc).__name__}: {exc}")
                    continue
                if why:
                    problems.append(f"{name}: {why}")
                elif parent is None or not parent.chunks:
                    problems.append(f"{name}: parent {coll} made no chunk, so the roll-up was not checked")
                else:
                    problems += _chunk_problems(name, parent, f" -> parent {coll}")
            continue

        if not chunks:
            problems.append(f"{name}: made no chunk from the synthetic row, so its text was never "
                            "checked. Add the fields it needs to FIXTURE_OVERRIDES, or list it in "
                            "ROLLUP_ONLY with a reason.")
            continue
        for i, chunk in enumerate(chunks):
            if name in ROLE_RESTRICTED and not chunk.allowed_roles:
                problems.append(f"{name} chunk {i}: allowed_roles must be set")
            banned = ROLE_EXCLUDED.get(name, set()) & set(chunk.allowed_roles or [])
            if banned:
                problems.append(f"{name} chunk {i}: allowed_roles includes {sorted(banned)}")
    assert not problems, "Chunker problems:\n" + "\n".join(problems)


# ---------------------------------------------------------------------------
# Part 5: deleted, soft-deleted and orphan rows make no chunks
# ---------------------------------------------------------------------------

# How each collection is soft-deleted in the app. The assistant's delete tool
# (services/copilot_tools.py, _tool_delete_entity) sets status "deleted" and
# deleted_at together on any of its collections, so that pair is checked for
# every indexed collection; the cases below are the collection-specific forms.
SOFT_DELETE_CASES = [
    ("delays", {"deleted_at": "2026-10-02T00:00:00+00:00"}),
    ("coordination_events", {"deleted_at": "2026-10-02T00:00:00+00:00"}),
    ("tasks", {"status": "deleted"}),
    ("locations", {"status": "deleted"}),
    ("recovery_reports", {"status": "archived"}),
]
_DELETE_TOOL_MARK = {"status": "deleted", "deleted_at": "2026-10-02T00:00:00+00:00"}


def _deleted_cases():
    try:
        from services.search.registry import get_registry
        names = [n for n in _indexed(get_registry()) if n not in ROLLUP_ONLY]
    except Exception:  # pragma: no cover - the registry fixture reports this
        names = []
    cases = [pytest.param(n, _DELETE_TOOL_MARK, id=f"{n}-delete_tool") for n in names]
    cases += [pytest.param(n, mark, id=f"{n}-{'-'.join(mark)}") for n, mark in SOFT_DELETE_CASES]
    return cases


@pytest.mark.parametrize("name,mark", _deleted_cases())
def test_soft_deleted_rows_make_no_chunks(registry, name, mark):
    entry = registry.get(name)
    assert entry is not None and entry.decision == "index", f"{name} is not indexed any more"
    doc = _synthetic_doc(name)
    # Sanity: the row makes chunks while live, so a zero below means something.
    live = _run(entry, copy.deepcopy(doc), _FakeDb(name))
    assert live and live.chunks, f"{name}: the live row made no chunk"
    doc.update(mark)
    result = _run(entry, doc, _FakeDb(name))
    assert not (result and result.chunks), f"{name} with {mark} still made {len(result.chunks)} chunk(s)"


def test_orphan_rows_make_no_chunks(registry):
    """A project-scoped row whose project (or, for daily-report children,
    whose report) no longer exists makes no chunks."""
    problems = []
    for name, entry in _indexed(registry).items():
        if name in ROLLUP_ONLY or name == "projects":
            continue
        live = _run(entry, _synthetic_doc(name), _FakeDb(name))
        if not live or not live.chunks or live.project_id is None:
            continue  # company-level record: no project to lose
        result = _run(entry, _synthetic_doc(name),
                      _FakeDb(name, missing={"projects", "daily_reports"}))
        if result and result.chunks:
            problems.append(f"{name}: project / report missing but made {len(result.chunks)} chunk(s)")
    assert not problems, "Orphan rows were indexed:\n" + "\n".join(problems)


# ---------------------------------------------------------------------------
# Part 6: a lookup never pulls another company's data into a chunk
# ---------------------------------------------------------------------------

def test_lookups_stay_inside_the_company(registry):
    """Every lookup on a company-scoped collection either filters by
    tenant_id or checks the row it gets back. Here an unfiltered lookup
    returns another company's row: its text must not reach the chunk, and
    the chunk must stay with the source row's company."""
    problems = []
    for name, entry in _indexed(registry).items():
        doc = _synthetic_doc(name)
        db = _FakeDb(name, foreign=True)
        try:
            result = _run(entry, doc, db)
        except Exception as exc:
            problems.append(f"{name}: chunker raised {type(exc).__name__}: {exc}")
            continue
        unscoped = sorted({c for c, f in db.lookups if db.is_unscoped(c, f)})
        for i, chunk in enumerate(result.chunks if result else []):
            for field_name in ("text", "title"):
                if "Othercorp" in (getattr(chunk, field_name, None) or ""):
                    problems.append(f"{name} chunk {i} {field_name}: another company's text "
                                    f"(lookups without tenant_id on: {', '.join(unscoped)})")
        if result and result.chunks and doc.get("tenant_id") and result.tenant_id != doc["tenant_id"]:
            problems.append(f"{name}: chunks filed under company {result.tenant_id!r}")
    assert not problems, "Cross-company lookups:\n" + "\n".join(problems)
