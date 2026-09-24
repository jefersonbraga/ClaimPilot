"""Execution history in the public demo is scoped to an anonymous browser cookie."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import DATA_DIR
from app.limits import UsageGuard
from app.main import VISITOR_COOKIE, app, get_audit_store, get_investigator, get_usage_guard
from app.observability.audit import AuditStore

CLAIM = json.loads((DATA_DIR / "claims" / "04_ambiguous_emergency_unknown.json").read_text())


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "audit.db")


@pytest.fixture
def clients(db_path):
    audit = AuditStore(db_path)
    app.dependency_overrides[get_audit_store] = lambda: audit
    app.dependency_overrides[get_investigator] = lambda: _investigator(audit)
    app.dependency_overrides[get_usage_guard] = lambda: UsageGuard(per_minute=1000, daily_analyses=1000,
                                                                   daily_budget_usd=100, counts_spend=False)
    yield TestClient(app), TestClient(app)   # two browsers: separate cookie jars
    app.dependency_overrides.clear()


def _investigator(audit):
    from app.config import Settings
    from app.llm.client import MockLLM
    from app.retrieval.policy_store import get_policy_store
    from app.workflows.claim_graph import ClaimInvestigator
    return ClaimInvestigator(llm=MockLLM(), policy_store=get_policy_store(), audit=audit,
                             settings=Settings(audit_db_path=":memory:"))


def test_each_visitor_sees_only_their_own_history(clients):
    alice, bob = clients
    first = alice.post("/claims/analyze", json=CLAIM).json()
    second = alice.post("/claims/analyze", json={**CLAIM, "emergency_indicator": True, "retro_auth_requested": True}).json()

    mine = alice.get("/executions").json()
    assert [r["execution_id"] for r in mine] == [second["execution_id"], first["execution_id"]]  # newest first
    assert [r["recommendation"] for r in mine] == ["APPROVE", "HUMAN_REVIEW"]
    assert "claim" not in mine[0] and "provider" not in mine[0]  # no free-text claim fields in the list

    assert bob.get("/executions").json() == []
    assert bob.get(f"/claims/{CLAIM['claim_id']}/audit").status_code == 404
    assert len(alice.get(f"/claims/{CLAIM['claim_id']}/audit").json()) == 2


def test_a_single_decision_can_be_shared_by_link(clients):
    alice, bob = clients
    run = alice.post("/claims/analyze", json=CLAIM).json()
    shared = bob.get(f"/executions/{run['execution_id']}")
    assert shared.status_code == 200 and shared.json()["recommendation"] == "HUMAN_REVIEW"


def test_streamed_analyses_are_recorded_in_the_visitors_history(clients):
    alice, _ = clients
    r = alice.post("/claims/analyze", json=CLAIM, headers={"Accept": "application/x-ndjson"})
    decision = json.loads(r.text.strip().splitlines()[-1])["decision"]
    assert [e["execution_id"] for e in alice.get("/executions").json()] == [decision["execution_id"]]


def test_history_filters_and_limits(clients):
    alice, _ = clients
    for _ in range(3):
        alice.post("/claims/analyze", json=CLAIM)
    alice.post("/claims/analyze", json={**CLAIM, "claim_id": "CLM-OTHER"})
    assert len(alice.get("/executions?limit=2").json()) == 2
    assert {r["claim_id"] for r in alice.get("/executions?claim_id=CLM-OTHER").json()} == {"CLM-OTHER"}


def test_cookie_is_anonymous_http_only_and_only_its_hash_is_stored(clients, db_path):
    alice, _ = clients
    r = alice.post("/claims/analyze", json=CLAIM)
    header = r.headers["set-cookie"].lower()
    assert VISITOR_COOKIE in header and "httponly" in header and "samesite=lax" in header
    token = alice.cookies.get(VISITOR_COOKIE)
    stored = sqlite3.connect(db_path).execute("SELECT visitor FROM executions").fetchone()[0]
    assert stored and token not in stored and len(stored) == 32

    # A forged or malformed cookie is replaced, not trusted.
    forged = TestClient(app)
    forged.cookies.set(VISITOR_COOKIE, "x")
    assert "set-cookie" in forged.get("/health").headers
