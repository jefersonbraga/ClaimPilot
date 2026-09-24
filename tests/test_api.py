import json

import pytest
from fastapi.testclient import TestClient

from app.config import DATA_DIR
from app.limits import UsageGuard
from app.main import app, get_audit_store, get_investigator, get_usage_guard


@pytest.fixture
def client(make_investigator, audit):
    app.dependency_overrides[get_investigator] = lambda: make_investigator()
    app.dependency_overrides[get_audit_store] = lambda: audit
    app.dependency_overrides[get_usage_guard] = lambda: UsageGuard(per_minute=1000, daily_analyses=1000,
                                                                   daily_budget_usd=100, counts_spend=False)
    yield TestClient(app)
    app.dependency_overrides.clear()


def claim_json(name):
    return json.loads((DATA_DIR / "claims" / f"{name}.json").read_text())


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["policies_loaded"] >= 5


def test_analyze_then_audit_and_replay(client):
    decision = client.post("/claims/analyze", json=claim_json("04_ambiguous_emergency_unknown")).json()
    assert decision["recommendation"] == "HUMAN_REVIEW"

    replay = client.get(f"/executions/{decision['execution_id']}").json()
    assert replay["retrieved_policy_versions"]["POL-MRI-001"] == "2.0"
    assert replay["human_review_reason"] and replay["routing_trail"]
    assert "chain_of_thought" not in replay

    history = client.get("/claims/CLM-92811/audit").json()
    assert [r["execution_id"] for r in history] == [decision["execution_id"]]


def test_malformed_claim_is_rejected(client):
    assert client.post("/claims/analyze", json={"claim_id": "X", "amount": "lots"}).status_code == 422


def test_unknown_execution_is_404(client):
    assert client.get("/executions/EXE-nope").status_code == 404
