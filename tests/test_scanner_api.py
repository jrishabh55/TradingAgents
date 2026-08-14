from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apps.api.routes import scanners as scanners_module
from tests.scanner_utils import make_store

DEF = {"logic": "AND", "children": [
    {"timeframe": "1d", "left": {"field": "close"}, "op": ">", "right": {"const": 100}}]}


@pytest.fixture()
def env(tmp_path):
    store = make_store(tmp_path, {"HI": [101.0] * 30, "LO": [99.0] * 30})
    current_user = {"id": "user_a"}
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):  # noqa: ANN001
        request.state.user_id = current_user["id"]
        return await call_next(request)

    app.include_router(scanners_module.router, prefix="/api")
    return TestClient(app), store, current_user


def test_crud_and_ownership(env):
    client, store, user = env
    r = client.post("/api/scanners", json={"name": "Mine", "definition": DEF})
    assert r.status_code == 201
    sid = r.json()["id"]

    assert any(s["id"] == sid for s in client.get("/api/scanners").json())

    user["id"] = "user_b"
    assert not any(s["id"] == sid for s in client.get("/api/scanners").json())
    assert client.put(f"/api/scanners/{sid}",
                      json={"name": "Stolen", "definition": DEF}).status_code == 404
    assert client.delete(f"/api/scanners/{sid}").status_code == 404
    assert client.post(f"/api/scanners/{sid}/run").status_code == 404

    user["id"] = "user_a"
    assert client.put(f"/api/scanners/{sid}",
                      json={"name": "Renamed", "definition": DEF}).status_code == 200
    assert client.delete(f"/api/scanners/{sid}").status_code == 204


def test_prebuilt_visible_but_read_only(env):
    client, store, _ = env
    store.upsert_prebuilt("Golden cross", "d", DEF)
    listed = client.get("/api/scanners").json()
    pb = next(s for s in listed if s["prebuilt"])
    assert client.put(f"/api/scanners/{pb['id']}",
                      json={"name": "Hacked", "definition": DEF}).status_code == 403
    assert client.delete(f"/api/scanners/{pb['id']}").status_code == 403
    assert client.post(f"/api/scanners/{pb['id']}/run").status_code == 200


def test_run_and_preview_return_matches(env):
    client, _, _ = env
    r = client.post("/api/scanners/preview", json={"definition": DEF})
    assert r.status_code == 200
    assert [m["symbol"] for m in r.json()["matches"]] == ["HI"]

    created = client.post("/api/scanners", json={"name": "S", "definition": DEF}).json()
    r2 = client.post(f"/api/scanners/{created['id']}/run")
    assert [m["symbol"] for m in r2.json()["matches"]] == ["HI"]


def test_engine_error_returns_422_not_500(env, monkeypatch):
    client, _, _ = env

    class BoomEngine:
        def run(self, definition):
            raise KeyError("boom")

    monkeypatch.setattr(scanners_module, "get_engine", lambda: BoomEngine())
    r = client.post("/api/scanners/preview", json={"definition": DEF})
    assert r.status_code == 422
    assert "scan failed" in r.json()["detail"]


def test_status_shape(env):
    client, store, _ = env
    r = client.get("/api/scanners/status")
    assert r.status_code == 200
    body = r.json()
    assert body["universe"] == 2  # HI + LO, seeded by make_store
    assert set(body["latest"]) == {"1d", "1h", "15m", "5m"}
    assert body["latest"]["1d"] is not None  # make_store seeds 1d bars
    assert body["latest"]["1h"] is None      # no 1h bars seeded


def test_status_requires_auth():
    app = FastAPI()
    app.include_router(scanners_module.router, prefix="/api")
    client = TestClient(app)
    assert client.get("/api/scanners/status").status_code in (401, 403, 422)


def test_invalid_definition_422(env):
    client, _, _ = env
    bad = {"logic": "AND", "children": [
        {"timeframe": "1d", "left": {"field": "nope"}, "op": ">", "right": {"const": 1}}]}
    assert client.post("/api/scanners/preview", json={"definition": bad}).status_code == 422
    assert client.post("/api/scanners", json={"name": "x", "definition": bad}).status_code == 422
    huge = {"logic": "AND", "children": [
        {"timeframe": "1d", "left": {"field": "close"}, "op": ">",
         "right": {"const": 1}}] * 60}
    assert client.post("/api/scanners/preview", json={"definition": huge}).status_code == 422
