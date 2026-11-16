"""Railway move (2026-11-16): stored-file and generic-edit hardening.

Covers:
  * services.storage.key_belongs_to_tenant -- a stored file key is only
    read when its company folder is the caller's company; path tricks are
    refused; keys under an older app name still pass;
  * GET /photos/{id}/file, GET /photo-tray/{id}/file and the room
    auto-detect plan read answer 404 (and never touch storage) when the row
    points at another company's file;
  * PhotoCreate refuses a storage_path that is not a server-made key;
  * PATCH /crud/{entity}/{id}: field names starting with "$", holding "."
    or a NUL character give 400; server-owned fields (file keys, company,
    lock state, hashes) are dropped; database errors give 400; normal
    screen edits (including people.role and crews.status) still save;
  * uploads are read in chunks with a running 25 MB limit (413), using the
    size the upload reports when it has one;
  * image metadata refuses decompression bombs but allows 48 MP phone
    photos, and a failure there never stops the photo being saved.

No network, no real Mongo: a small in-memory fake database stands in.
"""
import asyncio
import io

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from services import storage
from routers import iter3_router, iter8_router, photo_tray_router
from models import PhotoCreate


TID = "tenant-a"
OTHER = "tenant-b"
CTX = {"tenant_id": TID, "user": {"id": "u1", "role": "superintendent"},
       "request_id": "r1"}

OWN_KEY = f"ice-schedule-ops/photos/{TID}/abc.jpg"
OTHER_KEY = f"ice-schedule-ops/photos/{OTHER}/abc.jpg"


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------- fakes
def _match(doc, flt):
    for k, v in (flt or {}).items():
        if isinstance(v, dict):
            for op, arg in v.items():
                if op == "$in" and doc.get(k) not in arg:
                    return False
                if op == "$ne" and doc.get(k) == arg:
                    return False
                if op == "$gte" and not (doc.get(k) is not None and doc.get(k) >= arg):
                    return False
        elif doc.get(k) != v:
            return False
    return True


class _Res:
    def __init__(self, n):
        self.matched_count = n
        self.modified_count = n


