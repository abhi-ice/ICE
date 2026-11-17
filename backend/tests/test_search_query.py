"""Search everything, query side (services/search/query.py, routers/search_router.py).

A plain mongod cannot run $vectorSearch or $search, so the two database
searches are swapped for stubs (query._run_vector_stage / _run_text_stage)
that apply the Python copy of the permission filter to rows held in memory.
Everything else (project membership, source rows, project names) uses the
local test mongod (DB_NAME must start with test_). Voyage is never called.
"""
import asyncio
import os

import pytest
from bson import ObjectId
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

pytestmark = pytest.mark.destructive

DB = os.environ.get("DB_NAME", "test_search_query")
TEN = "ten-a"
OTHER = "ten-b"
P1, P2 = "proj-1", "proj-2"


def _uri():
    return os.environ.get("MONGO_URL", "mongodb://127.0.0.1:27017")


def _run(coro_fn):
    """Fresh client + clean database per test, installed as database._db so
    services.project_access and auth read the same test data."""
    if not DB.startswith("test_"):
        pytest.skip("needs a throwaway test database")
    motor = pytest.importorskip("motor.motor_asyncio")
    # Other test files swap sys.modules["database"] for a stand-in, so
    # "import database" here may not be the module the app code reads.
    # Patch the globals of the get_db the app modules actually hold.
    import auth as _auth
    from services import project_access as _pa
    # Some test files install a stand-in "database" module at collection
    # time, so the get_db these modules hold may be the stand-in's. Point
    # each module's own get_db at this test's database instead.
    holders = [_pa, _auth]

    async def go():
        client = motor.AsyncIOMotorClient(_uri(), serverSelectionTimeoutMS=3000)
        await client.drop_database(DB)
        db = client[DB]
        olds = [(m, m.get_db) for m in holders]
        for m, _ in olds:
            m.get_db = lambda: db
        try:
            return await coro_fn(db)
        finally:
            for m, old in olds:
                m.get_db = old
            await client.drop_database(DB)
            client.close()
    return asyncio.run(go())


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    from services import feature_flags
    state = {"search": True, "access": False}
    monkeypatch.setattr(feature_flags, "search_everything_enabled", lambda: state["search"])
    monkeypatch.setattr(feature_flags, "project_access_enabled", lambda: state["access"])
    return state


def _ctx(role="pm", uid="u1", tenant=TEN, scope=None):
    user = {"id": uid, "role": role}
    if scope:
        user["access_scope"] = scope
    return {"tenant_id": tenant, "user": user}


async def _seed_base(db):
    await db.tenants.insert_many([{"id": TEN, "ai_enabled": True}, {"id": OTHER, "ai_enabled": True}])
    await db.projects.insert_many([
        {"id": P1, "tenant_id": TEN, "name": "Hospital Wing"},
        {"id": P2, "tenant_id": TEN, "name": "School Annex"},
        {"id": "proj-x", "tenant_id": OTHER, "name": "Other Co Job"},
    ])
    await db.project_members.insert_one(
        {"tenant_id": TEN, "user_id": "u1", "project_id": P1, "role_on_project": "pm"})


# ---------------------------------------------------------------- filter building

def test_scope_not_enforced_has_no_project_filter():
    from services.search import query as Q

    async def go(db):
        await _seed_base(db)
        s = await Q.build_scope(_ctx())
        assert s.project_ids is None
        # recovery reports still need membership, flag or not
        assert s.recovery_project_ids == [P1]
        f = Q.vector_filter(s)
        assert {"tenant_id": {"$eq": TEN}} in f["$and"]
        assert not any("project_id" in c for c in f["$and"])
        # role and private clauses for a non-admin
        assert {"$or": [{"allowed_roles": {"$eq": None}}, {"allowed_roles": {"$eq": "pm"}}]} in f["$and"]
        assert {"$or": [{"private": {"$eq": False}}, {"owner_user_id": {"$eq": "u1"}}]} in f["$and"]
        assert {"$or": [{"source_collection": {"$ne": "recovery_reports"}},
                        {"project_id": {"$in": [P1]}}]} in f["$and"]
        tf = Q.text_filter_clauses(s)
        assert tf[0] == {"equals": {"path": "tenant_id", "value": TEN}}
        assert not any("project_id" in str(c) and "in" in c for c in tf)
    _run(go)


