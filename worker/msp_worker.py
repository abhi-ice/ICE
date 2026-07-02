"""ICE Site Ops — Microsoft Project Windows worker (Schedule Mode 2).

This program runs on a Windows computer that has Microsoft Project installed.
It repeatedly asks the ICE backend for schedule jobs, drives Microsoft Project
through COM automation to carry them out, and posts the results back.

Contract (must stay in lockstep with the backend):
  backend/routers/ms_project_internal_router.py   claim / results / renew-lease
  backend/services/ms_project_sync.py             job + result shapes, lease rules
  backend/services/ms_project_ingest.py           pull task/dependency shapes

Job types handled (the "op" field of a claimed job):
  push      Apply ICE-owned actuals (percent complete, actual start/finish) to
            the matching Project tasks, stamp each task's Text1 with the ICE
            activity code, recalculate, save, then read the full network back.
  pull      Read Project's full network (tasks + dependency links) without
            changing or saving anything.
  validate  Open the project file, recalculate in memory, report a summary.
            Nothing is changed or saved.
  create    Build a brand-new Project file from the ICE task list (activity
            codes, durations, milestones, dependency links), upload it to
            ICE, then read the network back so ICE can stamp Project task ids.

Where the .mpp comes from:
  Hosted mode  The job carries project_file.file_id — the file lives in ICE.
               The worker downloads a working copy per job
               (GET  /api/internal/ms-project/file/<job id>), verifies its
               content hash, and, when a push saved changes (or a create made
               a new file), uploads the new version back
               (POST /api/internal/ms-project/file/<job id>) BEFORE posting
               results. Rowversion in hosted mode is "rev-<rev>#<sha12>".
  Legacy mode  No file_id on the job — the .mpp is resolved on this machine
               as before (pinned path or MSP_PROJECT_DIR) and the rowversion
               stays "<file mtime>#<sha12>" (behavior unchanged, including
               the local conflict gate).

Result shapes posted to /api/internal/ms-project/results:
  ok        {job_id, seq, worker_id, status:"ok", project_rowversion,
             project_finish, critical_count, tasks:[...], dependencies:[...]}
            Each task: {unique_id, text1, name, start, finish, critical,
            total_slack_days, duration_days, milestone, wbs} — dates are
            "YYYY-MM-DD". Each dependency (pull only): {successor_code,
            predecessor_code, type: FS|SS|FF|SF, lag_days}.
  conflict  {job_id, seq, worker_id, status:"conflict", project_rowversion}
            Posted when a push job's expected_rowversion no longer matches the
            live file (the file was edited outside ICE since the last sync).
  error     {job_id, seq, worker_id, status:"error",
             error:{code, message}}

Configuration comes from worker.env next to this script (KEY=VALUE lines);
environment variables with the same names take precedence. See
worker.env.example. The token is never printed or logged.

Run:       python msp_worker.py            (or double-click run-worker.bat)
Self test: python msp_worker.py --selftest (no network; opens + quits Project)
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import socket
import sys
import tempfile
import threading
import time
from datetime import date, datetime

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ENV_FILE = os.path.join(SCRIPT_DIR, "worker.env")
LOG_FILE = os.path.join(SCRIPT_DIR, "worker.log")

# Lease rules — must match backend/services/ms_project_sync.py (LEASE_SECONDS=300).
# We heartbeat every 60 seconds, a 5x safety margin inside the lease window.
LEASE_SECONDS = 300
RENEW_EVERY_SECONDS = 60

# The claim endpoint clamps wait_ms to 25000, so the default poll window is 20s.
MAX_WAIT_MS = 25000
DEFAULT_POLL_SECONDS = 20

# Job types this worker can execute (sent as claim "capabilities").
CAPABILITIES = ["push", "pull", "validate", "create"]

# Microsoft Project COM constants.
PJ_DO_NOT_SAVE = 0                                  # pjDoNotSave
PJ_LINK_TYPE = {0: "FF", 1: "FS", 2: "SF", 3: "SS"}  # pjTaskLinkType values
PJ_LINK_CONST = {name: num for num, name in PJ_LINK_TYPE.items()}
PJ_SNET = 4                                         # pjSNET (start no earlier than)

CONFIG_KEYS = ("ICE_BASE_URL", "MS_PROJECT_WORKER_TOKEN", "POLL_SECONDS",
               "MSP_VISIBLE", "WORKER_ID", "MSP_PROJECT_DIR", "MSP_CACHE_DIR")

log = logging.getLogger("msp_worker")


# ---------------------------------------------------------------------------
# Configuration.
# ---------------------------------------------------------------------------
def parse_env_file(path: str) -> dict:
    """Parse a KEY=VALUE file: blank lines and #-comments ignored, surrounding
    single or double quotes stripped from values."""
    out: dict = {}
    if not path or not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            if key:
                out[key] = val
    return out


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def load_config(env_file: str = DEFAULT_ENV_FILE) -> dict:
    """worker.env values first, then real environment variables override."""
    raw = parse_env_file(env_file)
    for key in CONFIG_KEYS:
        if os.environ.get(key):
            raw[key] = os.environ[key]
    try:
        poll = int(float(raw.get("POLL_SECONDS") or DEFAULT_POLL_SECONDS))
    except (TypeError, ValueError):
        poll = DEFAULT_POLL_SECONDS
    poll = max(1, min(poll, MAX_WAIT_MS // 1000))
    return {
        "base_url": (raw.get("ICE_BASE_URL") or "").strip().rstrip("/"),
        "token": (raw.get("MS_PROJECT_WORKER_TOKEN") or "").strip(),
        "poll_seconds": poll,
        "visible": _truthy(raw.get("MSP_VISIBLE") or "false"),
        "worker_id": (raw.get("WORKER_ID") or socket.gethostname() or "msp-worker").strip(),
        "project_dir": (raw.get("MSP_PROJECT_DIR")
                        or os.path.join(SCRIPT_DIR, "projects")).strip(),
        "cache_dir": (raw.get("MSP_CACHE_DIR")
                      or os.path.join(SCRIPT_DIR, "cache")).strip(),
    }


def validate_config(cfg: dict) -> list:
    problems = []
    if not cfg["base_url"].startswith(("http://", "https://")):
        problems.append("ICE_BASE_URL is missing or is not an http(s) address")
    if not cfg["token"]:
        problems.append("MS_PROJECT_WORKER_TOKEN is missing "
                        "(copy it from ICE: Settings -> Microsoft Project connector)")
    return problems


# ---------------------------------------------------------------------------
# Backend API client (never logs the token).
# ---------------------------------------------------------------------------
class Api:
    def __init__(self, base_url: str, token: str):
        self._base = base_url
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {token}"})

    def claim(self, worker_id: str, wait_ms: int):
        """Long-poll for the next job. Returns the job dict, or None on 204."""
        resp = self._session.post(
            f"{self._base}/api/internal/ms-project/claim",
            json={"worker_id": worker_id, "capabilities": CAPABILITIES,
                  "wait_ms": wait_ms},
            timeout=(wait_ms / 1000.0) + 15,
        )
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        return resp.json()

    def renew_lease(self, job_id: str) -> bool:
        resp = self._session.post(
            f"{self._base}/api/internal/ms-project/renew-lease",
            json={"job_id": job_id}, timeout=30)
        resp.raise_for_status()
        return bool(resp.json().get("renewed"))

    def post_result(self, payload: dict):
        """Post a job result; retry with backoff. Returns the backend's reply
        dict, or None when every attempt failed (the lease reaper will requeue
        the job — pushing again is idempotent by contract)."""
        last_err = None
        for attempt in range(5):
            try:
                resp = self._session.post(
                    f"{self._base}/api/internal/ms-project/results",
                    json=payload, timeout=60)
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                last_err = exc
                wait = min(2 ** (attempt + 1), 20)
                log.warning("posting result for job %s failed (attempt %d/5): %s "
                            "— retrying in %ss", payload.get("job_id"),
                            attempt + 1, exc, wait)
                time.sleep(wait)
        log.error("giving up posting result for job %s: %s",
                  payload.get("job_id"), last_err)
        return None

    def download_file(self, job_id: str):
        """Hosted mode: GET the stored .mpp for a claimed job. Returns
        (bytes, headers) — headers carry X-File-Sha and X-File-Rev — or None
        when ICE has no stored file for the job's project (404)."""
        resp = self._session.get(
            f"{self._base}/api/internal/ms-project/file/{job_id}", timeout=300)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content, resp.headers

    def upload_file(self, job_id: str, data: bytes, sha: str):
        """Hosted mode: POST a changed/new .mpp back as the next stored
        version; ICE recomputes the hash from X-File-Sha and replies
        {"rev": n}. Returns the new revision number, the string "rejected"
        when ICE refused the upload for good (409 superseded base / 413 /
        422 — retrying can never succeed), or None when all 3 attempts
        failed on transient errors (the caller must then post an error
        result — a result may never claim a file version ICE lacks)."""
        last_err = None
        for attempt in range(3):
            try:
                resp = self._session.post(
                    f"{self._base}/api/internal/ms-project/file/{job_id}",
                    data=data,
                    headers={"X-File-Sha": sha,
                             "Content-Type": "application/vnd.ms-project"},
                    timeout=300)
                if resp.status_code in (409, 413, 422):
                    log.warning("ICE rejected the file for job %s (%d): %s",
                                job_id, resp.status_code, resp.text[:200])
                    return "rejected"
                resp.raise_for_status()
                return int(resp.json().get("rev"))
            except (requests.RequestException, TypeError, ValueError) as exc:
                last_err = exc
                wait = min(2 ** (attempt + 1), 20)
                log.warning("uploading file for job %s failed (attempt %d/3): "
                            "%s — retrying in %ss", job_id, attempt + 1, exc,
                            wait)
                time.sleep(wait)
        log.error("giving up uploading file for job %s: %s", job_id, last_err)
        return None


