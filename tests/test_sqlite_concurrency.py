"""Regression: the audit store and usage analytics share one SQLite file through separate connections.

Incident: Analytics.summary() ran a retention DELETE without committing. The analytics connection kept the write
lock, so the audit INSERT at the end of every analysis failed with "database is locked" after ~5 s. Tests used
separate :memory: databases, so they never exercised the shared file. These tests do.
"""

import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.config import DATA_DIR
from app.models.domain import Claim, ExecutionRecord
from app.observability.analytics import Analytics
from app.observability.audit import AuditStore

CLAIM = json.loads((DATA_DIR / "claims" / "04_ambiguous_emergency_unknown.json").read_text())


def _record(i: int) -> ExecutionRecord:
    return ExecutionRecord.model_validate({
        "execution_id": f"EXE-{i:032x}", "claim_id": "CLM-1", "timestamp": datetime.now(timezone.utc),
        "workflow_version": "w", "prompt_version": "p", "model": "m", "llm_provider": "mock",
        "recommendation": "HUMAN_REVIEW", "human_review_required": True, "human_review_reason": [],
        "missing_information": [], "risk_level": "LOW", "risk_factors": [], "confidence": 0.5,
        "deterministic_outcome": "PASS", "llm_recommendation": None, "reasoning_summary": "",
        "policy_evidence": [], "retrieved_policy_ids": [], "retrieved_policy_versions": {},
        "tools_executed": [], "tool_results": [], "routing_trail": [], "latency_ms": 1, "llm_latency_ms": 1,
        "input_tokens": 1, "output_tokens": 1, "estimated_cost": 0, "llm_error": None, "claim": Claim(claim_id="CLM-1"),
    })


def test_dashboard_reads_never_leave_a_transaction_open(tmp_path):
    db = str(tmp_path / "shared.db")
    audit, analytics = AuditStore(db), Analytics(db)
    analytics.record("page_view", "v1")
    analytics.summary(30)                          # runs the retention purge
    assert analytics._conn.in_transaction is False
    audit.save(_record(1))                         # used to fail: database is locked
    assert audit.get(f"EXE-{1:032x}") is not None


def test_opening_the_dashboard_does_not_break_the_next_analysis(tmp_path, monkeypatch):
    import app.main as main

    monkeypatch.setenv("AUDIT_DB_PATH", str(tmp_path / "prod-like.db"))
    monkeypatch.setenv("ADMIN_TOKEN", "a" * 40)
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    for cached in (main.get_audit_store, main.get_analytics, main.get_investigator, main.get_usage_guard):
        cached.cache_clear()
    try:
        client = TestClient(main.app)
        admin = {"Authorization": "Bearer " + "a" * 40}
        stream = {"Accept": "application/x-ndjson"}
        assert client.post("/claims/analyze", json=CLAIM, headers=stream).status_code == 200
        assert client.get("/admin/stats", headers=admin).status_code == 200      # the owner opens the dashboard
        client.cookies.set("cp_notrack", "1")                                     # owner's own visits aren't tracked
        client.get("/")
        last = json.loads(client.post("/claims/analyze", json=CLAIM, headers=stream).text.strip().splitlines()[-1])
        assert last["event"] == "decision", last                                  # used to be "error" after ~5 s
        assert client.post("/claims/analyze", json=CLAIM).status_code == 200      # plain JSON path too
    finally:
        for cached in (main.get_audit_store, main.get_analytics, main.get_investigator, main.get_usage_guard):
            cached.cache_clear()
