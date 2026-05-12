"""Operator-only CLI: reset a user's password directly via DB.

This is the bottom-of-the-escalation-ladder recovery path:

  1. User can log in normally          -> nothing to do
  2. User locked out, admin available   -> POST /api/team/members/{id}/reset-password
  3. Every admin locked out             -> THIS SCRIPT (requires shell + MONGO_URL)

Writes a freshly bcrypt'd password_hash and appends an
OPERATOR_PASSWORD_RESET audit entry. Use with care -- direct DB
write bypasses the tenant-admin permission check that the HTTP
endpoint enforces. The audit entry captures who ran the script
(--executed-by required). Active Bearer JWTs remain valid until
they expire naturally (~7 days).

Usage:

  python -m scripts.reset_password \\
    --email user@example.com \\
    --password 'NewPassword123!' \\
    --executed-by abhi@ice-aec.com \\
    --note "User locked out by hash drift; bcrypt self-test PASSED so issue isolated to this row."

Refuses without --executed-by, --email, --password. Prints the
hash prefix it generated so logs can correlate with later
verify_password traces. Does NOT print the plaintext.
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="scripts.reset_password",
        description=(
            "Reset a user's password by direct DB write. Bypasses HTTP "
            "auth -- only run when no tenant admin can log in."
        ),
    )
    p.add_argument("--email", required=True,
                    help="Email address of the user to reset.")
    p.add_argument("--password", required=True,
                    help="New plaintext password (>= 8 chars).")
    p.add_argument("--executed-by", required=True,
                    help="Operator id or email captured in the audit entry.")
    p.add_argument("--note", default=None,
                    help="Optional human-readable note for the audit entry.")
    p.add_argument("--tenant-id", default=None,
                    help="Optional tenant scope. If a user's email is unique "
                         "across the install (the common case), omit. If the "
                         "same email exists in multiple tenants, this picks "
                         "which one.")
    return p.parse_args(argv)


async def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    if len(args.password) < 8:
        print("ERROR: password must be at least 8 chars.", file=sys.stderr)
        return 2

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    # Lazy imports so --help works without backend deps installed.
    from motor.motor_asyncio import AsyncIOMotorClient
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    except ImportError:
        pass

    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME")
    if not mongo_url or not db_name:
        print("ERROR: MONGO_URL and DB_NAME env vars required.", file=sys.stderr)
        return 2

    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]
    import database as _database
    _database._client = client  # type: ignore[attr-defined]
    _database._db = db          # type: ignore[attr-defined]

    from auth import hash_password, validate_bcrypt_runtime
    from audit import write_audit

    # Run the bcrypt self-test FIRST. If it fails, refuse to write a
    # hash we know won't validate -- that would just dig the hole
    # deeper. The operator can then investigate the runtime first.
    rt = validate_bcrypt_runtime()
    if not rt.get("ok"):
        print(
            f"ERROR: bcrypt runtime self-test failed: {rt.get('error')!r}\n"
            f"Refusing to write a password hash that would not validate.\n"
            f"Investigate bcrypt runtime first (version={rt.get('version')!r}).",
            file=sys.stderr,
        )
        return 3
    print(f"bcrypt runtime self-test: PASS (version={rt.get('version')!r})")

    # Find the user. Default tenant scope is "any tenant" but warn if
    # the email is non-unique.
    email_lc = args.email.lower()
    q = {"email": email_lc}
    if args.tenant_id:
        q["tenant_id"] = args.tenant_id
    matches = await db.users.find(q, {"_id": 0, "id": 1, "tenant_id": 1, "email": 1, "password_hash": 1}).to_list(length=10)
    if not matches:
        scope = f" in tenant {args.tenant_id!r}" if args.tenant_id else ""
        print(f"ERROR: no user found with email {args.email!r}{scope}.", file=sys.stderr)
        return 4
    if len(matches) > 1:
        print(
            f"ERROR: email {args.email!r} matches {len(matches)} users across multiple tenants. "
            f"Re-run with --tenant-id to disambiguate.\n"
            f"Tenant ids: {[m.get('tenant_id') for m in matches]}",
            file=sys.stderr,
        )
        return 5

    user = matches[0]
    new_hash = hash_password(args.password)
    before_prefix = (user.get("password_hash") or "")[:7] if user.get("password_hash") else None
    now_iso = datetime.now(timezone.utc).isoformat()

    # Apply the reset.
    await db.users.update_one(
        {"id": user["id"]},
        {"$set": {
            "password_hash": new_hash,
            "password_reset_at": now_iso,
            "password_reset_by": args.executed_by,
        }},
    )
    # Note: the Google-OAuth cookie session path was removed in the
    # 2026-05-11 purge, so there's no longer a user_sessions collection
    # to invalidate. Active Bearer JWTs are stateless and expire on
    # their own.

    # Audit (uses write_audit so the SHA-256 chain stays intact).
    await write_audit(
        tenant_id=user["tenant_id"], table_name="users",
        op="OPERATOR_PASSWORD_RESET",
        record_id=user["id"],
        before={"hash_prefix": before_prefix},
        after={
            "hash_prefix": new_hash[:7],
            "reset_user_email": user.get("email"),
            "executed_by": args.executed_by,
            "note": args.note,
        },
        actor_id=args.executed_by,
        request_id=f"scripts.reset_password.{int(datetime.now(timezone.utc).timestamp())}",
    )

    print()
    print("=== scripts.reset_password ===")
    print(f"User:        {user.get('email')} (id={user['id']})")
    print(f"Tenant:      {user['tenant_id']}")
    print(f"Old hash prefix: {before_prefix!r}")
    print(f"New hash prefix: {new_hash[:7]!r}")
    print(f"Audit op: OPERATOR_PASSWORD_RESET written")
    print(f"Executed by: {args.executed_by}")
    if args.note:
        print(f"Note: {args.note}")
    print()
    print("User must log in with the new password. Active Bearer JWTs "
          "remain valid until they expire (~7 days).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
