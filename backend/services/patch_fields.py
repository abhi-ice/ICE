"""Fields a generic "edit any field" save must never write (2026-11-16).

Shared by the generic edit endpoint (PATCH /crud/{entity}/{id}), the
subcontractor / supplier edit endpoints and the AI assistant's
update_entity tool. All of them take a free-form set of fields from the
client and $set it on a record, so without this list a client could move a
record to another company (tenant_id), point a photo at another company's
stored file (storage_path), fake lock / approval stamps or audit hashes.

These fields are set by the server or belong to dedicated endpoints. No
screen sends them, so they are dropped quietly rather than refused: an
older client's save still goes through with its real edits.
"""
from __future__ import annotations

PROTECTED_FIELDS = frozenset({
    "id", "_id", "tenant_id", "created_at", "created_by",
    "updated_at", "updated_by",
    # stored files: keys are only ever built on the server (make_path)
    "storage_path", "storage_key", "thumbnail_path", "source_path",
    "pdf_url", "audio_url", "file_url", "dr_template_file_url",
    # photo evidence fingerprints and capture stamps
    "sha256", "sha256_hash", "phash", "captured_by", "uploaded_by",
    "server_received_timestamp", "tray_photo_id",
    # lock / approval state, owned by the daily report endpoints
    "locked", "is_locked", "locked_at", "locked_by", "progress_lock_at",
    "submitted_at", "submitted_by", "approved_at", "approved_by",
    # account / permission fields
    "is_admin", "is_superuser", "permissions", "password", "password_hash",
    "hashed_password",
    # audit-chain hashes
    "hash", "row_hash", "prev_hash", "audit_hash", "content_hash",
})


def is_bad_field_name(key) -> bool:
    """True for a name the database would read as an operator or a nested
    path ("$set", "a.b"), or that would crash the write (NUL, empty)."""
    return (not isinstance(key, str) or not key or key.startswith("$")
            or "." in key or "\x00" in key)


def split_patch_fields(payload: dict) -> tuple[dict, list]:
    """Return (fields safe to $set, list of bad field names).

    Protected fields are dropped silently; bad names are reported so the
    caller can answer with a clear error instead of a database crash."""
    clean: dict = {}
    bad: list = []
    for k, v in (payload or {}).items():
        if is_bad_field_name(k):
            bad.append(k)
            continue
        if k in PROTECTED_FIELDS:
            continue
        clean[k] = v
    return clean, bad