class _Coll:
    def __init__(self):
        self.docs = []
        self.updates = []
        self.raise_on_update = None

    async def find_one(self, flt, proj=None):
        for d in self.docs:
            if _match(d, flt):
                return dict(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def update_one(self, flt, upd):
        if self.raise_on_update:
            raise self.raise_on_update
        self.updates.append(upd)
        for d in self.docs:
            if _match(d, flt):
                d.update(upd.get("$set", {}))
                return _Res(1)
        return _Res(0)

    async def count_documents(self, flt):
        return len([d for d in self.docs if _match(d, flt)])


class _DB:
    def __init__(self):
        self._c = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._c.setdefault(name, _Coll())

    def __getitem__(self, name):
        return getattr(self, name)


@pytest.fixture
def db(monkeypatch):
    d = _DB()
    d.projects.docs.append({"id": "p1", "tenant_id": TID})
    d.projects.docs.append({"id": "p-other", "tenant_id": OTHER})
    for mod in (iter3_router, iter8_router, photo_tray_router):
        monkeypatch.setattr(mod, "get_db", lambda: d)
    # The photo upload now uses the shared project check (company, then
    # per-project access), which reads the database through these modules.
    from services import project_scope, project_access
    monkeypatch.setattr(project_scope, "get_db", lambda: d)
    monkeypatch.setattr(project_access, "get_db", lambda: d)
    return d


@pytest.fixture
def fake_storage(monkeypatch):
    reads, puts = [], []

    async def _get(path):
        reads.append(path)
        return b"\xff\xd8bytes", "image/jpeg"

    async def _put(path, data, ct):
        puts.append(path)
        return {}

    async def _record(**kw):
        return {}

    for mod in (iter3_router, photo_tray_router):
        monkeypatch.setattr(mod, "aget_object", _get)
        monkeypatch.setattr(mod, "aput_object", _put)
        monkeypatch.setattr(mod, "record_file", _record)
    monkeypatch.setattr(storage, "aget_object", _get)
    return {"reads": reads, "puts": puts}


class _Upload:
    """Stand-in for FastAPI's UploadFile: chunked read(n) and an optional
    reported size."""

    def __init__(self, data, filename="photo.jpg", size=None,
                 content_type="image/jpeg"):
        self._buf = io.BytesIO(data)
        self.filename = filename
        self.size = size
        self.content_type = content_type
        self.read_sizes = []

    async def read(self, n=-1):
        self.read_sizes.append(n)
        return self._buf.read(n)


def _png(w, h, mode="RGB"):
    from PIL import Image
    buf = io.BytesIO()
    Image.new(mode, (w, h)).save(buf, format="PNG")
    return buf.getvalue()


# ------------------------------------------------ key_belongs_to_tenant
@pytest.mark.parametrize("key", [
    storage.make_path(TID, "photos", "jpg"),
    storage.make_path(TID, "tray", "png"),
    storage.make_path(TID, "audio/voice", "webm"),   # kind with a slash
    f"older-app-name/photos/{TID}/abc.jpg",           # older first folder
    f"ice/tray/{TID}/abc.jpg",
])
def test_own_company_keys_pass(key):
    assert storage.key_belongs_to_tenant(key, TID)


@pytest.mark.parametrize("key", [
    OTHER_KEY,
    storage.make_path(OTHER, "photos", "jpg"),
    f"ice-schedule-ops/photos/{TID}/../{OTHER}/abc.jpg",
    f"ice-schedule-ops/photos/{OTHER}/./abc.jpg",
    f"ice-schedule-ops/{TID}/photos/{OTHER}/abc.jpg",   # company folder not right above the file
    f"/ice-schedule-ops/photos/{TID}/abc.jpg",
    f"ice-schedule-ops/photos/{TID}/abc.jpg?x=1",
    f"ice-schedule-ops/photos/{TID}/%2e%2e/abc.jpg",
    f"ice-schedule-ops/photos/{TID}/a\\b.jpg",
    f"ice-schedule-ops/photos/{TID}/a\x00.jpg",
    f"ice-schedule-ops/photos//{TID}/abc.jpg",
    f"{TID}/abc.jpg",
    "",
    None,
    123,
])
def test_other_or_odd_keys_fail(key):
    assert not storage.key_belongs_to_tenant(key, TID)


def test_empty_or_slashed_tenant_never_matches():
    assert not storage.key_belongs_to_tenant("ice-schedule-ops/photos//abc.jpg", "")
    assert not storage.key_belongs_to_tenant("a/b/c/d.jpg", "b/c")
    assert not storage.key_belongs_to_tenant(OWN_KEY, None)


# ---------------------------------------------------------- PhotoCreate
def test_photo_create_accepts_server_key_and_refuses_made_up_paths():
    ok = PhotoCreate(project_id="p1", storage_path=storage.make_path(TID, "photos", "jpg"))
    assert ok.storage_path.startswith("ice-schedule-ops/photos/")
    for bad in ("../../etc/passwd", "abc.jpg", f"/ice-schedule-ops/photos/{TID}/a.jpg",
                f"ice-schedule-ops/photos/{TID}/../x.jpg"):
        with pytest.raises(ValidationError):
            PhotoCreate(project_id="p1", storage_path=bad)


# -------------------------------------------------------- stream checks
def test_stream_photo_refuses_another_company_key(db, fake_storage):
    db.photos.docs.append({"id": "ph1", "tenant_id": TID, "storage_path": OTHER_KEY})
    with pytest.raises(HTTPException) as ei:
        _run(iter3_router.stream_photo("ph1", ctx=CTX))
    assert ei.value.status_code == 404
    assert fake_storage["reads"] == []


def test_stream_photo_own_key_works(db, fake_storage):
    db.photos.docs.append({"id": "ph1", "tenant_id": TID, "storage_path": OWN_KEY})
    resp = _run(iter3_router.stream_photo("ph1", ctx=CTX))
    assert resp.body == b"\xff\xd8bytes"
    assert fake_storage["reads"] == [OWN_KEY]


def test_stream_tray_refuses_another_company_key(db, fake_storage):
    db.photo_tray.docs.append({"id": "t1", "tenant_id": TID,
                               "storage_key": OTHER_KEY.replace("/photos/", "/tray/")})
    with pytest.raises(HTTPException) as ei:
        _run(photo_tray_router.stream_tray_image("t1", ctx=CTX))
    assert ei.value.status_code == 404
    assert fake_storage["reads"] == []


def test_stream_tray_own_key_works(db, fake_storage):
    key = OWN_KEY.replace("/photos/", "/tray/")
    db.photo_tray.docs.append({"id": "t1", "tenant_id": TID, "storage_key": key})
    resp = _run(photo_tray_router.stream_tray_image("t1", ctx=CTX))
    assert resp.status_code == 200
    assert fake_storage["reads"] == [key]


def test_auto_detect_rooms_refuses_another_company_plan(db, fake_storage):
    db.drawing_revisions.docs.append({"id": "rev1", "tenant_id": TID,
                                      "storage_path": f"ice-schedule-ops/drawings/{OTHER}/x.pdf"})
    db.floors.docs.append({"id": "f1", "tenant_id": TID})
    payload = iter8_router.AutoDetectRoomsRequest(drawing_revision_id="rev1", floor_id="f1")
    try:
        import pdf2image  # noqa: F401
    except ImportError:
        pytest.skip("pdf2image not installed")
    with pytest.raises(HTTPException) as ei:
        _run(iter8_router.auto_detect_rooms(payload, ctx=CTX))
    assert ei.value.status_code == 404
    assert fake_storage["reads"] == []


def test_tray_blob_removal_skips_another_company_key(db, monkeypatch):
    removed = []
    monkeypatch.setattr(storage, "remove_object", lambda k: removed.append(k) or "deleted")
    _run(photo_tray_router._remove_tray_blob(TID, OTHER_KEY))
    assert removed == []
    _run(photo_tray_router._remove_tray_blob(TID, OWN_KEY))
    assert removed == [OWN_KEY]


# ------------------------------------------------------------ crud_patch
class _BG:
    def add_task(self, *a, **k):
        pass


def _patch(entity, rid, body):
    return _run(iter8_router.crud_patch(entity, rid, body, _BG(), ctx=CTX))


@pytest.mark.parametrize("bad", [
    {"$where": "1"},
    {"$set": {"tenant_id": OTHER}},
    {"a.b": 1},
    {"notes\x00": "x"},
])
def test_crud_patch_refuses_bad_field_names(db, bad):
    db.photos.docs.append({"id": "ph1", "tenant_id": TID, "storage_path": OWN_KEY})
    with pytest.raises(HTTPException) as ei:
        _patch("photos", "ph1", bad)
    assert ei.value.status_code == 400
    assert db.photos.updates == []


def test_crud_patch_drops_server_owned_fields(db):
    db.photos.docs.append({"id": "ph1", "tenant_id": TID, "storage_path": OWN_KEY,
                           "sha256_hash": "aa"})
    out = _patch("photos", "ph1", {"caption": "east wall",
                                   "storage_path": OTHER_KEY,
                                   "storage_key": OTHER_KEY,
                                   "tenant_id": OTHER,
                                   "sha256_hash": "bb",
                                   "progress_lock_at": None,
                                   "is_admin": True})
    assert out["caption"] == "east wall"
    assert out["storage_path"] == OWN_KEY
    assert out["sha256_hash"] == "aa"
    assert out["tenant_id"] == TID
    assert "is_admin" not in out
    sent = db.photos.updates[0]["$set"]
    assert set(sent) == {"caption", "updated_at", "updated_by"}


def test_crud_patch_only_protected_fields_is_400(db):
    db.photos.docs.append({"id": "ph1", "tenant_id": TID, "storage_path": OWN_KEY})
    with pytest.raises(HTTPException) as ei:
        _patch("photos", "ph1", {"storage_path": OTHER_KEY})
    assert ei.value.status_code == 400
    assert db.photos.updates == []


def test_crud_patch_screen_edits_still_save(db):
    # The Resources screen sends people.role (a trade role, not a login
    # role) and crews.status; both must still save.
    db.people.docs.append({"id": "pe1", "tenant_id": TID, "role": "laborer"})
    out = _patch("people", "pe1", {"role": "foreman", "hourly_rate": 55.0, "phone": None})
    assert out["role"] == "foreman" and out["hourly_rate"] == 55.0
    db.crews.docs.append({"id": "c1", "tenant_id": TID, "status": "active"})
    out = _patch("crews", "c1", {"status": "inactive", "notes": "rain"})
    assert out["status"] == "inactive"


def test_crud_patch_database_error_is_400(db):
    from pymongo.errors import WriteError
    db.deliveries.docs.append({"id": "d1", "tenant_id": TID})
    db.deliveries.raise_on_update = WriteError("bad field name", code=52)
    with pytest.raises(HTTPException) as ei:
        _patch("deliveries", "d1", {"description": {"$bad": 1}})
    assert ei.value.status_code == 400


def test_crud_patch_non_object_body_is_400(db):
    with pytest.raises(HTTPException) as ei:
        _patch("photos", "ph1", ["x"])
    assert ei.value.status_code == 400


# ------------------------------------------------------ chunked uploads
def test_read_upload_limited_uses_reported_size_first():
    up = _Upload(b"x" * 10, size=10 ** 9)
    with pytest.raises(storage.UploadTooLarge):
        _run(storage.read_upload_limited(up, 100))
    assert up.read_sizes == []          # nothing read at all


def test_read_upload_limited_stops_at_the_limit_without_size():
    data = b"x" * (3 * 1024 * 1024)
    up = _Upload(data, size=None)
    with pytest.raises(storage.UploadTooLarge):
        _run(storage.read_upload_limited(up, 2 * 1024 * 1024))
    assert len(up.read_sizes) == 3      # stopped once over, in 1 MB chunks
    assert all(n == 1024 * 1024 for n in up.read_sizes)


def test_read_upload_limited_returns_whole_file_under_limit():
    data = b"y" * (2 * 1024 * 1024 + 5)
    assert _run(storage.read_upload_limited(_Upload(data, size=len(data)), 25 * 1024 * 1024)) == data
    assert _run(storage.read_upload_limited(_Upload(data), len(data))) == data


def _upload_photo(file, project_id="p1"):
    return _run(iter3_router.upload_photo(
        project_id=project_id, location_id=None, schedule_activity_id=None,
        daily_report_id=None, caption=None, photo_class="progress",
        drawing_pin_id=None, pin_drawing_id=None, pin_x=None, pin_y=None,
        file=file, ctx=CTX))


def test_upload_photo_over_25mb_is_413_and_nothing_stored(db, fake_storage):
    big = _Upload(b"x" * (25 * 1024 * 1024 + 1))
    with pytest.raises(HTTPException) as ei:
        _upload_photo(big)
    assert ei.value.status_code == 413
    assert fake_storage["puts"] == [] and db.photos.docs == []


def test_upload_photo_other_company_project_is_404(db, fake_storage):
    with pytest.raises(HTTPException) as ei:
        _upload_photo(_Upload(_png(10, 10)), project_id="p-other")
    assert ei.value.status_code == 404
    assert fake_storage["puts"] == []


def test_upload_photo_unreadable_image_still_saved(db, fake_storage):
    photo = _upload_photo(_Upload(b"\xff\xd8 not really a jpeg"))
    assert photo.storage_path.startswith(f"ice-schedule-ops/photos/{TID}/")
    assert len(db.photos.docs) == 1
    assert db.photos.docs[0].get("phash") is None


def test_upload_photo_reads_phash_for_a_normal_image(db, fake_storage):
    pytest.importorskip("imagehash")
    _upload_photo(_Upload(_png(64, 48)))
    assert db.photos.docs[0].get("phash")


def test_upload_to_tray_over_25mb_is_413(db, fake_storage):
    big = _Upload(b"x" * 10, size=26 * 1024 * 1024)
    with pytest.raises(HTTPException) as ei:
        _run(photo_tray_router.upload_to_tray(
            project_id="p1", capture_source="mobile_camera", file=big, bg=None, ctx=CTX))
    assert ei.value.status_code == 413
    assert fake_storage["puts"] == []


def test_upload_to_tray_other_company_project_is_404(db, fake_storage):
    with pytest.raises(HTTPException) as ei:
        _run(photo_tray_router.upload_to_tray(
            project_id="p-other", capture_source="mobile_camera",
            file=_Upload(_png(10, 10)), bg=None, ctx=CTX))
    assert ei.value.status_code == 404


# ------------------------------------------------- decompression bombs
def test_open_image_capped_allows_a_48_megapixel_phone_photo():
    img = storage.open_image_capped(_png(8000, 6000, mode="1"))
    assert img.size == (8000, 6000)


def test_open_image_capped_refuses_an_oversized_image():
    with pytest.raises(storage.ImageTooLarge):
        storage.open_image_capped(_png(9000, 9000, mode="1"))   # 81 MP


def test_tray_meta_fails_soft_on_oversized_image():
    body = _png(9000, 9000, mode="1")
    meta = photo_tray_router._extract_image_meta(body)
    assert meta["sha256"]                     # still fingerprinted by bytes
    assert meta["width"] is None and meta["phash"] is None


def test_iter3_meta_fails_soft_on_oversized_image():
    from datetime import datetime, timezone
    assert iter3_router._read_photo_meta(_png(9000, 9000, mode="1"),
                                         datetime.now(timezone.utc)) == {}


def test_pillow_global_limit_left_alone():
    from PIL import Image
    assert Image.MAX_IMAGE_PIXELS != storage.MAX_IMAGE_PIXELS


def test_open_image_capped_leaves_warning_filters_alone():
    # It runs in worker threads; swapping the process-wide warning filters
    # there could leave them changed for drawings / PDF code.
    import warnings
    before = list(warnings.filters)
    storage.open_image_capped(_png(100, 100))
    with pytest.raises(storage.ImageTooLarge):
        storage.open_image_capped(_png(9000, 9000, mode="1"))
    assert list(warnings.filters) == before


def test_open_image_capped_pillow_bomb_error_is_image_too_large():
    # Above Pillow's own hard limit (about 179 MP) Image.open raises before
    # our check runs; it must still come out as ImageTooLarge.
    with pytest.raises(storage.ImageTooLarge):
        storage.open_image_capped(_png(14000, 14000, mode="1"))   # 196 MP