class LeaseRenewer(threading.Thread):
    """Heartbeats /renew-lease every RENEW_EVERY_SECONDS while a job runs, so
    the backend reaper does not hand the job to another worker."""

    def __init__(self, api: Api, job_id: str):
        super().__init__(daemon=True, name=f"lease-{job_id[:8]}")
        self._api = api
        self._job_id = job_id
        self._stop = threading.Event()

    def run(self):
        while not self._stop.wait(RENEW_EVERY_SECONDS):
            try:
                if not self._api.renew_lease(self._job_id):
                    log.warning("lease renewal declined for job %s "
                                "(no longer claimed?)", self._job_id)
            except Exception as exc:
                log.warning("lease renewal failed for job %s: %s",
                            self._job_id, exc)

    def stop(self):
        self._stop.set()


# ---------------------------------------------------------------------------
# Rowversion: "file-mtime#hash" of the .mpp on disk (see backend models.py,
# external_rowversion). A push whose expected_rowversion differs from the live
# value means the file was edited outside ICE -> report a conflict.
# ---------------------------------------------------------------------------
def file_rowversion(path: str):
    if not path or not os.path.exists(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return f"{int(os.path.getmtime(path))}#{digest.hexdigest()[:12]}"


def sha12(data: bytes) -> str:
    """Content hash used by the hosted-file contract (sha256 hex, first 12)."""
    return hashlib.sha256(data).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Hosted project files: when a job carries project_file.file_id, the .mpp
# lives in ICE object storage. The worker downloads a working copy per job
# into <cache dir>\<ICE project id>\rev-<n>.mpp and, after a push that saved
# (or a create), uploads the new version back BEFORE posting results. Hosted
# rowversion is "rev-<rev>#<sha12>" of the STORED file, so the backend's
# file-rev guard replaces the local mtime conflict gate.
# ---------------------------------------------------------------------------
DOWNLOAD_CORRUPT = object()  # sentinel: bytes failed their hash check twice


def cache_path(cfg: dict, logical_id: str, filename: str) -> str:
    folder = os.path.join(cfg["cache_dir"], logical_id or "project")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, filename)


