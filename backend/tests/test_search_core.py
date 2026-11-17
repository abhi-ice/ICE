"""Search everything: queue, worker, gate, listener mapping and reconcile.

Runs against the local test mongod only (DB_NAME must start with test_),
with a stub registry and a fake embedder. Voyage is never called.
"""
import asyncio
import hashlib
import os
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

pytestmark = pytest.mark.destructive

DB = os.environ.get("DB_NAME", "test_search_core")


def _uri():
    return os.environ.get("MONGO_URL", "mongodb://127.0.0.1:27017")


def _run(coro_fn):
    """Fresh client + clean database per test."""
    if not DB.startswith("test_"):
        pytest.skip("needs a throwaway test database")
    motor = pytest.importorskip("motor.motor_asyncio")

    async def go():
        client = motor.AsyncIOMotorClient(_uri(), serverSelectionTimeoutMS=3000)
        await client.drop_database(DB)
        try:
            return await coro_fn(client[DB])
        finally:
            await client.drop_database(DB)
            client.close()
    return asyncio.run(go())


# ---------------------------------------------------------------- stubs

class FakeEmbedder:
    def __init__(self, fail_with=None):
        self.calls = []
        self.fail_with = fail_with

    async def __call__(self, texts, input_type="document"):
        self.calls.append(list(texts))
        if self.fail_with is not None:
            raise self.fail_with
        out = []
        for t in texts:
            h = hashlib.sha256(t.encode()).digest()
            out.append([b / 255.0 for b in h[:8]])
        return out, sum(len(t) for t in texts)


def _stub_registry():
    from services.search.types import Chunk, ChunkResult, Entry

    async def report_chunker(ctx, doc):
        if doc.get("deleted"):
            return ChunkResult(doc.get("tenant_id"), doc.get("project_id"), doc.get("id"))
        parts = [p for p in (doc.get("notes") or "").split("||") if p.strip()]
        return ChunkResult(
            tenant_id=doc["tenant_id"], project_id=doc["project_id"], source_id=doc["id"],
            chunks=[Chunk(kind="fake_report", header=f"Report {doc['id']}", body=p,
                          date="2026-11-01", status=doc.get("status")) for p in parts],
        )

    async def item_chunker(ctx, doc):
        parent = await ctx.one("fake_reports", {"id": doc["report_id"]},
                               {"tenant_id": 1, "project_id": 1, "deleted": 1, "status": 1})
        if not parent or parent.get("deleted"):
            return None
        return ChunkResult(
            tenant_id=parent["tenant_id"], project_id=parent["project_id"], source_id=doc["id"],
            chunks=[Chunk(kind="fake_item", header="Item", body=doc["text"],
                          parent_id=doc["report_id"], activity_code=["A100"],
                          status=parent.get("status"))],
            requeue=[("fake_reports", {"id": doc["report_id"]})],
        )

    async def project_chunker(ctx, doc):
        return ChunkResult(
            tenant_id=doc["tenant_id"], project_id=doc["id"], source_id=doc["id"],
            chunks=[Chunk(kind="project", header=f"Project {doc.get('name') or doc['id']}",
                          body="project")],
        )

    async def boom(ctx, doc):
        raise ValueError("bad row")

    return {
        "fake_reports": Entry("fake_reports", "index", "test", report_chunker),
        "fake_items": Entry("fake_items", "index", "test", item_chunker),
        "fake_broken": Entry("fake_broken", "index", "test", boom),
        "fake_excluded": Entry("fake_excluded", "exclude", "test"),
        "projects": Entry("projects", "index", "test", project_chunker),
    }


@pytest.fixture(autouse=True)
def stub_registry():
    from services.search import registry
    registry.set_registry(_stub_registry())
    yield
    registry.set_registry(None)


async def _seed_scope(db, *, cui=False, tenant_ai=True, level="public"):
    await db.tenants.insert_one({"id": "t1", "ai_enabled": tenant_ai})
    await db.projects.insert_one({"id": "p1", "tenant_id": "t1", "is_cui": cui, "ai_enabled": True})
    await db.master_docs.insert_one({"id": "masterdoc_p1", "tenant_id": "t1", "project_id": "p1",
                                     "govt_security_level": level})


async def _add_report(db, rid="r1", notes="Poured slab on level 2"):
    res = await db.fake_reports.insert_one({"id": rid, "tenant_id": "t1", "project_id": "p1",
                                           "notes": notes, "status": "draft"})
    from services.search import queue
    await queue.enqueue(db, "fake_reports", res.inserted_id)
    return res.inserted_id


async def _chunks(db, **filt):
    return await db.search_chunks.find(filt).sort("_id", 1).to_list(None)


# ---------------------------------------------------------------- queue

def test_enqueue_collapses_repeats():
    async def body(db):
        from services.search import queue
        oid = ObjectId()
        for _ in range(3):
            await queue.enqueue(db, "fake_reports", oid)
        rows = await db.search_queue.find({}).to_list(None)
        assert len(rows) == 1
        r = rows[0]
        assert r["_id"] == f"fake_reports:{oid}"
        assert r["status"] == "pending" and r["gen"] == 3 and r["source_oid"] == str(oid)
    _run(body)


