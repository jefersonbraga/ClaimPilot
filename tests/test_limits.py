"""A public demo must not let anyone burn the operator's LLM budget."""

import json

import pytest
from fastapi.testclient import TestClient

from app.config import DATA_DIR
from app.limits import UsageGuard
from app.main import app, get_audit_store, get_investigator, get_usage_guard

CLAIM = json.loads((DATA_DIR / "claims" / "01_obvious_approval.json").read_text())


@pytest.fixture
def client_with(make_investigator, audit):
    def _make(guard):
        app.dependency_overrides[get_investigator] = lambda: make_investigator()
        app.dependency_overrides[get_audit_store] = lambda: audit
        app.dependency_overrides[get_usage_guard] = lambda: guard
        return TestClient(app)
    yield _make
    app.dependency_overrides.clear()


def test_per_client_rate_limit_returns_429(client_with):
    client = client_with(UsageGuard(per_minute=2, daily_analyses=100, daily_budget_usd=1, counts_spend=False))
    assert [client.post("/claims/analyze", json=CLAIM).status_code for _ in range(3)] == [200, 200, 429]


def test_daily_analysis_cap_applies_to_everyone(client_with):
    client = client_with(UsageGuard(per_minute=100, daily_analyses=1, daily_budget_usd=1, counts_spend=False))
    assert client.post("/claims/analyze", json=CLAIM).status_code == 200
    r = client.post("/claims/analyze", json=CLAIM)
    assert r.status_code == 429 and "Daily demo limit" in r.json()["detail"]


def test_llm_budget_blocks_further_spend_for_real_providers():
    guard = UsageGuard(per_minute=100, daily_analyses=100, daily_budget_usd=0.01, counts_spend=True)
    guard.check("a")
    guard.record(0.011)  # the recorded estimated cost of the previous analysis
    with pytest.raises(Exception) as e:
        guard.check("b")
    assert e.value.status_code == 429 and "budget" in e.value.detail


def test_oversized_fields_are_rejected_before_reaching_the_llm(client_with):
    client = client_with(UsageGuard(per_minute=100, daily_analyses=100, daily_budget_usd=1, counts_spend=False))
    assert client.post("/claims/analyze", json={**CLAIM, "provider": "x" * 5000}).status_code == 422


def test_health_reports_limits(client_with):
    client = client_with(UsageGuard(per_minute=7, daily_analyses=50, daily_budget_usd=2, counts_spend=True))
    limits = client.get("/health").json()["demo_limits"]
    assert limits["per_minute_per_client"] == 7 and limits["daily_llm_budget_usd"]["limit"] == 2