def prune_cache(cfg: dict, logical_id: str, keep: int = 3):
    """Drop all but the newest `keep` cached copies for a project — one ~.mpp
    lands per rev, so an active project would otherwise grow the cache
    without bound. Best-effort: a locked file is just left for next time."""
    folder = os.path.join(cfg["cache_dir"], logical_id or "project")
    try:
        mpps = [os.path.join(folder, n) for n in os.listdir(folder)
                if n.lower().endswith(".mpp")]
    except OSError:
        return
    mpps.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for stale in mpps[keep:]:
        try:
            os.remove(stale)
        except OSError:
            pass


def fetch_hosted_file(api: Api, job: dict, cfg: dict):
    """Download the stored .mpp for this job and verify its content hash
    (against the X-File-Sha header, falling back to the job's file_sha).
    Returns (local_path, rev, sha) on success; None when ICE has no stored
    file (the caller falls back to the legacy local path); DOWNLOAD_CORRUPT
    when the bytes failed the hash check twice in a row."""
    pf = job.get("project_file") or {}
    logical = str(pf.get("logical_id") or job.get("project_id") or "project").strip()
    for attempt in (1, 2):
        got = api.download_file(job["id"])
        if got is None:
            return None
        data, headers = got
        sha = sha12(data)
        expected = str(headers.get("X-File-Sha") or pf.get("file_sha") or "").strip()
        if expected and sha != expected:
            log.warning("job %s: downloaded file hash %s does not match "
                        "expected %s (attempt %d/2)", job["id"], sha,
                        expected, attempt)
            continue
        rev = headers.get("X-File-Rev") or pf.get("file_rev") or 0
        try:
            rev = int(rev)
        except (TypeError, ValueError):
            rev = 0
        path = cache_path(cfg, logical, f"rev-{rev}.mpp")
        with open(path, "wb") as fh:
            fh.write(data)
        return path, rev, sha
    return DOWNLOAD_CORRUPT


# ---------------------------------------------------------------------------
# Date helpers. The backend parses dates with date.fromisoformat(str[:10]),
# so "YYYY-MM-DD" is the wire format in both directions.
# ---------------------------------------------------------------------------
def com_date_str(value):
    """COM date -> 'YYYY-MM-DD' or None. Project returns the string 'NA' for
    unset actual dates."""
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s or s.upper() == "NA":
            return None
        try:
            return date.fromisoformat(s[:10]).isoformat()
        except ValueError:
            return None
    try:
        return date(value.year, value.month, value.day).isoformat()
    except Exception:
        return None