def test_claim_lease_and_reaper():
    async def body(db):
        from services.search import queue
        oids = [ObjectId() for _ in range(3)]
        await queue.enqueue_many(db, "fake_reports", oids)
        got = await queue.claim_batch(db, "w1", 2)
        assert len(got) == 2 and all(j["status"] == "claimed" for j in got)
        assert len(await queue.claim_batch(db, "w2", 5)) == 1
        assert await queue.claim_batch(db, "w3", 5) == []
        # Expire one lease: the reaper puts it back. A lease running out is
        # counted apart from real failed attempts.
        await db.search_queue.update_one({"_id": got[0]["_id"]},
                                         {"$set": {"lease_until": datetime.now(timezone.utc) - timedelta(seconds=1)}})
        assert await queue.reap_expired(db) == 1
        row = await db.search_queue.find_one({"_id": got[0]["_id"]})
        assert row["status"] == "pending" and row["attempts"] == 0 and row["lease_expiries"] == 1
        again = await queue.claim_batch(db, "w4", 5)
        assert [j["_id"] for j in again] == [got[0]["_id"]]
    _run(body)


def test_write_during_processing_is_not_lost():
    async def body(db):
        from services.search import queue
        oid = ObjectId()
        await queue.enqueue(db, "fake_reports", oid)
        job = (await queue.claim_batch(db, "w1", 1))[0]
        await queue.enqueue(db, "fake_reports", oid)  # new write while claimed
        row = await db.search_queue.find_one({"_id": job["_id"]})
        assert row["status"] == "claimed"  # nobody else picks it up meanwhile
        await queue.mark_done(db, job, tenant_id="t1", source_id="r1")
        row = await db.search_queue.find_one({"_id": job["_id"]})
        assert row["status"] == "pending" and row["source_id"] == "r1"
    _run(body)


def test_mark_failed_backoff_then_failed():
    async def body(db):
        from services.search import queue
        await queue.enqueue(db, "fake_reports", ObjectId())
        delays = []
        for i in range(queue.MAX_ATTEMPTS):
            await db.search_queue.update_many({}, {"$set": {"available_at": datetime.now(timezone.utc)}})
            job = (await queue.claim_batch(db, "w", 1))[0]
            before = datetime.now(timezone.utc).replace(tzinfo=None)
            status = await queue.mark_failed(db, job, "boom")
            row = await db.search_queue.find_one({"_id": job["_id"]})
            if i < queue.MAX_ATTEMPTS - 1:
                assert status == "pending"
                delays.append(round((row["available_at"] - before).total_seconds()))
            else:
                assert status == "failed"
        assert delays[:4] == [30, 120, 600, 3600]
        assert await queue.failed_count(db) == 1
    _run(body)


# ---------------------------------------------------------------- worker

def test_insert_builds_chunks_and_embeds():
    async def body(db):
        from services.search.worker import process_once
        await _seed_scope(db)
        oid = await _add_report(db, notes="Poured slab||Formwork stripped")
        emb = FakeEmbedder()
        stats = await process_once(db, emb)
        assert stats["done"] == 1 and stats["embedded"] == 2
        ch = await _chunks(db)
        assert [c["_id"] for c in ch] == [f"fake_reports:{oid}:0", f"fake_reports:{oid}:1"]
        c = ch[0]
        assert c["tenant_id"] == "t1" and c["project_id"] == "p1" and c["source_id"] == "r1"
        assert c["source_oid"] == str(oid) and c["kind"] == "fake_report"
        assert c["embedded"] is True and len(c["embedding"]) == 8
        assert c["text"] == "Report r1\nPoured slab" and c["content_hash"]
        assert len(emb.calls) == 1 and len(emb.calls[0]) == 2  # one call per batch
        job = await db.search_queue.find_one({})
        assert job["status"] == "done" and job["source_id"] == "r1"
        usage = await db.ai_usage_tracking.find_one({})
        assert usage["tenant_id"] == "t1" and usage["service"] == "voyage" and usage["tokens"] > 0
    _run(body)


def test_same_text_is_not_reembedded_and_changed_text_is():
    async def body(db):
        from services.search import queue
        from services.search.worker import process_once
        await _seed_scope(db)
        oid = await _add_report(db, notes="One||Two")
        emb = FakeEmbedder()
        await process_once(db, emb)
        first = await _chunks(db)

        # Write that changes nothing searchable.
        await db.fake_reports.update_one({"_id": oid}, {"$set": {"updated_at": "x"}})
        await queue.enqueue(db, "fake_reports", oid)
        stats = await process_once(db, emb)
        assert stats["done"] == 1 and stats["embedded"] == 0 and len(emb.calls) == 1
        assert [c["indexed_at"] for c in await _chunks(db)] == [c["indexed_at"] for c in first]

        # Second piece edited, first piece removed -> only changed text embedded.
        await db.fake_reports.update_one({"_id": oid}, {"$set": {"notes": "Two changed"}})
        await queue.enqueue(db, "fake_reports", oid)
        stats = await process_once(db, emb)
        assert stats["embedded"] == 1 and emb.calls[-1] == ["Report r1\nTwo changed"]
        ch = await _chunks(db)
        assert [c["body"] for c in ch] == ["Two changed"]

        # A metadata change (status) also rewrites the chunk.
        await db.fake_reports.update_one({"_id": oid}, {"$set": {"status": "submitted"}})
        await queue.enqueue(db, "fake_reports", oid)
        stats = await process_once(db, emb)
        assert stats["embedded"] == 1
        assert (await _chunks(db))[0]["status"] == "submitted"
    _run(body)