def test_scope_enforced_limits_to_assigned_projects_plus_company_level(_flags):
    from services.search import query as Q
    _flags["access"] = True

    async def go(db):
        await _seed_base(db)
        s = await Q.build_scope(_ctx())
        assert s.project_ids == [P1] and s.include_company_level
        f = Q.vector_filter(s)
        assert {"$or": [{"project_id": {"$in": [P1]}}, {"project_id": {"$eq": None}}]} in f["$and"]
        assert Q.chunk_allowed(s, {"tenant_id": TEN, "project_id": P1})
        assert Q.chunk_allowed(s, {"tenant_id": TEN, "project_id": None})
        assert not Q.chunk_allowed(s, {"tenant_id": TEN, "project_id": P2})
        assert not Q.chunk_allowed(s, {"tenant_id": OTHER, "project_id": P1})
    _run(go)


def test_scope_enforced_with_no_projects_still_sees_company_level(_flags):
    from services.search import query as Q
    _flags["access"] = True

    async def go(db):
        await _seed_base(db)
        s = await Q.build_scope(_ctx(uid="nobody"))
        assert s.project_ids == []
        f = Q.vector_filter(s)
        # Atlas refuses an empty $in, so the clause is written as "no project"
        assert {"project_id": {"$eq": None}} in f["$and"]
        assert "'$in': []" not in str(f)
        assert {"source_collection": {"$ne": "recovery_reports"}} in f["$and"]
    _run(go)


def test_scope_restricted_admin_is_enforced_even_with_flag_off():
    from services.search import query as Q

    async def go(db):
        await _seed_base(db)
        await db.project_members.insert_one(
            {"tenant_id": TEN, "user_id": "adm", "project_id": P2, "role_on_project": "pm"})
        s = await Q.build_scope(_ctx(role="admin", uid="adm", scope="assigned"))
        assert s.project_ids == [P2]
        assert not s.full_admin
        f = Q.vector_filter(s)
        assert "allowed_roles" not in str(f)          # admin passes every role list
        assert "private" in str(f)                      # but is not an all-projects admin
    _run(go)


def test_scope_full_admin_sees_everything_in_own_company():
    from services.search import query as Q

    async def go(db):
        await _seed_base(db)
        s = await Q.build_scope(_ctx(role="admin", uid="boss"))
        assert s.full_admin and s.project_ids is None and s.recovery_project_ids is None
        assert Q.vector_filter(s) == {"tenant_id": {"$eq": TEN}}
        assert Q.chunk_allowed(s, {"tenant_id": TEN, "private": True, "owner_user_id": "x"})
        assert not Q.chunk_allowed(s, {"tenant_id": OTHER})
    _run(go)


def test_explicit_project_denied_when_enforced(_flags):
    from services.search import query as Q
    _flags["access"] = True

    async def go(db):
        await _seed_base(db)
        with pytest.raises(HTTPException) as e:
            await Q.build_scope(_ctx(), project_id=P2)
        assert e.value.status_code == 404
        res = await Q.search_everything(db, _ctx(), "anything", project_id=P2,
                                        embedder=_never_embedder)
        assert res["count"] == 0 and res["notice"] == "Project not found"
        # a project from another company is "not found" too
        res = await Q.search_everything(db, _ctx(), "anything", project_id="proj-x",
                                        embedder=_never_embedder)
        assert res["notice"] == "Project not found"
        # an allowed explicit project limits to that project only
        s = await Q.build_scope(_ctx(), project_id=P1)
        assert s.project_ids == [P1] and not s.include_company_level
        assert not Q.chunk_allowed(s, {"tenant_id": TEN, "project_id": None})
    _run(go)


