from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.scanner.store import reset_scanner_store_for_tests


def test_scanner_routes_mounted_and_prebuilt_seeded(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANNER_DB_PATH", str(tmp_path / "scanner.sqlite"))
    monkeypatch.setenv("SCANNER_INGEST", "0")
    monkeypatch.setenv("WEBAPP_DB_PATH", str(tmp_path / "runs.sqlite"))
    reset_scanner_store_for_tests(tmp_path / "scanner.sqlite")

    # create_app() calls load_dotenv(), which would pull the developer's real
    # .env (live Clerk vars) back into the environment and make every request
    # 401. Same guard used by tests/test_levels.py and tests/test_preflight.py
    # for full-app TestClients.
    from apps.api.auth import reset_verifier_for_tests

    monkeypatch.setattr("apps.api.app.load_dotenv", lambda *a, **k: None)
    for var in ("CLERK_JWKS_URL", "CLERK_ISSUER", "WEBAPP_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    reset_verifier_for_tests()

    from apps.api.app import create_app
    try:
        with TestClient(create_app()) as client:
            r = client.get("/api/scanners")
            assert r.status_code == 200
            assert len([s for s in r.json() if s["prebuilt"]]) == 19
    finally:
        reset_verifier_for_tests()