def parse_wire_date(value):
    """ISO string from the backend -> naive datetime at midnight, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Microsoft Project COM session.
# ---------------------------------------------------------------------------
class MsProject:
    """One COM session with Microsoft Project. Use as a context manager: the
    file is always closed WITHOUT saving and the application always quits;
    call save() explicitly (push only) before leaving the block to persist."""

    def __init__(self, visible: bool = False):
        self.visible = visible
        self.app = None
        self.proj = None

    def __enter__(self):
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        self.app = win32com.client.DispatchEx("MSProject.Application")
        try:
            self.app.Visible = bool(self.visible)
        except Exception:
            pass
        # Suppress modal alert dialogs so automation never hangs on a prompt.
        try:
            self.app.DisplayAlerts = False
        except Exception:
            pass
        try:
            self.app.Alerts(False)
        except Exception:
            pass
        return self

    def __exit__(self, exc_type, exc, tb):
        import pythoncom
        try:
            if self.app is not None:
                try:
                    self.app.FileCloseEx(PJ_DO_NOT_SAVE)
                except Exception:
                    pass
                try:
                    self.app.Quit()
                except Exception:
                    pass
        finally:
            self.app = None
            self.proj = None
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
        return False  # never swallow exceptions

    # -- file / project ----------------------------------------------------
    def open(self, path: str, read_only: bool = False):
        if not self.app.FileOpenEx(path, read_only):
            raise RuntimeError(f"Microsoft Project could not open: {path}")
        self.proj = self.app.ActiveProject
        return self.proj

    def save(self):
        self.app.FileSave()

    def save_as(self, path: str):
        self.app.FileSaveAs(path)

    def new_project(self, start_date=None):
        """Create op: open a brand-new blank project instead of a file."""
        try:
            self.app.FileNew()
            self.proj = self.app.ActiveProject
        except Exception:
            self.proj = self.app.Projects.Add()
        if self.proj is None:
            raise RuntimeError("Microsoft Project could not create a new project")
        start = parse_wire_date(start_date)
        if start is not None:
            try:
                self.proj.ProjectStart = start.replace(hour=8)
            except Exception as exc:
                log.warning("could not set the new project's start date: %s", exc)
        return self.proj

    def recalc(self):
        try:
            self.app.CalculateAll()
        except Exception:
            try:
                self.app.CalculateProject()
            except Exception as exc:
                log.warning("recalculate failed (continuing with Project's "
                            "current values): %s", exc)

    def minutes_per_day(self) -> float:
        """Project duration/slack/lag properties return MINUTES; convert to
        days using the project calendar's hours-per-day (default 8h = 480)."""
        try:
            hpd = float(self.proj.HoursPerDay) or 8.0
        except Exception:
            hpd = 8.0
        return hpd * 60.0

    def real_tasks(self):
        """Non-blank, non-summary, non-external tasks (real activities)."""
        out = []
        for t in self.proj.Tasks:
            if t is None:  # blank rows enumerate as None
                continue
            try:
                if t.Summary:
                    continue
            except Exception:
                pass
            try:
                if t.ExternalTask:
                    continue
            except Exception:
                pass
            out.append(t)
        return out

    # -- read the network back (push read-back and pull use the same shape) --
    def serialize(self):
        """Return (tasks, dependencies, project_finish, critical_count) in the
        exact shapes services/ms_project_sync._ingest_push_tasks and
        services/ms_project_ingest.worker_tasks_to_parsed_rows consume."""
        mpd = self.minutes_per_day()
        tasks, deps = [], []

        def code_of(task):
            try:
                c = str(task.Text1 or "").strip()
            except Exception:
                c = ""
            if c:
                return c
            try:
                return str(task.Name or "").strip()
            except Exception:
                return ""

        for t in self.real_tasks():
            try:
                uid = int(t.UniqueID)
                name = str(t.Name or "")
                try:
                    text1 = str(t.Text1 or "").strip()
                except Exception:
                    text1 = ""
                try:
                    wbs = str(t.WBS or "")
                except Exception:
                    wbs = ""
                tasks.append({
                    "unique_id": uid,
                    "text1": text1,
                    "name": name,
                    "start": com_date_str(t.Start),
                    "finish": com_date_str(t.Finish),
                    "critical": bool(t.Critical),
                    "total_slack_days": round(float(t.TotalSlack) / mpd, 4),
                    "duration_days": round(float(t.Duration) / mpd, 4),
                    "milestone": bool(t.Milestone),
                    "wbs": wbs,
                })
            except Exception as exc:
                log.warning("skipping unreadable task row: %s", exc)
                continue
            # Dependency links where THIS task is the successor (each link is
            # enumerated on both endpoint tasks; keeping To==self dedupes).
            try:
                links = t.TaskDependencies
            except Exception:
                links = None
            if links is None:
                continue
            for dep in links:
                try:
                    if int(dep.To.UniqueID) != uid:
                        continue
                    deps.append({
                        "successor_code": code_of(t),
                        "predecessor_code": code_of(dep.From),
                        "type": PJ_LINK_TYPE.get(int(dep.Type), "FS"),
                        "lag_days": round(float(dep.Lag) / mpd, 4),
                    })
                except Exception as exc:
                    log.warning("skipping unreadable dependency on '%s': %s",
                                name, exc)
        project_finish = com_date_str(self.proj.ProjectFinish)
        critical_count = sum(1 for x in tasks if x["critical"])
        return tasks, deps, project_finish, critical_count

    # -- push: apply ICE-owned actuals --------------------------------------
    def apply_task_updates(self, updates: list):
        """Apply a push change-set's task_updates. Matching order per task:
        1) Project UniqueID == external_task_id (deterministic, once linked)
        2) Text1 == activity_code (the stamped ICE code)
        3) Name  == activity_code (first-time link fallback; Text1 then stamped)
        Only values that actually differ are written, so replaying the same
        push is a no-op and the file is not dirtied (idempotent by contract).
        Returns (changed_count, unmatched_codes)."""
        by_uid, by_text1, by_name = {}, {}, {}
        for t in self.real_tasks():
            try:
                by_uid[int(t.UniqueID)] = t
                c = str(t.Text1 or "").strip()
                if c:
                    by_text1.setdefault(c, t)
                n = str(t.Name or "").strip()
                if n:
                    by_name.setdefault(n, t)
            except Exception:
                continue

        changed, unmatched = 0, []
        for upd in updates or []:
            code = str(upd.get("activity_code") or "").strip()
            task = None
            ext = upd.get("external_task_id")
            if ext is not None:
                try:
                    task = by_uid.get(int(ext))
                except (TypeError, ValueError):
                    task = None
            if task is None and code:
                task = by_text1.get(code) or by_name.get(code)
            if task is None:
                unmatched.append(code or str(ext))
                continue
            try:
                dirty = False
                # Stamp text fields (Text1 <- activity_code) so the read-back
                # and all future syncs match deterministically.
                for field, val in (upd.get("set_text_field") or {}).items():
                    if str(getattr(task, field) or "") != str(val):
                        setattr(task, field, str(val))
                        dirty = True
                # ICE only asserts the actuals it has; it never clears
                # Project's actuals (None -> skip). 8:00/17:00 mirror Project's
                # default working-day bounds; ingest compares date parts only.
                a_start = parse_wire_date(upd.get("actual_start"))
                if (a_start is not None
                        and com_date_str(task.ActualStart) != a_start.strftime("%Y-%m-%d")):
                    task.ActualStart = a_start.replace(hour=8)
                    dirty = True
                pct = upd.get("percent_complete")
                if pct is not None:
                    try:
                        pct_i = max(0, min(100, int(round(float(pct)))))
                    except (TypeError, ValueError):
                        pct_i = None
                    if pct_i is not None and int(task.PercentComplete) != pct_i:
                        task.PercentComplete = pct_i
                        dirty = True
                a_fin = parse_wire_date(upd.get("actual_finish"))
                if (a_fin is not None
                        and com_date_str(task.ActualFinish) != a_fin.strftime("%Y-%m-%d")):
                    task.ActualFinish = a_fin.replace(hour=17)
                    dirty = True
                if dirty:
                    changed += 1
            except Exception as exc:
                log.warning("could not update task '%s': %s", code, exc)
                unmatched.append(code)
        return changed, unmatched

    # -- create: build a brand-new network from the ICE task list ------------
    def build_network(self, task_specs: list):
        """Create op: add every task, then link the network. Each spec is
        {activity_code, name, duration_days, planned_start, planned_finish,
        is_milestone, predecessors:[{code, type, lag_days}]} (see
        services/ms_project_sync.enqueue_create_job). Tasks with no
        predecessors get a start-no-earlier-than constraint at planned_start
        so the ICE dates survive the recalculation. Returns
        (created_count, skipped_codes)."""
        mpd = self.minutes_per_day()
        by_code, skipped = {}, []
        for spec in task_specs or []:
            code = str(spec.get("activity_code") or "").strip()
            if not code:
                continue  # backend only sends coded tasks; belt and braces
            try:
                t = self.proj.Tasks.Add(str(spec.get("name") or code))
            except Exception as exc:
                log.warning("could not add task '%s': %s", code, exc)
                skipped.append(code)
                continue
            try:
                t.Manual = False  # auto-schedule, or the recalc can't move dates
            except Exception:
                pass  # older Project versions have no manual scheduling
            t.Text1 = code  # the stamp every future push/pull matches on
            try:
                # 0 must STAY 0 — milestones are zero-duration by contract;
                # only a missing value defaults to 1 day ("or" would eat the 0
                # and push every milestone's successors a day late).
                dur = spec.get("duration_days")
                t.Duration = int(round(float(1 if dur is None else dur) * mpd))
            except Exception as exc:
                log.warning("could not set duration on '%s': %s", code, exc)
            if spec.get("is_milestone"):
                try:
                    t.Milestone = True
                except Exception as exc:
                    log.warning("could not flag '%s' as a milestone: %s", code, exc)
            if not (spec.get("predecessors") or []):
                start = parse_wire_date(spec.get("planned_start"))
                if start is not None:
                    try:
                        t.ConstraintType = PJ_SNET
                        t.ConstraintDate = start.replace(hour=8)
                    except Exception as exc:
                        log.warning("could not set the start constraint on "
                                    "'%s': %s", code, exc)
            by_code[code] = t
        # Second pass: links, once every endpoint task exists.
        for spec in task_specs or []:
            succ = by_code.get(str(spec.get("activity_code") or "").strip())
            if succ is None:
                continue
            for pred_spec in spec.get("predecessors") or []:
                pcode = str(pred_spec.get("code") or "").strip()
                pred = by_code.get(pcode)
                if pred is None:
                    log.warning("task '%s': predecessor '%s' is not in this "
                                "project — link skipped",
                                spec.get("activity_code"), pcode)
                    continue
                link_type = PJ_LINK_CONST.get(
                    str(pred_spec.get("type") or "FS").upper(), PJ_LINK_CONST["FS"])
                try:
                    lag = int(round(float(pred_spec.get("lag_days") or 0) * mpd))
                except (TypeError, ValueError):
                    lag = 0
                try:
                    succ.TaskDependencies.Add(pred, link_type, lag)
                except Exception as exc:
                    log.warning("could not link '%s' -> '%s': %s",
                                pcode, spec.get("activity_code"), exc)
        return len(by_code), skipped