def test_delete_removes_chunks_and_rechecks_children():
    async def body(db):
        from services.search import queue
        from services.search.worker import process_once
        await _seed_scope(db)
        roid = await _add_report(db)
        ires = await db.fake_items.insert_one({"id": "i1", "report_id": "r1", "text": "Ductwork 40 ft"})
        await queue.enqueue(db, "fake_items", ires.inserted_id)
        await process_once(db, FakeEmbedder())
        assert len(await _chunks(db, parent_id="r1")) == 1
        assert len(await _chunks(db, source_collection="fake_reports")) == 1

        await db.fake_reports.delete_one({"_id": roid})
        await queue.enqueue(db, "fake_reports", roid, op="delete")
        stats = await process_once(db, FakeEmbedder())
        # Only the report's own chunk goes; the child is re-queued, not deleted.
        assert stats["deleted"] == 1 and stats["requeued"] == 1
        assert len(await _chunks(db, parent_id="r1")) == 1
        # The delete job's queue row is gone (the queue does not grow forever).
        assert await db.search_queue.count_documents({"_id": f"fake_reports:{roid}"}) == 0
        # The child's chunker sees its parent is gone and drops its chunk.
        await process_once(db, FakeEmbedder())
        assert await _chunks(db) == []
    _run(body)


def test_child_that_outlives_its_parent_keeps_its_chunks():
    async def body(db):
        from services.search import queue
        from services.search.types import Chunk, ChunkResult, Entry
        from services.search import registry
        from services.search.worker import process_once

        async def photo_chunker(ctx, doc):  # a photo stays when its report goes
            return ChunkResult("t1", "p1", doc["id"], chunks=[
                Chunk(kind="photo", header="Photo", body=doc["caption"], parent_id=doc["report_id"])])

        registry.set_registry({**_stub_registry(),
                               "fake_photos": Entry("fake_photos", "index", "test", photo_chunker)})
        await _seed_scope(db)
        roid = await _add_report(db)
        ph = await db.fake_photos.insert_one({"id": "ph1", "report_id": "r1", "caption": "Rebar"})
        await queue.enqueue(db, "fake_photos", ph.inserted_id)
        await process_once(db, FakeEmbedder())
        await db.fake_reports.delete_one({"_id": roid})
        await queue.enqueue(db, "fake_reports", roid, op="delete")
        await process_once(db, FakeEmbedder())
        await process_once(db, FakeEmbedder())
        assert [c["source_id"] for c in await _chunks(db)] == ["ph1"]
    _run(body)


def test_delete_with_no_chunks_uses_remembered_app_id():
    async def body(db):
        from services.search import queue
        from services.search.worker import process_once
        await _seed_scope(db)
        roid = await _add_report(db, notes="")  # report with no text -> no chunks
        ires = await db.fake_items.insert_one({"id": "i1", "report_id": "r1", "text": "Note"})
        await queue.enqueue(db, "fake_items", ires.inserted_id)
        await process_once(db, FakeEmbedder())
        await process_once(db, FakeEmbedder())  # the item re-queued its report
        assert await _chunks(db, source_collection="fake_reports") == []
        assert len(await _chunks(db, parent_id="r1")) == 1
        await db.fake_reports.delete_one({"_id": roid})
        await queue.enqueue(db, "fake_reports", roid, op="delete")
        await process_once(db, FakeEmbedder())  # remembered id finds the child...
        await process_once(db, FakeEmbedder())  # ...whose chunker drops it
        assert await _chunks(db) == []
    _run(body)


def test_soft_delete_of_parent_rechecks_children():
    async def body(db):
        from services.search import queue
        from services.search.worker import process_once
        await _seed_scope(db)
        roid = await _add_report(db)
        ires = await db.fake_items.insert_one({"id": "i1", "report_id": "r1", "text": "Note"})
        await queue.enqueue(db, "fake_items", ires.inserted_id)
        await process_once(db, FakeEmbedder())
        await process_once(db, FakeEmbedder())
        assert (await _chunks(db, parent_id="r1"))[0]["status"] == "draft"

        # The parent's status changes: the child copies it on its next pass.
        await db.fake_reports.update_one({"_id": roid}, {"$set": {"status": "submitted"}})
        await queue.enqueue(db, "fake_reports", roid)
        stats = await process_once(db, FakeEmbedder())
        assert stats["requeued"] >= 1
        await process_once(db, FakeEmbedder())
        assert (await _chunks(db, parent_id="r1"))[0]["status"] == "submitted"

        # Soft delete of the parent: the child's chunks go too.
        await db.fake_reports.update_one({"_id": roid}, {"$set": {"deleted": True}})
        await queue.enqueue(db, "fake_reports", roid)
        await process_once(db, FakeEmbedder())
        await process_once(db, FakeEmbedder())
        assert await _chunks(db) == []
    _run(body)


