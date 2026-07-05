"""Regression: photo-AI tenant gate must not report a real tenant as
"tenant_not_found".

Root cause (fixed 2026-07-05): check_cui_safety / check_budget looked the
tenant up with an INCLUSION projection listing only the AI/display fields
(e.g. {"_id":0, "ai_enabled":1, "ai_monthly_budget_usd":1, ...}). When the
matched tenant document carried NONE of those fields -- which is exactly the
shape of a demo/seed/freshly-signed-up tenant that predates migration 010's
backfill -- MongoDB returns an EMPTY dict {} for the projection. {} is falsy in
Python, so `if not tenant:` fired and the gate returned reason="tenant_not_found",
surfacing "Tenant not found." in the UI on Upload photo -> AI analysis.

The fix projects `id` explicitly so a matched-but-field-sparse tenant still
resolves to a truthy doc, and missing budget fields fall back to their defaults.

These tests bind services.photo_ai.get_db to a projection-AWARE in-memory stub
(the shared test `database` stub ignores projections and so cannot reproduce the
bug). Run: python -m pytest backend/tests/test_photo_ai_tenant_lookup.py -q
"""
import asyncio
import os
import sys
import types
from pathlib import Path

_BACKEND = str(Path(__file__).resolve().parents[1])
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Force dev-stub posture so no network / Vision is ever touched here.
os.environ["PHOTO_AI_DEV_MODE"] = "1"


# ---------------------------------------------------------------------------
# Projection-AWARE in-memory stub. Mirrors MongoDB inclusion-projection
# semantics: a matched doc is reduced to ONLY the projected keys that EXIST on
# it, so a doc lacking every projected field projects down to {} (the exact
# condition that triggered the bug).
# ---------------------------------------------------------------------------
def _matches(row, q):
    for k, v in (q or {}).items():
        if k == "_id":
            continue
        rv = row.get(k)
        if isinstance(v, dict):
            if "$in" in v and rv not in v["$in"]:
                return False
            if "$exists" in v and (k in row) != v["$exists"]:
                return False
        elif rv != v:
            return False
    return True


def _project(row, proj):
    if not proj:
        return dict(row)
    include = {k for k, val in proj.items() if val in (1, True) and k != "_id"}
    out = {k: row[k] for k in include if k in row}
    if proj.get("_id", 1) not in (0, False):
        out["_id"] = row.get("_id")
    return out


class _Col:
    def __init__(self):
        self.rows = []

    async def find_one(self, q=None, p=None):
        for r in self.rows:
            if _matches(r, q or {}):
                return _project(r, p)
        return None

    async def update_one(self, q, ops):
        for r in self.rows:
            if _matches(r, q or {}):
                if "$set" in ops:
                    r.update(ops["$set"])
                return types.SimpleNamespace(matched_count=1, modified_count=1)
        return types.SimpleNamespace(matched_count=0, modified_count=0)


class _DB:
    def __init__(self):
        self.tenants = _Col()
        self.projects = _Col()


_STUB = _DB()

# Install a `database` module stub DEFENSIVELY (only if a sibling test module
# hasn't already), so photo_ai imports without the real `motor` driver when
# this file is collected/run in isolation. Mirrors test_enterprise_cpm's guard.
if "database" not in sys.modules:
    _db_mod = types.ModuleType("database")
    _db_mod.get_db = lambda: _STUB
    _db_mod.tenant_filter = lambda tid, q=None: {**(q or {}), "tenant_id": tid}
    sys.modules["database"] = _db_mod

if "audit" not in sys.modules:
    _audit_mod = types.ModuleType("audit")

    async def _noop_audit(**_kw):
        return None

    _audit_mod.write_audit = _noop_audit
    sys.modules["audit"] = _audit_mod

import services.photo_ai as pa  # noqa: E402

# Bind THIS module's gate calls onto our projection-aware stub, independent of
# whichever `database` stub won the sys.modules slot at collection time.
pa.get_db = lambda: _STUB


def _reset(tenants=None, projects=None):
    _STUB.tenants.rows = list(tenants or [])
    _STUB.projects.rows = list(projects or [])


# A demo/seed-shaped tenant: exists, has id + display_name, but NONE of the AI
# budget fields (never backfilled by migration 010).
_SPARSE_TENANT = {
    "id": "TENANT_SPARSE",
    "display_name": "ICE Construction",
    "legal_name": "ICE Construction Inc.",
    "slug": "ice",
    "status": "active",
}


def test_check_budget_field_sparse_tenant_is_not_tenant_not_found():
    _reset(tenants=[_SPARSE_TENANT])
    res = asyncio.run(pa.check_budget(tenant_id="TENANT_SPARSE"))
    assert res["reason"] != "tenant_not_found", res
    assert res["budget_ok"] is True, res
    # Falls back to the documented defaults when fields are absent.
    assert res["monthly_limit"] == 100.0, res
    assert res["current_spend"] == 0.0, res


def test_check_cui_safety_passes_tenant_gate_for_sparse_tenant():
    # Project present + not CUI so the only thing that can fail is the tenant gate.
    _reset(
        tenants=[_SPARSE_TENANT],
        projects=[{"id": "P1", "tenant_id": "TENANT_SPARSE",
                   "is_cui": False, "ai_enabled": True, "name": "P"}],
    )
    res = asyncio.run(
        pa.check_cui_safety(tenant_id="TENANT_SPARSE", project_id="P1")
    )
    assert res["reason"] != "tenant_not_found", res
    assert res["safe_to_analyze"] is True, res


def test_check_cui_safety_tenant_gate_for_tenant_without_display_name():
    # A tenant carrying ai_enabled but no display_name must still pass the
    # tenant-existence gate (the projection previously could return {} here too).
    _reset(
        tenants=[{"id": "T2", "legal_name": "Acme", "slug": "acme",
                  "ai_enabled": True}],
        projects=[{"id": "P2", "tenant_id": "T2", "is_cui": False,
                   "ai_enabled": True, "name": "P"}],
    )
    res = asyncio.run(pa.check_cui_safety(tenant_id="T2", project_id="P2"))
    assert res["reason"] != "tenant_not_found", res
    assert res["safe_to_analyze"] is True, res


def test_genuinely_missing_tenant_still_reports_tenant_not_found():
    # The gate must STILL correctly report a truly-absent tenant.
    _reset(tenants=[])
    b = asyncio.run(pa.check_budget(tenant_id="NOPE"))
    assert b["reason"] == "tenant_not_found", b
    c = asyncio.run(pa.check_cui_safety(tenant_id="NOPE", project_id="P"))
    assert c["reason"] == "tenant_not_found", c


def test_ai_disabled_tenant_still_gated():
    # A field-sparse-but-AI-disabled tenant must report tenant_ai_disabled,
    # not slip through as enabled.
    _reset(tenants=[{"id": "T3", "display_name": "Off", "ai_enabled": False}])
    b = asyncio.run(pa.check_budget(tenant_id="T3"))
    assert b["budget_ok"] is False and b["ai_enabled"] is False, b
    c = asyncio.run(pa.check_cui_safety(tenant_id="T3", project_id="P"))
    assert c["reason"] == "tenant_ai_disabled", c
