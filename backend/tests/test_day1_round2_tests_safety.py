"""Round 2 — the repo's own test suite must never wipe or target a real system.

Covers:
  1. The two "drop every collection" cleanup tests skip unless
     ICE_ALLOW_DESTRUCTIVE_TEST_CLEANUP=1 AND DB_NAME starts with test_/ice_test.
  2. conftest stops the whole session if destructive cleanup is switched on
     for a database that does not look like a throwaway test database.
  3. test_weather_autofill_concurrency.py no longer loads a .env file and
     skips cleanly when its settings are missing.
  4. No test file falls back to a remote preview host; live-server files skip
     when REACT_APP_BACKEND_URL is unset.

Every pytest run here is a child process so the outer session is untouched.
Needs MONGO_URL (a local MongoDB) for the drop checks.
"""
import os
import pathlib
import subprocess
import sys
import uuid

import pytest

TESTS_DIR = pathlib.Path(__file__).resolve().parent
BACKEND_DIR = TESTS_DIR.parent
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://127.0.0.1:27017")


def _run_pytest(args, env_overrides, unset=()):
    env = dict(os.environ)
    for k in ("ICE_ALLOW_DESTRUCTIVE_TEST_CLEANUP", "ICE_TEST_DB_ALLOWLIST", *unset):
        env.pop(k, None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args, "-q", "-rs", "-p", "no:cacheprovider"],
        cwd=str(BACKEND_DIR), env=env, capture_output=True, text=True, timeout=180,
    )


@pytest.fixture
def mongo():
    from pymongo import MongoClient
    cli = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
    try:
        cli.admin.command("ping")
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"MongoDB not reachable at {MONGO_URL}: {exc}")
    created = []

    def seed(db_name):
        created.append(db_name)
        db = cli[db_name]
        db["keep_a"].insert_one({"x": 1})
        db["keep_b"].insert_one({"x": 2})
        return db

    yield cli, seed
    for name in created:
        cli.drop_database(name)
    cli.close()


def _user_collections(db):
    return sorted(c for c in db.list_collection_names() if not c.startswith("system."))


# Unreachable local address: the cleanup test in iteration 12 makes no HTTP calls.
_FAKE_BACKEND = "http://127.0.0.1:9"
_ITER12_CLEANUP = "tests/test_iteration12.py::test_zzz_cleanup_drop_all"
_ITER11_CLEANUP = "tests/test_iteration11.py::test_zzz_cleanup_drop_all"


@pytest.mark.parametrize("node", [_ITER12_CLEANUP, _ITER11_CLEANUP])
def test_cleanup_skips_without_opt_in_even_on_test_db(mongo, node):
    cli, seed = mongo
    name = f"test_r2_victim_{uuid.uuid4().hex[:8]}"
    db = seed(name)
    before = _user_collections(db)
    r = _run_pytest([node], {"REACT_APP_BACKEND_URL": _FAKE_BACKEND,
                             "MONGO_URL": MONGO_URL, "DB_NAME": name})
    assert "1 skipped" in r.stdout, r.stdout + r.stderr
    assert "ICE_ALLOW_DESTRUCTIVE_TEST_CLEANUP" in r.stdout
    assert _user_collections(db) == before


@pytest.mark.parametrize("node", [_ITER12_CLEANUP, _ITER11_CLEANUP])
def test_cleanup_skips_with_opt_in_on_allowlisted_non_test_name(mongo, node):
    # conftest lets an allowlisted name through, but the wipe itself still
    # insists on a test_/ice_test prefix.
    cli, seed = mongo
    name = f"unit_r2_victim_{uuid.uuid4().hex[:8]}"
    db = seed(name)
    before = _user_collections(db)
    r = _run_pytest([node], {"REACT_APP_BACKEND_URL": _FAKE_BACKEND, "MONGO_URL": MONGO_URL,
                             "DB_NAME": name, "ICE_ALLOW_DESTRUCTIVE_TEST_CLEANUP": "1"})
    assert "1 skipped" in r.stdout, r.stdout + r.stderr
    assert "does not start with" in r.stdout
    assert _user_collections(db) == before


def test_session_stops_when_opt_in_targets_production_like_db(mongo):
    cli, seed = mongo
    name = f"ice_r2_prodlike_{uuid.uuid4().hex[:8]}"
    db = seed(name)
    before = _user_collections(db)
    r = _run_pytest([_ITER12_CLEANUP], {"REACT_APP_BACKEND_URL": _FAKE_BACKEND, "MONGO_URL": MONGO_URL,
                                        "DB_NAME": name, "ICE_ALLOW_DESTRUCTIVE_TEST_CLEANUP": "1"})
    assert r.returncode == 3, r.stdout + r.stderr
    assert "STOPPED" in (r.stdout + r.stderr)
    assert _user_collections(db) == before


def test_warning_banner_without_opt_in_on_production_like_db(mongo):
    cli, seed = mongo
    name = f"ice_r2_prodlike_{uuid.uuid4().hex[:8]}"
    db = seed(name)
    before = _user_collections(db)
    r = _run_pytest([_ITER12_CLEANUP], {"REACT_APP_BACKEND_URL": _FAKE_BACKEND,
                                        "MONGO_URL": MONGO_URL, "DB_NAME": name})
    assert "WARNING: tests that wipe the database" in r.stdout, r.stdout + r.stderr
    assert "1 skipped" in r.stdout
    assert _user_collections(db) == before