# ---------------------------------------------------------------------------
# Job execution.
# ---------------------------------------------------------------------------
def resolve_project_file(job: dict, cfg: dict):
    """The job's project_file.expected_fullname (set by an ICE admin) wins;
    otherwise <MSP_PROJECT_DIR>\\<logical project id>.mpp."""
    pf = job.get("project_file") or {}
    if pf.get("expected_fullname"):
        return str(pf["expected_fullname"])
    logical = str(pf.get("logical_id") or job.get("project_id") or "").strip()
    if not logical:
        return None
    return os.path.join(cfg["project_dir"], f"{logical}.mpp")


def execute_job(job: dict, cfg: dict, api: Api) -> dict:
    """Run one claimed job and return the /results payload."""
    op = job.get("op")
    base = {"job_id": job["id"], "seq": job.get("seq"),
            "worker_id": cfg["worker_id"]}
    if op not in CAPABILITIES:
        return {**base, "status": "error",
                "error": {"code": "unsupported_op",
                          "message": f"worker cannot handle op '{op}'"}}

    if op == "create":
        return execute_create(job, cfg, api, base)

    # Hosted mode: the .mpp lives in ICE — work on a downloaded copy.
    if (job.get("project_file") or {}).get("file_id"):
        fetched = fetch_hosted_file(api, job, cfg)
        if fetched is DOWNLOAD_CORRUPT:
            return {**base, "status": "error",
                    "error": {"code": "file_download_corrupt",
                              "message": "the Microsoft Project file downloaded "
                                         "from ICE did not match its expected "
                                         "content hash (tried twice)"}}
        if fetched is not None:
            return execute_hosted_job(job, cfg, api, base, *fetched)
        log.warning("job %s: ICE has no stored file for this project — "
                    "falling back to the legacy local file", job["id"])

    # Legacy mode (no stored file): the .mpp is a local file on this machine.
    path = resolve_project_file(job, cfg)
    if not path or not os.path.exists(path):
        return {**base, "status": "error",
                "error": {"code": "project_file_not_found",
                          "message": (f"Microsoft Project file not found: "
                                      f"{path or '(unresolved)'}. Put the .mpp at "
                                      f"{cfg['project_dir']}\\<ICE project id>.mpp "
                                      f"or set the file path on the ICE project.")}}

    # Conflict gate (push only): if the file changed since ICE last ingested it
    # (rowversion moved), do NOT touch Project — the PM pulls to reconcile first.
    live_rv = file_rowversion(path)
    expected_rv = (job.get("project_file") or {}).get("expected_rowversion")
    if op == "push" and expected_rv and live_rv != expected_rv:
        log.warning("job %s: %s was edited outside ICE (rowversion %s, expected "
                    "%s) — reporting conflict", job["id"], os.path.basename(path),
                    live_rv, expected_rv)
        return {**base, "status": "conflict", "project_rowversion": live_rv}

    change_set = job.get("change_set") or {}
    applied, unmatched = 0, []
    with MsProject(visible=cfg["visible"]) as mp:
        mp.open(path, read_only=(op != "push"))
        if op == "push":
            if change_set.get("dependency_ops"):
                log.warning("job %s: dependency_ops are not part of the push "
                            "contract (network is Project-owned); ignoring",
                            job["id"])
            applied, unmatched = mp.apply_task_updates(
                change_set.get("task_updates") or [])
            log.info("job %s: applied changes to %d task(s), %d unmatched",
                     job["id"], applied, len(unmatched))
        if change_set.get("recalc", True):
            mp.recalc()
        tasks, deps, project_finish, critical_count = mp.serialize()
        if op == "push" and applied:
            mp.save()  # the context manager then closes without further saving
    if op == "push" and applied:
        live_rv = file_rowversion(path)  # our own save is the new sync basis

    result = {**base, "status": "ok", "project_rowversion": live_rv,
              "project_finish": project_finish, "critical_count": critical_count,
              "tasks": tasks}
    if op == "pull":
        result["dependencies"] = deps
    if unmatched:
        # Informational; the backend ignores unknown keys. These are ICE
        # activities with no matching Project task (fix Text1/names in the file).
        result["unmatched_codes"] = unmatched
    return result


