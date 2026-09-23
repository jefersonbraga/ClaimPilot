"""FastAPI entrypoint."""

from functools import lru_cache
from statistics import mean

from fastapi import Depends, FastAPI, HTTPException

from app.config import WORKFLOW_VERSION, get_settings
from app.llm.client import get_llm
from app.llm.prompts import PROMPT_VERSION
from app.models.domain import Claim, ClaimDecision, ExecutionRecord
from app.observability.audit import AuditStore
from app.retrieval.policy_store import get_policy_store
from app.workflows.claim_graph import ClaimInvestigator

app = FastAPI(
    title="ClaimPilot — Healthcare Claims AI Investigator",
    description="AI-assisted investigation of synthetic claims exceptions. Synthetic data only. Not a claims adjudication system.",
    version="0.1.0",
)


@lru_cache(maxsize=1)
def get_audit_store() -> AuditStore:
    return AuditStore(get_settings().audit_db_path)


@lru_cache(maxsize=1)
def get_investigator() -> ClaimInvestigator:
    settings = get_settings()
    return ClaimInvestigator(llm=get_llm(settings), policy_store=get_policy_store(), audit=get_audit_store(), settings=settings)


@app.get("/health")
def health(investigator: ClaimInvestigator = Depends(get_investigator)):
    return {
        "status": "ok",
        "llm_provider": investigator.llm.provider,
        "model": investigator.llm.model,
        "prompt_version": PROMPT_VERSION,
        "workflow_version": WORKFLOW_VERSION,
        "policies_loaded": len(get_policy_store().policies),
    }


@app.post("/claims/analyze", response_model=ClaimDecision)
def analyze_claim(claim: Claim, investigator: ClaimInvestigator = Depends(get_investigator)):
    return investigator.investigate(claim)


@app.get("/claims/{claim_id}/audit", response_model=list[ExecutionRecord])
def claim_audit(claim_id: str, audit: AuditStore = Depends(get_audit_store)):
    records = audit.for_claim(claim_id)
    if not records:
        raise HTTPException(404, f"No executions recorded for claim {claim_id}")
    return records


@app.get("/executions/{execution_id}", response_model=ExecutionRecord)
def replay_execution(execution_id: str, audit: AuditStore = Depends(get_audit_store)):
    """Decision replay: evidence, policy versions, tool results, model/prompt/workflow versions and routing trail."""
    record = audit.get(execution_id)
    if record is None:
        raise HTTPException(404, f"Execution {execution_id} not found")
    return record


@app.get("/metrics")
def metrics(audit: AuditStore = Depends(get_audit_store)):
    records = audit.all()
    if not records:
        return {"executions": 0}
    by_outcome: dict[str, int] = {}
    for r in records:
        by_outcome[r.recommendation.value] = by_outcome.get(r.recommendation.value, 0) + 1
    return {
        "executions": len(records),
        "by_recommendation": by_outcome,
        "human_review_rate": round(sum(r.human_review_required for r in records) / len(records), 3),
        "avg_latency_ms": round(mean(r.latency_ms for r in records), 2),
        "avg_llm_latency_ms": round(mean(r.llm_latency_ms for r in records), 2),
        "avg_tokens": round(mean(r.input_tokens + r.output_tokens for r in records), 1),
        "total_estimated_cost": round(sum(r.estimated_cost for r in records), 6),
    }