def test_session_project_keeps_company_level_records(_flags):
    """The assistant's session project (explicit=False) is still the only
    project searched, but company-wide records (vendors, crews, knowledge
    base) stay in, still limited to the caller's company."""
    from services.search import query as Q
    _flags["access"] = True

    async def go(db):
        await _seed_base(db)
        s = await Q.build_scope(_ctx(), project_id=P1, explicit=False)
        assert s.project_ids == [P1] and s.include_company_level
        f = Q.vector_filter(s)
        assert {"$or": [{"project_id": {"$in": [P1]}}, {"project_id": {"$eq": None}}]} in f["$and"]
        assert Q.chunk_allowed(s, {"tenant_id": TEN, "project_id": P1})
        assert Q.chunk_allowed(s, {"tenant_id": TEN, "project_id": None})
        assert not Q.chunk_allowed(s, {"tenant_id": TEN, "project_id": P2})
        assert not Q.chunk_allowed(s, {"tenant_id": OTHER, "project_id": None})
        # The project is still checked: one the caller is not on is refused.
        with pytest.raises(HTTPException):
            await Q.build_scope(_ctx(), project_id=P2, explicit=False)
        # The default stays "explicit": that project only.
        s = await Q.build_scope(_ctx(), project_id=P1)
        assert not s.include_company_level
    _run(go)


def test_role_not_allowed_and_private_rules():
    from services.search.query import Scope, chunk_allowed, vector_filter
    tech = Scope(tenant_id=TEN, role="submittal_tech", user_id="u9", full_admin=False,
                 project_ids=None, include_company_level=True, recovery_project_ids=None)
    rec = {"tenant_id": TEN, "project_id": P1, "source_collection": "recovery_reports",
           "allowed_roles": ["admin", "pm", "super"]}
    assert not chunk_allowed(tech, rec)
    assert {"$or": [{"allowed_roles": {"$eq": None}},
                    {"allowed_roles": {"$eq": "submittal_tech"}}]} in vector_filter(tech)["$and"]
    pm = Scope(tenant_id=TEN, role="pm", user_id="u1", full_admin=False,
               project_ids=None, include_company_level=True, recovery_project_ids=[P1])
    assert chunk_allowed(pm, rec)
    assert not chunk_allowed(pm, {**rec, "project_id": P2})   # recovery needs membership
    assert chunk_allowed(pm, {"tenant_id": TEN, "private": True, "owner_user_id": "u1"})
    assert not chunk_allowed(pm, {"tenant_id": TEN, "private": True, "owner_user_id": "u2"})
    admin = Scope(tenant_id=TEN, role="admin", user_id="a", full_admin=False,
                  project_ids=None, include_company_level=True, recovery_project_ids=None)
    assert chunk_allowed(admin, rec)
    dated = Scope(tenant_id=TEN, role="pm", user_id="u1", full_admin=False, project_ids=None,
                  include_company_level=True, recovery_project_ids=None, kinds=["rfi"],
                  date_from="2026-01-01", date_to="2026-06-30")
    assert chunk_allowed(dated, {"tenant_id": TEN, "kind": "rfi", "date": "2026-03-01"})
    assert not chunk_allowed(dated, {"tenant_id": TEN, "kind": "rfi", "date": "2026-07-01"})
    assert not chunk_allowed(dated, {"tenant_id": TEN, "kind": "rfi", "date": None})
    assert not chunk_allowed(dated, {"tenant_id": TEN, "kind": "task", "date": "2026-03-01"})
    f = vector_filter(dated)
    assert {"kind": {"$in": ["rfi"]}} in f["$and"]
    assert {"date": {"$gte": "2026-01-01", "$lte": "2026-06-30"}} in f["$and"]