def test_project_delete_and_rename():
    async def body(db):
        from services.search import queue
        from services.search.worker import process_once
        await _seed_scope(db)
        roid = await _add_report(db)
        proj = await db.projects.find_one({"id": "p1"})
        await queue.enqueue(db, "projects", proj["_id"])
        await process_once(db, FakeEmbedder())
        assert {c["source_collection"] for c in await _chunks(db)} == {"fake_reports", "projects"}

        # Rename: every record in the project is re-queued (headers carry the name).
        await db.projects.update_one({"id": "p1"}, {"$set": {"name": "Tower B"}})
        await queue.enqueue(db, "projects", proj["_id"])
        stats = await process_once(db, FakeEmbedder())
        assert stats["requeued"] == 1
        row = await db.search_queue.find_one({"_id": f"fake_reports:{roid}"})
        assert row["status"] == "pending"
        await process_once(db, FakeEmbedder())

        # Hard delete of the project: chunks of records the delete did not
        # cascade to go too, and their waiting queue rows.
        await queue.enqueue(db, "fake_reports", roid)  # a waiting job in the project
        await db.projects.delete_one({"_id": proj["_id"]})
        await queue.enqueue(db, "projects", proj["_id"], op="delete")
        await db.search_queue.update_one({"_id": f"fake_reports:{roid}"},
                                         {"$set": {"available_at": datetime.now(timezone.utc) + timedelta(hours=1)}})
        await process_once(db, FakeEmbedder())
        assert await _chunks(db) == []
        assert await db.search_queue.count_documents({}) == 0
    _run(body)


def test_soft_delete_and_unindexed_collection_remove_chunks():
    async def body(db):
        from services.search import queue
        from services.search.worker import process_once
        await _seed_scope(db)
        oid = await _add_report(db)
        await process_once(db, FakeEmbedder())
        await db.fake_reports.update_one({"_id": oid}, {"$set": {"deleted": True}})
        await queue.enqueue(db, "fake_reports", oid)
        await process_once(db, FakeEmbedder())
        assert await _chunks(db) == []

        # Leftover chunks of a collection that is not indexed are dropped.
        xo = ObjectId()
        await db.search_chunks.insert_one({"_id": f"fake_excluded:{xo}:0",
                                           "source_collection": "fake_excluded", "source_oid": str(xo)})
        await queue.enqueue(db, "fake_excluded", xo)
        await process_once(db, FakeEmbedder())
        assert await _chunks(db) == []
    _run(body)


@pytest.mark.parametrize("scope", [dict(cui=True), dict(tenant_ai=False), dict(level="secret")])
def test_gate_stores_chunk_without_vector(scope):
    async def body(db):
        from services.search.worker import process_once
        await _seed_scope(db, **scope)
        await _add_report(db)
        emb = FakeEmbedder()
        stats = await process_once(db, emb)
        assert stats["done"] == 1 and emb.calls == []
        c = (await _chunks(db))[0]
        assert c["embedding"] is None and c["embedded"] is False and c["model"] is None
    _run(body)


def test_gate_flip_requeues_and_embeds_project():
    async def body(db):
        from services.search import queue
        from services.search.worker import process_once
        await _seed_scope(db, cui=True)
        await _add_report(db)
        await process_once(db, FakeEmbedder())
        assert (await _chunks(db))[0]["embedded"] is False
        # Project no longer CUI; any write in the project notices the change.
        await db.projects.update_one({"id": "p1"}, {"$set": {"is_cui": False}})
        other = await db.fake_reports.insert_one({"id": "r2", "tenant_id": "t1", "project_id": "p1", "notes": "x"})
        await queue.enqueue(db, "fake_reports", other.inserted_id)
        stats = await process_once(db, FakeEmbedder())
        assert stats["requeued"] >= 1
        await process_once(db, FakeEmbedder())
        assert all(c["embedded"] for c in await _chunks(db))
    _run(body)


def test_requeue_from_chunker():
    async def body(db):
        from services.search import queue
        from services.search.worker import process_once
        await _seed_scope(db)
        roid = await _add_report(db)
        await process_once(db, FakeEmbedder())
        ires = await db.fake_items.insert_one({"id": "i1", "report_id": "r1", "text": "Note"})
        await queue.enqueue(db, "fake_items", ires.inserted_id)
        stats = await process_once(db, FakeEmbedder())
        assert stats["requeued"] == 1
        row = await db.search_queue.find_one({"_id": f"fake_reports:{roid}"})
        assert row["status"] == "pending"
    _run(body)