def execute_hosted_job(job: dict, cfg: dict, api: Api, base: dict,
                       path: str, rev: int, file_sha: str) -> dict:
    """push/pull/validate against the downloaded working copy. A push that
    saved uploads the new version back to ICE BEFORE the result is posted;
    pull/validate never write or upload. There is no local conflict gate
    here: the stored file cannot be edited behind ICE's back, and the
    backend's file-rev guard rejects stale ingests instead."""
    op = job.get("op")
    change_set = job.get("change_set") or {}
    applied, unmatched = 0, []
    with MsProject(visible=cfg["visible"]) as mp:
        mp.open(path, read_only=(op != "push"))
        if op == "push":
            if change_set.get("dependency_ops"):
                log.warning("job %s: dependency_ops are not part of the push "
                            "contract (network is Project-owned); ignoring",
                            job["id"])
            applied, unmatched = mp.apply_task_updates(
                change_set.get("task_updates") or [])
            log.info("job %s: applied changes to %d task(s), %d unmatched",
                     job["id"], applied, len(unmatched))
        if change_set.get("recalc", True):
            mp.recalc()
        tasks, deps, project_finish, critical_count = mp.serialize()
        if op == "push" and applied:
            mp.save()
    if op == "push" and applied:
        with open(path, "rb") as fh:
            data = fh.read()
        file_sha = sha12(data)
        new_rev = api.upload_file(job["id"], data, file_sha)
        if new_rev == "rejected":
            # A newer version was uploaded in ICE while this job ran; ICE
            # re-enqueues a fresh push against it (self-heal), so this job
            # just reports why it stopped.
            return {**base, "status": "error",
                    "error": {"code": "file_upload_rejected",
                              "message": "ICE refused the updated file — a "
                                         "newer version exists there"}}
        if new_rev is None:
            return {**base, "status": "error",
                    "error": {"code": "file_upload_failed",
                              "message": "could not upload the updated "
                                         "Microsoft Project file to ICE "
                                         "(tried 3 times)"}}
        rev = new_rev
        pf = job.get("project_file") or {}
        prune_cache(cfg, str(pf.get("logical_id") or job.get("project_id") or "project").strip())

    result = {**base, "status": "ok",
              "project_rowversion": f"rev-{rev}#{file_sha}",
              "project_finish": project_finish, "critical_count": critical_count,
              "tasks": tasks}
    if op == "pull":
        result["dependencies"] = deps
    if unmatched:
        result["unmatched_codes"] = unmatched
    return result