def test_cleanup_still_works_when_explicitly_allowed_on_test_db(mongo):
    cli, seed = mongo
    name = f"test_r2_wipe_ok_{uuid.uuid4().hex[:8]}"
    db = seed(name)
    r = _run_pytest([_ITER12_CLEANUP], {"REACT_APP_BACKEND_URL": _FAKE_BACKEND, "MONGO_URL": MONGO_URL,
                                        "DB_NAME": name, "ICE_ALLOW_DESTRUCTIVE_TEST_CLEANUP": "1"})
    assert "1 passed" in r.stdout, r.stdout + r.stderr
    assert _user_collections(db) == []


def test_session_stops_when_mongo_url_names_production_like_db(mongo):
    # DB_NAME looks safe but MONGO_URL points at another database: still stop.
    cli, seed = mongo
    name = f"test_r2_victim_{uuid.uuid4().hex[:8]}"
    db = seed(name)
    before = _user_collections(db)
    r = _run_pytest([_ITER12_CLEANUP], {"REACT_APP_BACKEND_URL": _FAKE_BACKEND,
                                        "MONGO_URL": MONGO_URL.rstrip("/") + "/ice_production",
                                        "DB_NAME": name, "ICE_ALLOW_DESTRUCTIVE_TEST_CLEANUP": "1"})
    assert r.returncode == 3, r.stdout + r.stderr
    assert _user_collections(db) == before


@pytest.mark.parametrize("how", [["-k", "not drop_all"], ["--deselect", _ITER12_CLEANUP]])
def test_deselecting_the_wipe_lets_the_run_continue(mongo, how):
    # The stop message says "deselect these tests"; that advice must work,
    # and the guard must only judge tests that will really run.
    cli, seed = mongo
    name = f"ice_r2_prodlike_{uuid.uuid4().hex[:8]}"
    db = seed(name)
    before = _user_collections(db)
    r = _run_pytest(["tests/test_iteration12.py", "--co", *how],
                    {"REACT_APP_BACKEND_URL": _FAKE_BACKEND, "MONGO_URL": MONGO_URL,
                     "DB_NAME": name, "ICE_ALLOW_DESTRUCTIVE_TEST_CLEANUP": "1"})
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "STOPPED" not in out and "WARNING: tests that wipe" not in out, out
    assert "1 deselected" in r.stdout and "test_zzz_cleanup_drop_all" not in r.stdout, out
    assert _user_collections(db) == before


def test_conftest_name_rules():
    sys.path.insert(0, str(TESTS_DIR))
    import conftest  # type: ignore
    assert conftest._looks_like_test_db("unit_r2_x")
    assert conftest._looks_like_test_db("test_abc")
    assert conftest._looks_like_test_db("ice_test_db")
    assert conftest._looks_like_test_db("smoke_1")
    assert not conftest._looks_like_test_db("ice_day1")
    assert not conftest._looks_like_test_db("ice_site_ops")
    os.environ["ICE_TEST_DB_ALLOWLIST"] = "scratch_a, scratch_b"
    try:
        assert conftest._looks_like_test_db("scratch_b")
    finally:
        os.environ.pop("ICE_TEST_DB_ALLOWLIST", None)


def test_weather_concurrency_file_reads_no_env_file():
    src = (TESTS_DIR / "test_weather_autofill_concurrency.py").read_text(encoding="utf-8")
    assert "load_dotenv" not in src
    assert ".env\"" not in src


def test_weather_concurrency_skips_cleanly_without_settings():
    r = _run_pytest(["tests/test_weather_autofill_concurrency.py"], {},
                    unset=("REACT_APP_BACKEND_URL", "MONGO_URL", "DB_NAME"))
    assert r.returncode in (0, 5), r.stdout + r.stderr
    assert "skipped" in r.stdout and "error" not in r.stdout.lower(), r.stdout


def test_no_test_file_defaults_to_remote_preview_host():
    offenders = [p.name for p in TESTS_DIR.glob("*.py")
                 if p.name != pathlib.Path(__file__).name
                 and ("emergentagent.com" in p.read_text(encoding="utf-8", errors="ignore")
                      or "emergent.host" in p.read_text(encoding="utf-8", errors="ignore"))]
    assert offenders == []


_LIVE_FILES = ["test_iteration7.py", "test_iteration8.py", "test_iteration9.py",
               "test_iteration10.py", "test_iteration11.py", "test_iteration12.py",
               "test_dr_voice_e2e.py", "test_dr_voice_intake.py"]


def test_live_server_files_skip_without_backend_url():
    r = _run_pytest([f"tests/{f}" for f in _LIVE_FILES], {"DB_NAME": "unit_r2_noop"},
                    unset=("REACT_APP_BACKEND_URL",))
    assert "passed" not in r.stdout and "failed" not in r.stdout and "error" not in r.stdout.lower(), r.stdout
    assert f"{len(_LIVE_FILES)} skipped" in r.stdout, r.stdout