def test_embedder_unavailable_keeps_job_pending_with_backoff():
    async def body(db):
        from services.search.voyage_client import VoyageUnavailable
        from services.search.worker import process_once
        await _seed_scope(db)
        oid = await _add_report(db)
        for n in range(1, 8):
            await db.search_queue.update_many({}, {"$set": {"available_at": datetime.now(timezone.utc)}})
            stats = await process_once(db, FakeEmbedder(fail_with=VoyageUnavailable("down")))
            assert stats["retry"] == 1
            row = await db.search_queue.find_one({"_id": f"fake_reports:{oid}"})
            assert row["status"] == "pending" and row["embed_waits"] == n
            wait = (row["available_at"] - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()
            assert 0 < wait <= 3600
        assert await _chunks(db) == []  # nothing half-written
        await db.search_queue.update_many({}, {"$set": {"available_at": datetime.now(timezone.utc)}})
        assert (await process_once(db, FakeEmbedder()))["done"] == 1
        assert (await _chunks(db))[0]["embedded"] is True
    _run(body)


def test_other_embedder_error_and_chunker_error_back_off():
    async def body(db):
        from services.search import queue
        from services.search.worker import process_once
        await _seed_scope(db)
        oid = await _add_report(db)
        await process_once(db, FakeEmbedder(fail_with=RuntimeError("weird")))
        row = await db.search_queue.find_one({"_id": f"fake_reports:{oid}"})
        assert row["status"] == "pending" and row["attempts"] == 1
        bo = await db.fake_broken.insert_one({"x": 1})
        await queue.enqueue(db, "fake_broken", bo.inserted_id)
        await process_once(db, FakeEmbedder())
        row = await db.search_queue.find_one({"_id": f"fake_broken:{bo.inserted_id}"})
        assert row["status"] == "pending" and row["attempts"] == 1 and "bad row" in row["last_error"]
    _run(body)


# ---------------------------------------------------------------- listener

def test_event_to_job_mapping():
    from services.search.listener import build_pipeline, event_to_job
    oid = ObjectId()
    ins = {"operationType": "insert", "ns": {"db": "d", "coll": "fake_reports"}, "documentKey": {"_id": oid}}
    assert event_to_job(ins, {"fake_reports"}) == ("fake_reports", str(oid), "upsert")
    for op in ("update", "replace"):
        assert event_to_job({**ins, "operationType": op})[2] == "upsert"
    assert event_to_job({**ins, "operationType": "delete"})[2] == "delete"
    assert event_to_job({**ins, "operationType": "drop"}) is None
    assert event_to_job(ins, {"other"}) is None
    assert event_to_job({**ins, "documentKey": {}}) is None
    assert event_to_job({**ins, "documentKey": {"_id": "abc"}})[1] == "abc"
    pipe = build_pipeline(["a", "b"])
    assert pipe[0]["$match"]["ns.coll"] == {"$in": ["a", "b"]}


def test_handle_event_enqueues_and_leader_lease():
    async def body(db):
        from services.search import listener
        oid = ObjectId()
        ev = {"operationType": "update", "ns": {"coll": "fake_reports"}, "documentKey": {"_id": oid}}
        assert await listener.handle_event(db, ev, {"fake_reports"})
        assert await db.search_queue.count_documents({"_id": f"fake_reports:{oid}"}) == 1
        assert await listener.try_lead(db, "a") is True
        assert await listener.try_lead(db, "b") is False
        assert await listener.try_lead(db, "a") is True
        await listener.release_lead(db, "a")
        assert await listener.try_lead(db, "b") is True
    _run(body)


# ---------------------------------------------------------------- reconcile

def test_backfill_orphans_and_one_time_trigger():
    async def body(db):
        from services.search import reconcile
        from services.search.worker import process_once
        await _seed_scope(db)
        a = await db.fake_reports.insert_one({"id": "r1", "tenant_id": "t1", "project_id": "p1", "notes": "A"})
        await db.fake_reports.insert_one({"id": "r2", "tenant_id": "t1", "project_id": "p1", "notes": "B"})
        assert await reconcile.nightly_tick(db) == "backfill"
        assert await db.search_queue.count_documents({"collection": "fake_reports"}) == 2
        assert await reconcile.nightly_tick(db) != "backfill"  # never a second backfill
        await process_once(db, FakeEmbedder())
        assert len(await _chunks(db, source_collection="fake_reports")) == 2
        # Row deleted while nobody listened -> reconcile removes its chunks.
        await db.fake_reports.delete_one({"_id": a.inserted_id})
        assert await reconcile.delete_orphan_chunks(db) == 1
        assert [c["source_id"] for c in await _chunks(db, source_collection="fake_reports")] == ["r2"]
        # Listener asked for a full re-queue.
        await reconcile.request_full_reconcile(db, "test")
        assert await reconcile.nightly_tick(db) == "full"
    _run(body)


def test_nightly_runs_after_backfill_then_weekly_full(monkeypatch):
    async def body(db):
        from services.search import reconcile
        clock = {"now": datetime(2026, 11, 17, 3, 0, tzinfo=timezone.utc)}
        monkeypatch.setattr(reconcile, "_local_now", lambda: clock["now"])
        await _seed_scope(db)
        assert await reconcile.nightly_tick(db) == "backfill"
        # Past 02:00 on the same day: the nightly run happens (changed rows only).
        assert await reconcile.nightly_tick(db) == "nightly"
        assert await reconcile.nightly_tick(db) is None  # once per day
        # A week after the last full pass, the nightly run is a full pass.
        await db.search_state.update_one({"_id": "nightly"}, {"$set": {
            "last_full_at": datetime.now(timezone.utc) - timedelta(days=8)}})
        clock["now"] = clock["now"] + timedelta(days=1)
        assert await reconcile.nightly_tick(db) == "full_nightly"
        clock["now"] = clock["now"] + timedelta(days=1)
        assert await reconcile.nightly_tick(db) == "nightly"
        # Before 02:00 nothing runs.
        clock["now"] = clock["now"].replace(hour=1) + timedelta(days=1)
        assert await reconcile.nightly_tick(db) is None
    _run(body)


def test_orphan_pass_removes_chunks_of_deleted_project_and_company():
    async def body(db):
        from services.search import queue, reconcile
        from services.search.worker import process_once
        await _seed_scope(db)
        await db.tenants.insert_one({"id": "t2", "ai_enabled": True})
        await db.projects.insert_one({"id": "p2", "tenant_id": "t2"})
        await _add_report(db, "r1")
        other = await db.fake_reports.insert_one({"id": "r2", "tenant_id": "t2", "project_id": "p2",
                                                  "notes": "Other company"})
        await queue.enqueue(db, "fake_reports", other.inserted_id)
        await process_once(db, FakeEmbedder())
        assert len(await _chunks(db)) == 2
        # Project removed without any event for the report (no cascade).
        await db.projects.delete_one({"id": "p1"})
        assert await reconcile.delete_orphan_chunks(db) == 1
        assert [c["tenant_id"] for c in await _chunks(db)] == ["t2"]
        # Company closed: everything it had goes.
        await db.tenants.delete_one({"id": "t2"})
        assert await reconcile.delete_orphan_chunks(db) == 1
        assert await _chunks(db) == []
    _run(body)


def test_recheck_gates_requeues_on_company_switch():
    async def body(db):
        from services.search import reconcile
        from services.search.worker import process_once
        await _seed_scope(db)
        oid = await _add_report(db)
        await process_once(db, FakeEmbedder())
        assert (await _chunks(db))[0]["embedded"] is True
        assert await reconcile.recheck_gates(db) == 0  # nothing changed
        # Company AI switch turned off with no record written.
        await db.tenants.update_one({"id": "t1"}, {"$set": {"ai_enabled": False}})
        assert await reconcile.recheck_gates(db) == 1
        row = await db.search_queue.find_one({"_id": f"fake_reports:{oid}"})
        assert row["status"] == "pending"
        await process_once(db, FakeEmbedder())
        c = (await _chunks(db))[0]
        assert c["embedded"] is False and c["embedding"] is None
    _run(body)


def test_enqueue_changed_since():
    async def body(db):
        from services.search import reconcile
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=5)
        await db.fake_reports.insert_many([
            {"id": "old_text", "updated_at": old.isoformat()},
            {"id": "old_date", "updated_at": old},
            {"id": "old_naive", "updated_at": old.replace(tzinfo=None).isoformat()},
            {"id": "new_text", "updated_at": now.isoformat()},             # ISO text with zone
            {"id": "new_date", "updated_at": now},                         # a real date
            {"id": "new_naive", "updated_at": now.replace(tzinfo=None).isoformat()},  # no zone
            {"id": "none"},                                                # left to listener / weekly
        ])
        await reconcile.enqueue_changed_since(db, now - timedelta(hours=1), ["fake_reports"])
        queued = {r["source_oid"] async for r in db.search_queue.find({})}
        ids = {str(r["_id"]): r["id"] async for r in db.fake_reports.find({})}
        assert {ids[o] for o in queued} == {"new_text", "new_date", "new_naive"}
    _run(body)


