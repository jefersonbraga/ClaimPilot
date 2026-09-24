"""FastAPI entrypoint."""

import json
from functools import lru_cache
from pathlib import Path
from statistics import mean

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import DATA_DIR, ROOT_DIR, WORKFLOW_VERSION, get_settings
from app.llm.client import get_llm
from app.limits import UsageGuard
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

STATIC_DIR = Path(__file__).parent / "static"
EVAL_RESULTS = ROOT_DIR / "evals" / "results" / "latest.json"
EVAL_REFERENCE_DIR = ROOT_DIR / "evals" / "reference"  # committed results from real-model runs
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@lru_cache(maxsize=1)
def get_audit_store() -> AuditStore:
    return AuditStore(get_settings().audit_db_path)


@lru_cache(maxsize=1)
def get_usage_guard() -> UsageGuard:
    s = get_settings()
    return UsageGuard(per_minute=s.rate_limit_per_minute, daily_analyses=s.daily_max_analyses,
                      daily_budget_usd=s.daily_llm_budget_usd, counts_spend=s.resolved_provider != "mock")


@lru_cache(maxsize=1)
def get_investigator() -> ClaimInvestigator:
    settings = get_settings()
    return ClaimInvestigator(llm=get_llm(settings), policy_store=get_policy_store(), audit=get_audit_store(), settings=settings)


@app.get("/health")
def health(investigator: ClaimInvestigator = Depends(get_investigator), guard: UsageGuard = Depends(get_usage_guard)):
    return {
        "status": "ok",
        "llm_provider": investigator.llm.provider,
        "model": investigator.llm.model,
        "prompt_version": PROMPT_VERSION,
        "workflow_version": WORKFLOW_VERSION,
        "policies_loaded": len(get_policy_store().policies),
        "demo_limits": guard.status(),
    }


@app.post("/claims/analyze", response_model=ClaimDecision)
def analyze_claim(claim: Claim, request: Request, investigator: ClaimInvestigator = Depends(get_investigator),
                  guard: UsageGuard = Depends(get_usage_guard)):
    guard.check(request.client.host if request.client else "unknown")  # 429 before any LLM spend
    decision = investigator.investigate(claim)
    guard.record(decision.estimated_cost)
    return decision


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


# --------------------------------------------------------------------------- demo UI (read-only helpers)


@app.get("/", include_in_schema=False)
def demo_ui():
    """Single-page demo workbench (plain HTML/CSS/JS in app/static). It only calls the public API."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/demo/scenarios", tags=["demo"])
def demo_scenarios():
    """Predefined synthetic demo claims, described by data/claims/scenarios.json."""
    claims_dir = DATA_DIR / "claims"
    manifest = json.loads((claims_dir / "scenarios.json").read_text())
    return [{**s, "claim": json.loads((claims_dir / s["file"]).read_text())} for s in manifest]


def _eval_summary(path: Path) -> dict:
    summary = json.loads(path.read_text())
    summary.pop("results", None)
    return summary


@app.get("/evals/latest", tags=["demo"])
def latest_evaluation():
    """Summary of the most recent `python -m evals.run`, plus committed real-model reference runs.
    404 if the suite has not been run."""
    if not EVAL_RESULTS.exists():
        raise HTTPException(404, "No evaluation results yet. Run: python -m evals.run")
    return {**_eval_summary(EVAL_RESULTS),
            "reference_runs": [_eval_summary(p) for p in sorted(EVAL_REFERENCE_DIR.glob("*.json"))]}
