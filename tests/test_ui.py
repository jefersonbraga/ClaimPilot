"""The demo UI is a thin static layer over the public API."""

import pytest
from fastapi.testclient import TestClient

from app.limits import UsageGuard
from app.main import app, get_audit_store, get_investigator, get_usage_guard
from app.models.domain import Claim


@pytest.fixture
def client(make_investigator, audit):
    app.dependency_overrides[get_investigator] = lambda: make_investigator()
    app.dependency_overrides[get_audit_store] = lambda: audit
    app.dependency_overrides[get_usage_guard] = lambda: UsageGuard(per_minute=1000, daily_analyses=1000,
                                                                   daily_budget_usd=100, counts_spend=False)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_root_serves_the_workbench_and_assets(client):
    page = client.get("/")
    assert page.status_code == 200 and "Claims Exception Resolution Workbench" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/docs").status_code == 200


def test_demo_scenarios_are_valid_claims_with_one_default(client):
    scenarios = client.get("/demo/scenarios").json()
    assert sum(bool(s.get("default")) for s in scenarios) == 1
    assert next(s for s in scenarios if s.get("default"))["id"] == "ambiguous-emergency"
    for s in scenarios:
        Claim(**s["claim"])


def test_every_demo_scenario_analyzes_through_the_api(client):
    for s in client.get("/demo/scenarios").json():
        r = client.post("/claims/analyze", json=s["claim"])
        assert r.status_code == 200, s["id"]
        assert client.get(f"/executions/{r.json()['execution_id']}").status_code == 200


def test_latest_evaluation_is_404_or_a_summary_without_per_case_rows(client):
    r = client.get("/evals/latest")
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert "unsafe_autonomous_actions" in r.json() and "results" not in r.json()
