import json
import logging

import pytest

from app.config import DATA_DIR, Settings
from app.llm.client import MockLLM
from app.models.domain import Claim
from app.observability.audit import AuditStore
from app.retrieval.policy_store import get_policy_store
from app.workflows.claim_graph import ClaimInvestigator

logging.getLogger("claimpilot").setLevel(logging.WARNING)


class ScriptedLLM:
    """Returns a fixed model response, to prove guardrails hold when the model misbehaves."""

    provider, model = "scripted", "scripted-test"

    def __init__(self, response: dict | str):
        self.response = response if isinstance(response, str) else json.dumps(response)

    def complete(self, system: str, user: str) -> tuple[str, int, int]:
        return self.response, 100, 50


@pytest.fixture
def audit(tmp_path):
    return AuditStore(str(tmp_path / "audit.db"))


@pytest.fixture
def make_investigator(audit):
    def _make(llm=None):
        return ClaimInvestigator(llm=llm or MockLLM(), policy_store=get_policy_store(), audit=audit,
                                 settings=Settings(audit_db_path=":memory:"))
    return _make


@pytest.fixture
def demo_claim():
    def _load(name: str, **overrides) -> Claim:
        data = json.loads((DATA_DIR / "claims" / f"{name}.json").read_text())
        return Claim(**{**data, **overrides})
    return _load


@pytest.fixture(autouse=True)
def _fresh_general_rate_limit():
    """The general per-client limiter is process-wide; keep tests independent of each other."""
    from app.main import request_limiter
    request_limiter.reset()
    yield
    request_limiter.reset()
