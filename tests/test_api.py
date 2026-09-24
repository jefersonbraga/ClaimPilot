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


def test_analyze_can_stream_per_node_progress(client):
    """Same endpoint; with Accept: application/x-ndjson it reports each LangGraph node as it completes."""
    r = client.post("/claims/analyze", json=claim_json("04_ambiguous_emergency_unknown"),
                    headers={"Accept": "application/x-ndjson"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in r.text.splitlines()]
    nodes = [e["node"] for e in events if e["event"] == "node"]
    assert nodes == ["validate_claim", "retrieve_policy", "check_eligibility", "check_authorization",
                     "refine_retrieval", "analyze_claim", "evaluate_risk", "escalate_to_human", "record_audit"]
    decision = events[-1]
    assert decision["event"] == "decision" and decision["decision"]["recommendation"] == "HUMAN_REVIEW"
    assert client.get(f"/executions/{decision['decision']['execution_id']}").status_code == 200


def test_caller_can_pick_the_model_provider_per_analysis(client, make_investigator):
    """Same claim, same rules and guardrails; only the model changes. Unknown providers are refused."""
    from fastapi import HTTPException

    from app.main import get_investigator_selector
    from tests.conftest import ScriptedLLM

    class OtherModel(ScriptedLLM):
        provider, model = "groq", "other-model"

    other = make_investigator(OtherModel("not json"))  # any model; guardrails keep the outcome safe

    def select(provider):
        if provider != "groq":
            raise HTTPException(422, f"Provider '{provider}' is not available on this server.")
        return other

    app.dependency_overrides[get_investigator_selector] = lambda: select

    default = client.post("/claims/analyze", json=claim_json("01_obvious_approval")).json()
    picked = client.post("/claims/analyze?provider=groq", json=claim_json("01_obvious_approval")).json()
    assert client.get(f"/executions/{default['execution_id']}").json()["llm_provider"] == "mock"
    assert client.get(f"/executions/{picked['execution_id']}").json()["model"] == "other-model"
    assert picked["recommendation"] == "HUMAN_REVIEW"  # invalid model output -> safe fallback, never autonomous
    assert client.post("/claims/analyze?provider=nope", json=claim_json("01_obvious_approval")).status_code == 422
    assert client.post("/claims/analyze?provider=Bad!", json=claim_json("01_obvious_approval")).status_code == 422


def test_health_lists_available_providers(client):
    providers = client.get("/health").json()["available_providers"]
    assert providers and providers[0]["default"] is True
