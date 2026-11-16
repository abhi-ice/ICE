"""Activity codes for activities created in the field (2026-11-16).

Every activity a superintendent adds from a daily report (an "unplanned"
activity, not in the master schedule) gets its own code in its project:
FW-0001, FW-0002, ... ("FW" = field work).

The old rule took the trailing number of the alphabetically-last code in the
project and added one, so a project whose schedule codes sort after the new
code handed out the SAME code (for example AF2311) to every new activity.

How the next number is chosen:
  * a per-project counter document (collection field_activity_code_counters)
    is raised to at least the highest FW-n code already in the project, so
    codes created before the counter existed, or imported, are never reused;
  * the counter is then increased by one in a single atomic database step,
    so two activities created at the same moment always get different codes.
Existing tasks keep their codes; nothing here renames old data.
"""
from __future__ import annotations

import re

from pymongo.errors import DuplicateKeyError

from database import tenant_filter

FIELD_CODE_PREFIX = "FW-"
_CODE_RE = re.compile(r"^FW-(\d+)$")


def format_field_code(n: int) -> str:
    return f"{FIELD_CODE_PREFIX}{int(n):04d}"


async def _highest_existing(db, tenant_id: str, project_id: str) -> int:
    rows = await db.tasks.find(
        tenant_filter(tenant_id, {"project_id": project_id,
                                  "activity_code": {"$regex": r"^FW-\d+$"}}),
        {"_id": 0, "activity_code": 1},
    ).to_list(None)
    best = 0
    for r in rows:
        m = _CODE_RE.match(r.get("activity_code") or "")
        if m:
            best = max(best, int(m.group(1)))
    return best


async def next_field_activity_code(db, tenant_id: str, project_id: str) -> str:
    """Reserve and return the next unused FW-n code for this project."""
    counter_id = f"{tenant_id}:{project_id}"
    highest = await _highest_existing(db, tenant_id, project_id)
    # Raise the counter to at least the highest code in use. Two first-time
    # callers can race on creating the document; the loser just retries.
    for _ in range(3):
        try:
            await db.field_activity_code_counters.update_one(
                {"_id": counter_id},
                {"$max": {"seq": highest},
                 "$setOnInsert": {"tenant_id": tenant_id, "project_id": project_id}},
                upsert=True,
            )
            break
        except DuplicateKeyError:
            continue
    doc = await db.field_activity_code_counters.find_one_and_update(
        {"_id": counter_id}, {"$inc": {"seq": 1}},
        upsert=True, return_document=True,  # True = return the document after the change
    )
    return format_field_code(int((doc or {}).get("seq", highest + 1)))