def test_store_hash_and_search_index_definitions():
    from services.search import store
    a = store.content_hash("t", {"tenant_id": "x", "status": "open"})
    assert a == store.content_hash("t", {"tenant_id": "x", "status": "open"})
    assert a != store.content_hash("t", {"tenant_id": "x", "status": "closed"})
    vec = store.vector_index_definition()["fields"]
    assert vec[0]["numDimensions"] == 1024 and vec[0]["similarity"] == "dotProduct"
    assert {f["path"] for f in vec[1:]} >= {"tenant_id", "project_id", "allowed_roles",
                                            "source_collection", "status", "model"}
    # Definition comparison: Atlas's extra defaults are fine, a missing filter is not.
    want = store.vector_index_definition()
    stored = {"fields": [dict(f, extra=1) for f in reversed(want["fields"])], "x": 1}
    assert store._covers(stored, want)
    assert not store._covers({"fields": want["fields"][:-1]}, want)

    async def body(db):
        await store.ensure_indexes(db)
        out = await store.ensure_search_indexes(db)  # local mongod: logged, not raised
        assert set(out) == {"vector", "text"}
    _run(body)


def test_real_registry_excludes_system_collections():
    from services.search import registry
    registry.set_registry(None)
    reg = registry.get_registry()
    for name in ("audit_log", "runtime_settings", "team_invites", "copilot_messages", "search_chunks",
                 "search_queue", "search_state", "ai_usage_tracking"):
        assert reg[name].decision == "exclude"
    assert reg["audit_log"].decision != "index"
    assert not set(registry.INDEXED_COLLECTIONS()) & {"audit_log", "search_chunks", "users"}


# ---------------------------------------------------------------- leases, takeover, rejection