def test_text_stage_shape():
    from services.search.query import Scope, text_search_stage
    s = Scope(tenant_id=TEN, role="pm", user_id="u1", full_admin=False, project_ids=[P1],
              include_company_level=True, recovery_project_ids=[P1])
    st = text_search_stage("stair rail A1240 at grid", s)["$search"]
    assert st["index"] == "search_chunks_text"
    must = st["compound"]["must"][0]["compound"]["should"]
    assert must[0]["text"]["path"] == ["text", "title", "header"]
    assert must[1]["text"]["fuzzy"] == {"maxEdits": 1}
    assert must[1]["text"]["query"] == "stair A1240"          # only words longer than 4
    should = st["compound"]["should"]
    assert any("phrase" in c for c in should)
    assert {"equals": {"path": "activity_code", "value": "A1240",
                       "score": {"boost": {"value": 3}}}} in should
    assert st["compound"]["filter"][0] == {"equals": {"path": "tenant_id", "value": TEN}}


# ---------------------------------------------------------------- merging

def test_rrf_math():
    from services.search.query import rrf_fuse
    fused = dict(rrf_fuse(["a", "b"], ["b", "c"], k=60))
    assert fused["a"] == pytest.approx(1 / 61)
    assert fused["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert fused["c"] == pytest.approx(1 / 62)
    assert [i for i, _ in rrf_fuse(["a", "b"], ["b", "c"])] == ["b", "a", "c"]
    # ties keep first-seen order
    assert [i for i, _ in rrf_fuse(["x"], ["y"])] == ["x", "y"]


def test_collapse_keeps_best_piece_per_record():
    from services.search.query import collapse
    rows = [
        {"_id": "rfis:1:1", "source_collection": "rfis", "source_oid": "1"},
        {"_id": "rfis:2:0", "source_collection": "rfis", "source_oid": "2"},
        {"_id": "rfis:1:0", "source_collection": "rfis", "source_oid": "1"},
        {"_id": "tasks:1:0", "source_collection": "tasks", "source_oid": "1"},
    ]
    assert [r["_id"] for r in collapse(rows)] == ["rfis:1:1", "rfis:2:0", "tasks:1:0"]


def test_best_excerpt_window():
    from services.search.query import best_excerpt
    text = ("filler words here " * 60) + "the stair rail failed inspection at grid C4 " + ("tail " * 80)
    ex = best_excerpt(text, "stair rail inspection")
    assert len(ex) <= 400 and "stair rail failed inspection" in ex
    assert ex.startswith("…") and ex.endswith("…")
    assert best_excerpt("short body", "x") == "short body"


# ---------------------------------------------------------------- end to end with stub stages

def _chunk(oid, *, tenant=TEN, project=P1, kind="rfi", coll="rfis", title=None,
           body="", roles=None, private=False, owner=None, embedded=True, n=0):
    return {"_id": f"{coll}:{oid}:{n}", "tenant_id": tenant, "project_id": project, "kind": kind,
            "source_collection": coll, "source_oid": oid, "source_id": f"id-{oid}", "chunk_no": n,
            "date": "2026-11-01", "activity_code": [], "status": "open", "private": private,
            "owner_user_id": owner, "allowed_roles": roles, "title": title or f"Record {oid}",
            "url": f"/rfis?openId=id-{oid}", "header": "hdr", "body": body, "text": "hdr\n" + body,
            "embedded": embedded, "score": 1.0}


class Stages:
    """Stub search stages: return the given chunk lists, filtered by the
    Python permission check (what the Atlas filter would do)."""

    def __init__(self, monkeypatch, vec, txt, fail_text=False):
        from services.search import query as Q
        self.vec_calls = self.txt_calls = 0

        async def v(db, pipeline):
            self.vec_calls += 1
            self.vec_pipeline = pipeline
            return [dict(c) for c in vec]

        async def t(db, pipeline):
            self.txt_calls += 1
            self.txt_pipeline = pipeline
            if fail_text:
                raise RuntimeError("no Atlas Search here")
            return [dict(c) for c in txt]
        monkeypatch.setattr(Q, "_run_vector_stage", v)
        monkeypatch.setattr(Q, "_run_text_stage", t)


async def _fake_embedder(texts, input_type="query"):
    assert input_type == "query"
    return [[0.1] * 4 for _ in texts], 3


async def _never_embedder(texts, input_type="query"):
    raise AssertionError("embedder must not be called")


async def _failing(*a, **k):
    from services.search.voyage_client import VoyageUnavailable
    raise VoyageUnavailable("VOYAGE_API_KEY is not set")


async def _seed_rows(db, oids, **extra):
    for oid in oids:
        await db.rfis.insert_one({"_id": ObjectId(oid), "id": f"id-{oid}", "tenant_id": TEN,
                                  "project_id": P1, **extra.get(oid, {})})


def _oids(n):
    return [str(ObjectId()) for _ in range(n)]


def test_hybrid_merge_rerank_collapse_and_cites(monkeypatch):
    from services.search import query as Q
    a, b, c = _oids(3)
    vec = [_chunk(a, body="alpha"), _chunk(b, body="beta"), _chunk(a, body="alpha two", n=1)]
    # c is still waiting for its vector (embedded False): it is never sent
    # to the reranker and keeps its merged slot.
    txt = [_chunk(c, body="gamma rail", embedded=False), _chunk(b, body="beta")]
    st = Stages(monkeypatch, vec, txt)

    seen = {}

    async def reranker(q, docs, top_k=None):
        # put the last document first
        seen["docs"] = list(docs)
        return [(len(docs) - 1, 0.9)] + [(i, 0.5 - i * 0.01) for i in range(len(docs) - 1)]

    async def go(db):
        await _seed_base(db)
        await _seed_rows(db, [a, b, c])
        res = await Q.search_everything(db, _ctx(), "rail", embedder=_fake_embedder,
                                        reranker=reranker)
        assert res["mode"] == "hybrid" and res["notice"] is None
        assert st.vec_calls == 1 and st.txt_calls == 1
        vs = st.vec_pipeline[0]["$vectorSearch"]
        assert vs["index"] == "search_chunks_vector" and vs["numCandidates"] == 400 and vs["limit"] == 40
        assert [r["cite"] for r in res["results"]] == ["S1", "S2", "S3"]
        ids = [r["source_id"] for r in res["results"]]
        assert len(ids) == len(set(ids)) == 3            # a's two pieces collapsed
        # Merged order is b (both lists), a, c, a-piece-2. Sent to the
        # reranker: b, a, a-piece-2 (not c). The reranker put the last one
        # first, giving a-piece-2, b, c (kept slot), a; collapsed: a, b, c.
        # Without the rerank step the order would be b, a, c.
        assert len(seen["docs"]) == 3 and not any("gamma" in d for d in seen["docs"])
        assert seen["docs"][-1].endswith("alpha two")
        assert ids == [f"id-{a}", f"id-{b}", f"id-{c}"]
        assert res["results"][0]["score"] == pytest.approx(0.9)
        assert res["results"][2]["score"] == pytest.approx(round(1 / 61, 4))   # merged score kept
        assert res["results"][0]["project_name"] == "Hospital Wing"
        assert set(res) == {"count", "mode", "results", "notice"}   # no internal timings
        for r in res["results"]:
            assert set(r) == {"cite", "kind", "source_collection", "source_id", "project_id",
                              "project_name", "label", "date", "activity_code", "excerpt",
                              "score", "url"}
    _run(go)


def test_rehydrate_drops_deleted_missing_foreign_and_dev_stub(monkeypatch):
    from services.search import query as Q
    ok, gone, missing, foreign, stub, deleted_at, archived, archived_at, placeholder = _oids(9)
    everyone = (ok, gone, missing, foreign, stub, deleted_at, archived, archived_at, placeholder)
    chunks = [_chunk(x, body="rail") for x in everyone]
    Stages(monkeypatch, [], chunks)

    async def go(db):
        await _seed_base(db)
        await _seed_rows(db, [ok, gone, stub, deleted_at, archived, archived_at, placeholder], **{
            gone: {"status": "deleted"},
            stub: {"ai_model_version": __import__("services.photo_ai", fromlist=["x"]).DEV_STUB_MODEL},
            deleted_at: {"deleted_at": "2026-11-02"},
            archived: {"status": "Archived"},
            archived_at: {"archived_at": "2026-11-03"},
            placeholder: {"_placeholder": True},
        })
        # a row that exists but belongs to another company
        await db.rfis.insert_one({"_id": ObjectId(foreign), "id": "f", "tenant_id": OTHER,
                                  "project_id": "proj-x"})
        res = await Q.search_everything(db, _ctx(), "rail", embedder=_failing)
        assert [r["source_id"] for r in res["results"]] == [f"id-{ok}"]
    _run(go)


def test_rehydrate_uses_chunk_project_for_rows_without_one(monkeypatch, _flags):
    """A daily report activity row has no project_id of its own; the
    project resolved for the chunk decides."""
    from services.search import query as Q
    _flags["access"] = True
    mine, theirs = _oids(2)
    Stages(monkeypatch, [], [_chunk(mine, coll="daily_report_activities", project=P1, body="rail"),
                             _chunk(theirs, coll="daily_report_activities", project=P2, body="rail")])

    async def go(db):
        await _seed_base(db)
        for oid in (mine, theirs):
            await db.daily_report_activities.insert_one({"_id": ObjectId(oid), "id": f"id-{oid}"})
        s = await Q.build_scope(_ctx())
        hits = [_chunk(mine, coll="daily_report_activities", project=P1),
                _chunk(theirs, coll="daily_report_activities", project=P2)]
        kept = await Q.rehydrate(db, _ctx(), s, hits)
        assert [h["source_oid"] for h in kept] == [mine]
        res = await Q.search_everything(db, _ctx(), "rail", embedder=_failing)
        assert [r["source_id"] for r in res["results"]] == [f"id-{mine}"]
    _run(go)


def test_keyword_only_when_embedder_fails(monkeypatch):
    from services.search import query as Q
    a = _oids(1)[0]
    st = Stages(monkeypatch, [_chunk(a)], [_chunk(a, body="rail")])

    async def go(db):
        await _seed_base(db)
        await _seed_rows(db, [a])
        res = await Q.search_everything(db, _ctx(), "rail", embedder=_failing,
                                        reranker=_never_embedder)
        assert res["mode"] == "keyword"
        assert res["notice"] == Q.NOTICE_NO_VECTOR
        assert st.vec_calls == 0 and st.txt_calls == 1
        assert res["count"] == 1
    _run(go)


def test_gate_keeps_query_off_voyage(monkeypatch):
    from services.search import query as Q
    a = _oids(1)[0]
    st = Stages(monkeypatch, [], [_chunk(a, body="rail")])

    async def go(db):
        await _seed_base(db)
        await db.projects.update_one({"id": P1}, {"$set": {"is_cui": True}})
        await _seed_rows(db, [a])
        res = await Q.search_everything(db, _ctx(role="admin", uid="boss"), "rail",
                                        project_id=P1, embedder=_never_embedder,
                                        reranker=_never_embedder)
        assert res["mode"] == "keyword" and res["notice"] == Q.NOTICE_GATED
        assert st.vec_calls == 0 and res["count"] == 1
        # a company with AI switched off: same, even without a project
        await db.tenants.update_one({"id": TEN}, {"$set": {"ai_enabled": False}})
        res = await Q.search_everything(db, _ctx(role="admin", uid="boss"), "rail",
                                        embedder=_never_embedder)
        assert res["notice"] == Q.NOTICE_GATED
    _run(go)


def test_reranker_failure_keeps_merged_order(monkeypatch):
    from services.search import query as Q
    a, b, c = _oids(3)
    Stages(monkeypatch, [_chunk(a), _chunk(b), _chunk(c)], [_chunk(a), _chunk(b), _chunk(c)])

    async def go(db):
        await _seed_base(db)
        await _seed_rows(db, [a, b, c])
        res = await Q.search_everything(db, _ctx(), "rail", embedder=_fake_embedder,
                                        reranker=_failing)
        assert res["mode"] == "hybrid"
        assert [r["source_id"] for r in res["results"]] == [f"id-{a}", f"id-{b}", f"id-{c}"]
        assert res["results"][0]["score"] == pytest.approx(round(2 / 61, 4))
    _run(go)


def test_rerank_never_sends_gated_pieces(monkeypatch):
    from services.search import query as Q
    top = [(_chunk("1", embedded=True, body="one"), 0.3),
           (_chunk("2", embedded=False, body="secret cui"), 0.2),
           (_chunk("3", embedded=True, body="three"), 0.1)]
    seen = {}

    async def reranker(q, docs, top_k=None):
        seen["docs"] = docs
        return [(1, 0.9), (0, 0.1)]

    async def allow_all(pid):
        return True
    out = asyncio.run(Q.rerank_top("q", top, reranker, allow_all))
    assert all("secret" not in d for d in seen["docs"]) and len(seen["docs"]) == 2
    assert [d["source_oid"] for d, _ in out] == ["3", "2", "1"]   # gated piece keeps its slot


def test_rerank_checks_the_gate_again_at_query_time():
    """A project marked CUI after indexing still has embedded = True pieces
    until the worker re-queues them; their text must not go to the reranker."""
    from services.search import query as Q
    top = [(_chunk("1", project=P1, body="one"), 0.4),
           (_chunk("2", project=P2, body="now cui"), 0.3),
           (_chunk("3", project=P1, body="three"), 0.2),
           (_chunk("4", project=None, body="company"), 0.1)]
    asked, seen = [], {}

    async def allows(pid):
        asked.append(pid)
        return pid != P2

    async def reranker(q, docs, top_k=None):
        seen["docs"] = docs
        return [(2, 0.9), (1, 0.5), (0, 0.1)]
    out = asyncio.run(Q.rerank_top("q", top, reranker, allows))
    assert sorted(asked, key=str) == sorted([P1, P2, None], key=str)   # once per project
    assert not any("now cui" in d for d in seen["docs"]) and len(seen["docs"]) == 3
    assert [d["source_oid"] for d, _ in out] == ["4", "2", "3", "1"]  # "2" keeps its slot

    # a gate check that fails counts as "do not send"
    async def broken(pid):
        if pid == P1:
            raise RuntimeError("database hiccup")
        return True
    seen.clear()
    out = asyncio.run(Q.rerank_top("q", top, reranker, broken))
    assert not any(d.endswith(("one", "three")) for d in seen.get("docs", []))


def test_rerank_skips_project_marked_cui_after_indexing(monkeypatch):
    from services.search import query as Q
    a, b, c = _oids(3)
    Stages(monkeypatch, [_chunk(a, body="alpha"), _chunk(b, project=P2, body="cui text"),
                         _chunk(c, body="gamma")], [])
    seen = {}

    async def reranker(q, docs, top_k=None):
        seen["docs"] = list(docs)
        return [(i, 1.0 - i * 0.1) for i in range(len(docs))]

    async def go(db):
        await _seed_base(db)
        await db.projects.update_one({"id": P2}, {"$set": {"is_cui": True}})
        await _seed_rows(db, [a, c])
        await db.rfis.insert_one({"_id": ObjectId(b), "id": f"id-{b}", "tenant_id": TEN,
                                  "project_id": P2})
        res = await Q.search_everything(db, _ctx(role="admin", uid="boss"), "rail",
                                        embedder=_fake_embedder, reranker=reranker)
        assert res["mode"] == "hybrid" and res["count"] == 3
        assert len(seen["docs"]) == 2 and not any("cui text" in d for d in seen["docs"])
    _run(go)


def test_private_piece_hidden_when_user_id_unknown():
    from services.search.query import Scope, chunk_allowed
    anon = Scope(tenant_id=TEN, role="pm", user_id=None, full_admin=False, project_ids=None,
                 include_company_level=True, recovery_project_ids=None)
    assert not chunk_allowed(anon, {"tenant_id": TEN, "private": True, "owner_user_id": None})
    assert chunk_allowed(anon, {"tenant_id": TEN, "private": False})


def test_label_is_one_line_and_capped():
    from services.search import query as Q
    long_title = "Recovery report  for\n" + ("very long words " * 40)
    lab = Q._label({"title": long_title})
    assert len(lab) <= Q.LABEL_CHARS and lab.endswith("…") and "\n" not in lab
    assert lab.startswith("Recovery report for very")
    assert Q._label({"title": "", "header": "RFI 12 | Stair rail"}) == "RFI 12"
    assert len(Q._label({"header": "x" * 1000})) == Q.LABEL_CHARS
    assert Q._label({"kind": "daily_report"}) == "daily report"


def test_unavailable_when_both_searches_fail(monkeypatch):
    from services.search import query as Q
    Stages(monkeypatch, [], [], fail_text=True)

    async def go(db):
        await _seed_base(db)
        res = await Q.search_everything(db, _ctx(), "rail", embedder=_failing)
        assert res["mode"] == "unavailable" and res["count"] == 0
        assert res["notice"] == Q.NOTICE_UNAVAILABLE
    _run(go)


def test_flag_off_returns_nothing(_flags):
    from services.search import query as Q
    _flags["search"] = False
    res = asyncio.run(Q.search_everything(None, _ctx(), "rail", embedder=_never_embedder))
    assert res["mode"] == "unavailable" and res["notice"] == "Search is turned off."


# ---------------------------------------------------------------- endpoint

def _app(override_ctx=None):
    from auth import require_tenant
    from routers.search_router import router
    app = FastAPI()
    app.include_router(router, prefix="/api")
    if override_ctx is not None:
        app.dependency_overrides[require_tenant] = lambda: override_ctx
    return app


def test_endpoint_needs_sign_in():
    client = TestClient(_app())
    r = client.get("/api/search/everything", params={"q": "rail"})
    assert r.status_code in (401, 403)


def test_endpoint_flag_off_is_503(_flags):
    _flags["search"] = False
    client = TestClient(_app(_ctx()))
    r = client.get("/api/search/everything", params={"q": "rail"})
    assert r.status_code == 503 and r.json()["detail"] == "Search is turned off."


def test_endpoint_flag_on_passes_arguments(monkeypatch):
    import routers.search_router as sr
    got = {}

    async def fake(db, ctx, q, **kw):
        got.update(q=q, tenant=ctx["tenant_id"], **kw)
        return {"count": 0, "mode": "keyword", "results": [], "notice": None}

    async def no_project(ctx, pid, projection=None):
        got["checked_project"] = pid
    monkeypatch.setattr(sr, "search_everything", fake)
    monkeypatch.setattr(sr, "require_project_if_given", no_project)
    monkeypatch.setattr(sr, "get_db", lambda: None)
    client = TestClient(_app(_ctx()))
    r = client.get("/api/search/everything", params={
        "q": "stair rail", "project_id": P1, "kind": "rfi,delay",
        "date_from": "2026-01-01", "date_to": "2026-02-01", "limit": 5})
    assert r.status_code == 200, r.text
    assert got == {"q": "stair rail", "tenant": TEN, "project_id": P1, "kinds": ["rfi", "delay"],
                   "date_from": "2026-01-01", "date_to": "2026-02-01", "limit": 5,
                   "checked_project": P1}
    r = client.get("/api/search/everything", params={"q": "x", "date_from": "yesterday"})
    assert r.status_code == 422


def test_endpoint_does_not_replace_header_search():
    """GET /api/search (the header search box) is a different route."""
    from routers.search_router import router
    assert [r.path for r in router.routes] == ["/search/everything"]