def execute_create(job: dict, cfg: dict, api: Api, base: dict) -> dict:
    """CREATE: build a brand-new Project file from the ICE task list, upload
    it to ICE (stored as source 'generated'), and post the read-back in the
    same shape as a push result so the ingest stamps every task's Project
    UniqueID as external_task_id."""
    change_set = job.get("change_set") or {}
    project_spec = change_set.get("project") or {}
    task_specs = change_set.get("tasks") or []
    logical = str((job.get("project_file") or {}).get("logical_id")
                  or job.get("project_id") or "project").strip()
    path = cache_path(cfg, logical, "generated.mpp")
    try:
        if os.path.exists(path):
            os.remove(path)  # leftover from an earlier attempt
    except OSError:
        pass
    with MsProject(visible=cfg["visible"]) as mp:
        mp.new_project(project_spec.get("start_date"))
        built, skipped = mp.build_network(task_specs)
        log.info("job %s: built a new Microsoft Project file with %d task(s)"
                 "%s", job["id"], built,
                 f", {len(skipped)} skipped" if skipped else "")
        mp.recalc()
        mp.save_as(path)
        tasks, _deps, project_finish, critical_count = mp.serialize()
    with open(path, "rb") as fh:
        data = fh.read()
    file_sha = sha12(data)
    rev = api.upload_file(job["id"], data, file_sha)
    if rev == "rejected":
        # Someone uploaded a real .mpp while this file was being generated;
        # ICE keeps the uploaded one, so the generated file is discarded.
        return {**base, "status": "error",
                "error": {"code": "file_upload_rejected",
                          "message": "ICE refused the generated file — a "
                                     "real file was uploaded meanwhile"}}
    if rev is None:
        return {**base, "status": "error",
                "error": {"code": "file_upload_failed",
                          "message": "could not upload the generated "
                                     "Microsoft Project file to ICE "
                                     "(tried 3 times)"}}
    prune_cache(cfg, logical)
    result = {**base, "status": "ok",
              "project_rowversion": f"rev-{rev}#{file_sha}",
              "project_finish": project_finish, "critical_count": critical_count,
              "tasks": tasks}
    if skipped:
        result["unmatched_codes"] = skipped  # informational, like push
    return result


def execute_with_retry(job: dict, cfg: dict, api: Api) -> dict:
    """COM can fail transiently (busy server, RPC hiccup): retry exactly once
    with a fresh Project instance; report the error result if that also fails."""
    try:
        return execute_job(job, cfg, api)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        log.warning("job %s attempt failed (%s: %s) — retrying once with a "
                    "fresh Microsoft Project instance",
                    job["id"], type(exc).__name__, exc)
        time.sleep(3)
        try:
            return execute_job(job, cfg, api)
        except KeyboardInterrupt:
            raise
        except Exception as exc2:
            log.error("job %s failed after retry: %s: %s",
                      job["id"], type(exc2).__name__, exc2)
            return {"job_id": job["id"], "seq": job.get("seq"),
                    "worker_id": cfg["worker_id"], "status": "error",
                    "error": {"code": type(exc2).__name__,
                              "message": str(exc2)[:2000]}}