def test_taken_over_job_is_never_overwritten():
    async def body(db):
        from services.search import queue
        oid = ObjectId()
        await queue.enqueue(db, "fake_reports", oid)
        a = (await queue.claim_batch(db, "wA", 1))[0]
        # A's lease runs out, B takes the job.
        await db.search_queue.update_one({"_id": a["_id"]}, {"$set": {
            "lease_until": datetime.now(timezone.utc) - timedelta(seconds=1)}})
        await queue.reap_expired(db)
        b = (await queue.claim_batch(db, "wB", 1))[0]
        await queue.enqueue(db, "fake_reports", oid)  # new write while B holds it
        # A wakes up late: none of its updates touch B's claim.
        assert not await queue.still_holds(db, a)
        await queue.mark_done(db, a, source_id="stale")
        assert await queue.mark_failed(db, a, "late") == "lost"
        await queue._release_if_requeued(db, a)
        assert not await queue.finish_delete(db, a)
        row = await db.search_queue.find_one({"_id": a["_id"]})
        assert row["status"] == "claimed" and row["claimed_by"] == "wB"
        assert row.get("source_id") is None and row.get("last_error") is None
        assert await queue.still_holds(db, b)
    _run(body)


def test_worker_skips_write_when_job_was_taken_over():
    async def body(db):
        from services.search.worker import process_once
        await _seed_scope(db)
        oid = await _add_report(db)

        class StealingEmbedder(FakeEmbedder):
            async def __call__(self, texts, input_type="document"):
                # While we wait on the service, another worker takes the job.
                await db.search_queue.update_many({}, {"$set": {"claimed_by": "other"}})
                return await super().__call__(texts, input_type)

        stats = await process_once(db, StealingEmbedder(), worker_id="me")
        assert stats["lost"] == 1 and stats["done"] == 0
        assert await _chunks(db) == []
        row = await db.search_queue.find_one({"_id": f"fake_reports:{oid}"})
        assert row["status"] == "claimed" and row["claimed_by"] == "other"
    _run(body)


def test_slow_embed_keeps_its_lease(monkeypatch):
    async def body(db):
        from services.search import queue
        from services.search.worker import process_once
        monkeypatch.setattr(queue, "LEASE_SECONDS", 1)
        monkeypatch.setattr(queue, "HEARTBEAT_SECONDS", 0.2)
        await _seed_scope(db)
        await _add_report(db)

        class SlowEmbedder(FakeEmbedder):
            async def __call__(self, texts, input_type="document"):
                await asyncio.sleep(2.5)
                # Another replica's reaper finds nothing to take back.
                assert await queue.reap_expired(db) == 0
                return await super().__call__(texts, input_type)

        stats = await process_once(db, SlowEmbedder())
        assert stats["done"] == 1 and stats["lost"] == 0
        row = await db.search_queue.find_one({})
        assert row["status"] == "done" and row.get("lease_expiries") == 0
    _run(body)


def test_lease_expiries_fail_only_after_many(monkeypatch):
    async def body(db):
        from services.search import queue
        await queue.enqueue(db, "fake_reports", ObjectId())
        for i in range(queue.MAX_LEASE_EXPIRIES):
            await db.search_queue.update_many({}, {"$set": {"available_at": datetime.now(timezone.utc)}})
            job = (await queue.claim_batch(db, f"w{i}", 1))[0]
            await db.search_queue.update_one({"_id": job["_id"]}, {"$set": {
                "lease_until": datetime.now(timezone.utc) - timedelta(seconds=1)}})
            await queue.reap_expired(db)
            row = await db.search_queue.find_one({})
            if i < queue.MAX_ATTEMPTS:
                assert row["status"] == "pending"  # never failed after just 6
        assert row["status"] == "failed" and row["attempts"] == 0
    _run(body)


def test_rejected_input_fails_only_its_own_job():
    async def body(db):
        from services.search.voyage_client import VoyageUnavailable
        from services.search import queue
        from services.search.worker import process_once
        await _seed_scope(db)
        good1 = await _add_report(db, "r1", "Fine text")
        bad = await _add_report(db, "r2", "BAD input")
        good2 = await _add_report(db, "r3", "More fine text")

        class Picky(FakeEmbedder):
            async def __call__(self, texts, input_type="document"):
                self.calls.append(list(texts))
                if any("BAD" in t for t in texts):
                    raise VoyageUnavailable("Voyage returned HTTP 400")
                return await FakeEmbedder.__call__(FakeEmbedder(), texts, input_type)

        stats = await process_once(db, Picky())
        assert stats["done"] == 2 and stats["retry"] == 1
        assert {c["source_id"] for c in await _chunks(db)} == {"r1", "r3"}
        row = await db.search_queue.find_one({"_id": f"fake_reports:{bad}"})
        assert row["status"] == "pending" and row["attempts"] == 1 and row["embed_waits"] == 0
        assert "HTTP 400" in row["last_error"]
        for oid in (good1, good2):
            assert (await db.search_queue.find_one({"_id": f"fake_reports:{oid}"}))["status"] == "done"
        # Repeated refusals end in failed (shown by failed_count), unlike an outage.
        for _ in range(queue.MAX_ATTEMPTS):
            await db.search_queue.update_many({}, {"$set": {"available_at": datetime.now(timezone.utc)}})
            await process_once(db, Picky())
        assert (await db.search_queue.find_one({"_id": f"fake_reports:{bad}"}))["status"] == "failed"
    _run(body)


