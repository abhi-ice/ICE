"""Make backend/ importable as a flat module root so schedule_proposal's
`from models import TaskCreate` resolves during tests.

Also guards against wiping a real database: if any test that drops or wipes
database data ("destructive" marker, or a name containing "drop_all") is
selected while DB_NAME (or the database named in MONGO_URL) does not look like
a throwaway test database, the session prints a loud warning. If destructive
cleanup was explicitly switched on (ICE_ALLOW_DESTRUCTIVE_TEST_CLEANUP=1) for
such a database, the whole session is stopped before any test runs.

Throwaway database names start with test_, ice_test, unit_ or smoke, or are
listed (comma separated) in ICE_TEST_DB_ALLOWLIST.
"""
import os
import sys
import pathlib
from urllib.parse import urlparse

import pytest

_backend = pathlib.Path(__file__).resolve().parents[1]
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))

_TEST_DB_PREFIXES = ("test_", "ice_test", "unit_", "smoke")


def _target_db_names():
    names = []
    db_name = (os.environ.get("DB_NAME") or "").strip()
    if db_name:
        names.append(db_name)
    mongo_url = (os.environ.get("MONGO_URL") or "").strip()
    if mongo_url:
        try:
            path_db = urlparse(mongo_url).path.lstrip("/").split("?")[0].strip()
        except Exception:
            path_db = ""
        if path_db and path_db not in names:
            names.append(path_db)
    return names


def _looks_like_test_db(name):
    allow = {
        n.strip() for n in (os.environ.get("ICE_TEST_DB_ALLOWLIST") or "").split(",") if n.strip()
    }
    return name in allow or name.lower().startswith(_TEST_DB_PREFIXES)


def _is_destructive(item):
    return item.get_closest_marker("destructive") is not None or "drop_all" in item.name


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "destructive: test drops or wipes database data; only runs against a throwaway test database",
    )


# trylast: run after -k / -m / --deselect have removed tests, so only tests
# that will really run are checked.
@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(session, config, items):
    destructive = [it for it in items if _is_destructive(it)]
    if not destructive:
        return
    risky = [n for n in _target_db_names() if not _looks_like_test_db(n)]
    if not risky:
        return
    names = ", ".join(risky)
    tests = "\n".join(f"    {it.nodeid}" for it in destructive)
    banner = "!" * 78
    if os.environ.get("ICE_ALLOW_DESTRUCTIVE_TEST_CLEANUP") == "1":
        pytest.exit(
            f"\n{banner}\n"
            f"STOPPED: destructive cleanup is switched on, but the target database "
            f"({names}) does not look like a throwaway test database.\n"
            f"These selected tests would wipe it:\n{tests}\n"
            f"Use a database whose name starts with test_ or ice_test, or deselect these tests.\n"
            f"{banner}",
            returncode=3,
        )
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(banner, red=True, bold=True)
        reporter.write_line(
            f"WARNING: tests that wipe the database are selected while the target database "
            f"({names}) does not look like a throwaway test database. They will be skipped.",
            red=True,
            bold=True,
        )
        reporter.write_line(banner, red=True, bold=True)
