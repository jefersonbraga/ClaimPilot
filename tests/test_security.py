"""OWASP-aligned HTTP hardening: secure headers, resource limits, safe errors, unguessable IDs."""

import json

import pytest
from fastapi.testclient import TestClient

from app.config import DATA_DIR
from app.limits import UsageGuard
from app.main import app, get_audit_store, get_investigator, get_usage_guard, request_limiter

CLAIM = json.loads((DATA_DIR / "claims" / "01_obvious_approval.json").read_text())


@pytest.fixture
def client(make_investigator, audit):
    request_limiter.reset()
    app.dependency_overrides[get_investigator] = lambda: make_investigator()
    app.dependency_overrides[get_audit_store] = lambda: audit
    app.dependency_overrides[get_usage_guard] = lambda: UsageGuard(per_minute=1000, daily_analyses=1000,
                                                                   daily_budget_usd=100, counts_spend=False)
    yield TestClient(app, base_url="https://testserver")
    app.dependency_overrides.clear()
    request_limiter.reset()


@pytest.mark.parametrize("path", ["/", "/health", "/executions", "/static/app.js"])
def test_secure_headers_on_every_response(client, path):
    h = client.get(path).headers
    assert h["strict-transport-security"] == "max-age=31536000; includeSubDomains"
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in h["permissions-policy"]
    assert h["cross-origin-opener-policy"] == "same-origin"
    assert h["cross-origin-resource-policy"] == "same-origin"
    assert h["cross-origin-embedder-policy"] == "require-corp"
    csp = h["content-security-policy"]
    assert "script-src 'self';" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert "upgrade-insecure-requests" in csp


def test_hsts_only_over_https(make_investigator, audit):
    plain = TestClient(app)  # http://testserver
    h = plain.get("/health").headers
    assert "strict-transport-security" not in h and "upgrade-insecure-requests" not in h["content-security-policy"]


def test_swagger_ui_gets_a_narrow_csp_exception(client):
    h = client.get("/docs").headers
    assert "https://cdn.jsdelivr.net" in h["content-security-policy"]
    assert "cross-origin-embedder-policy" not in h
    assert client.get("/redoc").status_code == 404  # unused docs UI disabled


def test_api_responses_are_not_cached(client):
    client.post("/claims/analyze", json=CLAIM)
    assert client.get("/executions").headers["cache-control"] == "no-store"
    assert "cache-control" not in client.get("/static/app.js").headers


def test_oversized_and_unsized_bodies_are_rejected(client):
    big = json.dumps({**CLAIM, "padding": "x" * 70_000})
    assert client.post("/claims/analyze", content=big, headers={"Content-Type": "application/json"}).status_code == 413
    chunked = client.post("/claims/analyze", content=iter([json.dumps(CLAIM).encode()]),
                          headers={"Content-Type": "application/json"})
    assert chunked.status_code == 411


def test_cross_site_form_posts_cannot_trigger_an_analysis(client):
    """CSRF: a plain HTML form can only send form/text bodies; the API accepts JSON only."""
    r = client.post("/claims/analyze", content=json.dumps(CLAIM), headers={"Content-Type": "text/plain"})
    assert r.status_code == 422


def test_general_rate_limit_applies_to_cheap_endpoints(client, monkeypatch):
    monkeypatch.setattr(request_limiter, "per_minute", 3)
    codes = [client.get("/health").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    assert client.get("/health").headers["x-frame-options"] == "DENY"  # headers even on rejections


def test_execution_ids_are_unguessable_and_validated(client):
    run = client.post("/claims/analyze", json=CLAIM).json()
    assert len(run["execution_id"]) == len("EXE-") + 32  # 128-bit random, shareable-link safe
    assert client.get("/executions/../../etc/passwd").status_code == 404
    assert client.get("/executions/EXE-<script>").status_code == 422
    assert client.get("/claims/<script>/audit").status_code == 422


def test_stream_failures_do_not_leak_internal_details(client, make_investigator):
    class Broken:
        llm = make_investigator().llm

        def investigate_stream(self, claim, visitor=None):
            raise RuntimeError("connection to http://10.0.0.5:8000 failed: secret-token-123")
            yield  # pragma: no cover

    app.dependency_overrides[get_investigator] = lambda: Broken()
    r = client.post("/claims/analyze", json=CLAIM, headers={"Accept": "application/x-ndjson"})
    event = json.loads(r.text.strip().splitlines()[-1])
    assert event["event"] == "error" and "10.0.0.5" not in event["detail"] and "secret" not in event["detail"]