# ---------------------------------------------------------------------------
# Main loop.
# ---------------------------------------------------------------------------
def run(cfg: dict) -> None:
    api = Api(cfg["base_url"], cfg["token"])
    wait_ms = min(cfg["poll_seconds"] * 1000, MAX_WAIT_MS)
    backoff = 5
    log.info("worker '%s' polling %s (window %ss, capabilities: %s, "
             "project dir: %s, cache dir: %s)", cfg["worker_id"],
             cfg["base_url"], wait_ms // 1000, ", ".join(CAPABILITIES),
             cfg["project_dir"], cfg["cache_dir"])
    while True:
        try:
            job = api.claim(cfg["worker_id"], wait_ms)
            backoff = 5
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            if code == 401:
                log.error("claim rejected (401): the worker token is wrong — "
                          "re-copy MS_PROJECT_WORKER_TOKEN into worker.env")
            elif code == 503:
                log.error("claim unavailable (503): the ICE backend does not "
                          "have its Microsoft Project connector token set yet")
            else:
                log.error("claim failed (HTTP %s)", code)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        except requests.RequestException as exc:
            log.error("cannot reach ICE at %s (%s) — retrying in %ss",
                      cfg["base_url"], exc.__class__.__name__, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        if not job:
            time.sleep(1)  # long poll returned empty; breathe, then re-poll
            continue

        log.info("claimed job %s (op=%s seq=%s project=%s attempt=%s)",
                 job.get("id"), job.get("op"), job.get("seq"),
                 job.get("project_id"), job.get("attempt"))
        renewer = LeaseRenewer(api, job["id"])
        renewer.start()
        try:
            result = execute_with_retry(job, cfg, api)
        finally:
            renewer.stop()
        reply = api.post_result(result)
        log.info("job %s done: status=%s, backend replied: %s",
                 job["id"], result.get("status"), reply)


# ---------------------------------------------------------------------------
# Self test (--selftest): config parsing + COM availability. NO network.
# ---------------------------------------------------------------------------
def selftest() -> int:
    all_ok = True

    # 1) Config parsing against a dummy worker.env.
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dummy = os.path.join(tmp, "worker.env")
            with open(dummy, "w", encoding="utf-8") as fh:
                fh.write(
                    "# dummy config for --selftest\n"
                    "\n"
                    "ICE_BASE_URL = https://example.invalid/ \n"
                    'MS_PROJECT_WORKER_TOKEN="dummy-token-for-selftest"\n'
                    "POLL_SECONDS=7\n"
                    "MSP_VISIBLE=true\n"
                    "WORKER_ID='rig-01'\n")
            # Shield the parse from any real env vars, then restore them.
            saved = {k: os.environ.pop(k, None) for k in CONFIG_KEYS}
            try:
                cfg = load_config(dummy)
            finally:
                for k, v in saved.items():
                    if v is not None:
                        os.environ[k] = v
            assert cfg["base_url"] == "https://example.invalid", cfg["base_url"]
            assert cfg["token"] == "dummy-token-for-selftest"
            assert cfg["poll_seconds"] == 7
            assert cfg["visible"] is True
            assert cfg["worker_id"] == "rig-01"
            assert cfg["cache_dir"] == os.path.join(SCRIPT_DIR, "cache"), cfg["cache_dir"]
            assert validate_config(cfg) == []
        print("PASS  config parsing (KEY=VALUE, comments, quotes, defaults, env override shield)")
    except Exception as exc:
        all_ok = False
        print(f"FAIL  config parsing: {type(exc).__name__}: {exc}")

    # 2) COM automation: actually open and quit Microsoft Project (not visible).
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        app_name, app_version = "", ""
        try:
            app = win32com.client.DispatchEx("MSProject.Application")
            try:
                app.Visible = False
            except Exception:
                pass
            app_name = str(app.Name)
            try:
                app_version = str(app.Version)
            except Exception:
                pass
            try:
                app.Quit()
            except Exception:
                pass
        finally:
            pythoncom.CoUninitialize()
        print(f"PASS  COM automation ({app_name} {app_version} opened and quit, visible=false)")
    except Exception as exc:
        all_ok = False
        print(f"FAIL  COM automation (is Microsoft Project installed, and pywin32? "
              f"try: pip install pywin32): {type(exc).__name__}: {exc}")

    # 3) By construction this self test makes zero HTTP calls.
    print("PASS  no network contacted (selftest never calls the ICE backend)")

    print("SELFTEST " + ("PASSED" if all_ok else "FAILED"))
    return 0 if all_ok else 1


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------
def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    log.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)
    try:
        filelog = logging.FileHandler(LOG_FILE, encoding="utf-8")
        filelog.setFormatter(fmt)
        log.addHandler(filelog)
    except OSError:
        pass  # console-only if the log file cannot be opened


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="ICE Site Ops — Microsoft Project worker (Schedule Mode 2)")
    parser.add_argument("--selftest", action="store_true",
                        help="check config parsing and Microsoft Project COM; "
                             "makes no network calls")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE,
                        help="path to worker.env (default: next to this script)")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    setup_logging()
    cfg = load_config(args.env_file)
    problems = validate_config(cfg)
    if problems:
        for p in problems:
            log.error("configuration problem: %s", p)
        log.error("Fix worker.env (start from worker.env.example) and run again.")
        return 2
    log.info("configuration OK (base=%s, worker_id=%s, poll=%ss, visible=%s, "
             "token=<set, hidden>)", cfg["base_url"], cfg["worker_id"],
             cfg["poll_seconds"], cfg["visible"])
    try:
        run(cfg)
    except KeyboardInterrupt:
        log.info("Ctrl+C received — worker stopped cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