def test_key_problem_waits_like_an_outage():
    async def body(db):
        from services.search.voyage_client import VoyageUnavailable
        from services.search.worker import _rejected_status, process_once
        assert _rejected_status(VoyageUnavailable("Voyage returned HTTP 413")) == 413
        assert _rejected_status(VoyageUnavailable("Voyage returned HTTP 429")) is None
        assert _rejected_status(VoyageUnavailable("VOYAGE_API_KEY is not set")) is None
        err = RuntimeError("x")
        err.status_code = 422
        assert _rejected_status(err) == 422
        await _seed_scope(db)
        await _add_report(db, "r1", "a")
        await _add_report(db, "r2", "b")
        emb = FakeEmbedder(fail_with=VoyageUnavailable("Voyage returned HTTP 401"))
        stats = await process_once(db, emb)
        assert stats["retry"] == 2 and len(emb.calls) == 1  # no splitting for a bad key
        rows = await db.search_queue.find({}).to_list(None)
        assert all(r["embed_waits"] == 1 and r["attempts"] == 0 for r in rows)
    _run(body)


# ---------------------------------------------------------------- listener history, idle loops

class _FakeStream:
    def __init__(self, exc):
        self.exc = exc
        self.resume_token = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def try_next(self):
        raise self.exc


class _DbWithStream:
    """The real test database, with a fake change stream."""

    def __init__(self, db, exc):
        self._db = db
        self._exc = exc
        self.watch_kwargs = None

    def __getitem__(self, name):
        return self._db[name]

    def watch(self, pipeline, **kwargs):
        self.watch_kwargs = kwargs
        return _FakeStream(self._exc)


def test_listener_lost_history_asks_for_full_requeue():
    async def body(db):
        from pymongo.errors import OperationFailure
        from services.search import listener, reconcile
        await listener.save_token(db, {"_data": "old"})
        fake = _DbWithStream(db, OperationFailure("history lost", code=286))
        await listener._listen(fake, "me", lambda: True)
        assert fake.watch_kwargs["resume_after"] == {"_data": "old"}
        assert await listener.load_token(db) is None
        req = await db.search_state.find_one({"_id": reconcile.FULL_REQUEST_ID})
        assert req["requested"] is True
        assert await reconcile.nightly_tick(db) in ("backfill", "full")

        # Without a saved point, the same error is not "lost history": it is raised.
        await db.search_state.delete_many({})
        fake = _DbWithStream(db, OperationFailure("other", code=286))
        with pytest.raises(OperationFailure):
            await listener._listen(fake, "me", lambda: True)
        # Any other error code with a token is raised too.
        await listener.save_token(db, {"_data": "old"})
        fake = _DbWithStream(db, OperationFailure("boom", code=2))
        with pytest.raises(OperationFailure):
            await listener._listen(fake, "me", lambda: True)
        assert await db.search_state.find_one({"_id": reconcile.FULL_REQUEST_ID}) is None
    _run(body)


def test_loops_do_no_database_work_while_flag_is_off(monkeypatch):
    from services import feature_flags
    from services.search import listener, reconcile, worker
    monkeypatch.setattr(feature_flags, "search_everything_enabled", lambda: False)
    for mod in (listener, worker, reconcile):
        if hasattr(mod, "FIRST_SLEEP_SECONDS"):
            monkeypatch.setattr(mod, "FIRST_SLEEP_SECONDS", 0)
    monkeypatch.setattr(listener, "FLAG_OFF_SLEEP_SECONDS", 0.01)
    monkeypatch.setattr(worker, "FLAG_OFF_SLEEP_SECONDS", 0.01)
    monkeypatch.setattr(reconcile, "TICK_SECONDS", 0.01)
    calls = []

    def get_db():
        calls.append(1)
        raise AssertionError("no database work while the flag is off")

    async def go():
        tasks = [asyncio.ensure_future(f(get_db)) for f in
                 (listener.run_listener_loop, worker.run_worker_loop, reconcile.run_nightly_loop)]
        await asyncio.sleep(0.3)
        for t in tasks:
            assert not t.done()
            t.cancel()
        for t in tasks:
            with pytest.raises(asyncio.CancelledError):
                await t
    asyncio.run(go())
    assert calls == []


def test_listener_waits_before_first_flag_check():
    from services.search import listener
    assert 60 <= listener.FIRST_SLEEP_SECONDS <= 120


def test_ensure_ready_retries_failed_atlas_index(monkeypatch):
    async def body(db):
        from services.search import store
        results = [{"vector": "failed:8", "text": "created"}, {"vector": "created", "text": "exists"}]
        calls = []

        async def fake(dbx):
            calls.append(1)
            return results[len(calls) - 1]

        monkeypatch.setattr(store, "ensure_search_indexes", fake)
        await store.ensure_ready(db)
        assert id(db) not in store._ready_done and len(calls) == 1
        await store.ensure_ready(db)          # too soon: not retried yet
        assert len(calls) == 1
        store._retry_at[id(db)] = 0           # retry time reached
        await store.ensure_ready(db)
        assert len(calls) == 2 and id(db) in store._ready_done
        store._ready_done.discard(id(db))
        store._indexes_done.discard(id(db))
    _run(body)
