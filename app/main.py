"""FastAPI entrypoint."""

import hashlib
import hmac
import json
import re
import secrets
from functools import lru_cache
from pathlib import Path
from statistics import mean

from fastapi import Depends, FastAPI, HTTPException, Path as PathParam, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.exception_handlers import http_exception_handler
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import DATA_DIR, PROVIDERS, ROOT_DIR, WORKFLOW_VERSION, Settings, get_settings, local_health_url
from app.llm.client import endpoint_healthy, get_llm
from app import security
from app.limits import RequestRateLimiter, UsageGuard
from app.llm.prompts import PROMPT_VERSION
from app.models.domain import Claim, ClaimDecision, ExecutionRecord
from app.observability.analytics import Analytics, classify_agent, referrer_host
from app.observability.audit import AuditStore, log_event
from app.retrieval.policy_store import get_policy_store
from app.workflows.claim_graph import ClaimInvestigator

app = FastAPI(
    title="ClaimPilot — Healthcare Claims AI Investigator",
    description="AI-assisted investigation of synthetic claims exceptions. Synthetic data only. Not a claims adjudication system.",
    version="0.1.0",
    redoc_url=None,  # unused second docs UI; less surface (OWASP: minimize exposed endpoints)
)

STATIC_DIR = Path(__file__).parent / "static"
EVAL_RESULTS = ROOT_DIR / "evals" / "results" / "latest.json"
EVAL_REFERENCE_DIR = ROOT_DIR / "evals" / "reference"  # committed results from real-model runs
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# --------------------------------------------------------------------------- anonymous visitor scope
# The public demo has no login. Each browser gets a random, anonymous cookie; executions are tagged with a hash of
# it, so "your history" shows only your own analyses. Production would use authenticated identity + RBAC instead.
VISITOR_COOKIE = "cp_visitor"
VISITOR_MAX_AGE = 90 * 24 * 3600
_VALID_TOKEN = re.compile(r"[A-Za-z0-9_-]{24,64}")


@app.middleware("http")
async def anonymous_visitor(request: Request, call_next):
    token = request.cookies.get(VISITOR_COOKIE, "")
    is_new = not _VALID_TOKEN.fullmatch(token)
    if is_new:
        token = secrets.token_urlsafe(24)
    request.state.visitor = hashlib.sha256(token.encode()).hexdigest()[:32]  # only the hash is ever stored
    response = await call_next(request)
    if is_new:
        response.set_cookie(VISITOR_COOKIE, token, max_age=VISITOR_MAX_AGE, httponly=True, samesite="lax",
                            secure=request.url.scheme == "https", path="/")
    return response


def current_visitor(request: Request) -> str:
    return request.state.visitor


# Registered after the visitor middleware, so it is the outermost layer: headers land on every response,
# including rate-limit and size rejections.
request_limiter = RequestRateLimiter(get_settings().general_rate_limit_per_minute)
security.install(app, request_limiter)

# Browser navigation gets a branded page; API clients keep JSON errors.
ADMIN_DISABLED_MESSAGE = "The private usage dashboard is not enabled on this deployment."


@app.exception_handler(StarletteHTTPException)
async def friendly_http_errors(request: Request, exc: StarletteHTTPException):
    if security.wants_html(request):
        message = ADMIN_DISABLED_MESSAGE if request.url.path == "/admin" and exc.status_code == 404 else None
        return security.error_page(exc.status_code, message)
    return await http_exception_handler(request, exc)


@app.exception_handler(Exception)
async def unexpected_errors(request: Request, exc: Exception):
    log_event("unhandled_error", path=request.url.path, error_type=type(exc).__name__)  # details stay server-side
    if security.wants_html(request):
        return security.error_page(500)
    return JSONResponse({"detail": "Internal Server Error"}, status_code=500)


EXECUTION_ID = r"^EXE-[0-9a-f]{12,32}$"
CLAIM_ID = r"^[A-Za-z0-9._-]{1,40}$"


@lru_cache(maxsize=1)
def get_audit_store() -> AuditStore:
    return AuditStore(get_settings().audit_db_path)


@lru_cache(maxsize=1)
def get_analytics() -> Analytics:
    return Analytics(get_settings().audit_db_path)


NOTRACK_COOKIE = "cp_notrack"


def track(request: Request, analytics: Analytics, kind: str, detail: str | None = None, with_referrer: bool = False):
    """Record one anonymous usage event. Never raises: analytics must not affect the product."""
    try:
        if request.cookies.get(NOTRACK_COOKIE) == "1":
            return
        agent = classify_agent(request.headers.get("user-agent"))
        if agent:  # crawlers, link previews, scripts: kept apart so they never count as visitors
            analytics.record("automated", request.state.visitor, detail=agent)
            return
        ref = referrer_host(request.headers.get("referer"), request.url.hostname or "") if with_referrer else None
        analytics.record(kind, request.state.visitor, detail=detail, referrer=ref)
    except Exception as e:  # pragma: no cover - defensive
        log_event("analytics_failed", error_type=type(e).__name__)


@lru_cache(maxsize=1)
def get_usage_guard() -> UsageGuard:
    s = get_settings()
    return UsageGuard(per_minute=s.rate_limit_per_minute, daily_analyses=s.daily_max_analyses,
                      daily_budget_usd=s.daily_llm_budget_usd, counts_spend=s.resolved_provider != "mock")


@lru_cache(maxsize=1)
def get_investigator() -> ClaimInvestigator:
    settings = get_settings()
    return ClaimInvestigator(llm=get_llm(settings), policy_store=get_policy_store(), audit=get_audit_store(), settings=settings)


def available_providers() -> list[dict]:
    """Providers a caller may pick per analysis: those with a key configured on the server. Default first."""
    default = get_settings().resolved_provider
    found = []
    for name in PROVIDERS:
        s = Settings(llm_provider=name)
        if name == "local":  # self-hosted: offered only while its server answers the health check
            url = local_health_url(s)
            if not (s.llm_base_url and url and endpoint_healthy(url)):
                continue
        elif not s.llm_api_key:
            continue
        found.append({"id": name, "model": s.llm_model, "default": name == default,
                      "location": "on-prem" if name == "local" else "cloud"})
    if not found:
        found = [{"id": "mock", "model": "mock-analyst-v1", "default": True}]
    return sorted(found, key=lambda p: not p["default"])


@lru_cache(maxsize=None)
def _investigator_for(provider: str) -> ClaimInvestigator:
    settings = Settings(llm_provider=provider)
    return ClaimInvestigator(llm=get_llm(settings), policy_store=get_policy_store(), audit=get_audit_store(), settings=settings)


def get_investigator_selector():
    """Returns provider -> investigator for per-request model choice (overridable in tests)."""
    def select(provider: str) -> ClaimInvestigator:
        if provider not in {p["id"] for p in available_providers()}:
            raise HTTPException(422, f"Provider '{provider}' is not available on this server.")
        return _investigator_for(provider)
    return select


@app.get("/health")
def health(investigator: ClaimInvestigator = Depends(get_investigator), guard: UsageGuard = Depends(get_usage_guard)):
    return {
        "status": "ok",
        "llm_provider": investigator.llm.provider,
        "model": investigator.llm.model,
        "available_providers": available_providers(),
        "prompt_version": PROMPT_VERSION,
        "workflow_version": WORKFLOW_VERSION,
        "policies_loaded": len(get_policy_store().policies),
        "demo_limits": guard.status(),
        "admin_dashboard": admin_status(),
    }


def admin_status() -> str:
    """Whether /admin is enabled, and why not. Never reveals the token (the route itself is public knowledge)."""
    token = get_settings().admin_token
    if not token:
        return "disabled: ADMIN_TOKEN not set"
    if len(token) < 24:
        return f"disabled: ADMIN_TOKEN too short ({len(token)} chars, need 24+)"
    return "enabled"


@app.post("/claims/analyze", response_model=ClaimDecision)
def analyze_claim(claim: Claim, request: Request, investigator: ClaimInvestigator = Depends(get_investigator),
                  guard: UsageGuard = Depends(get_usage_guard), visitor: str = Depends(current_visitor),
                  select_investigator=Depends(get_investigator_selector),
                  analytics: Analytics = Depends(get_analytics),
                  provider: str | None = Query(None, pattern=r"^[a-z]{2,20}$",
                                               description="Model provider for this analysis (see /health available_providers); default when omitted")):
    if provider and provider != investigator.llm.provider:
        investigator = select_investigator(provider)  # same workflow, rules and guardrails; only the model changes
    guard.check(request.client.host if request.client else "unknown")  # 429 before any LLM spend
    model_label = f"{investigator.llm.provider} · {investigator.llm.model}"
    on_done = lambda: track(request, analytics, "analysis", detail=model_label)  # noqa: E731
    if "application/x-ndjson" in request.headers.get("accept", ""):
        # Same endpoint, same workflow; the client opted into per-node progress events (used by the demo UI).
        return StreamingResponse(_progress_events(investigator, claim, guard, visitor, on_done),
                                 media_type="application/x-ndjson")
    decision = investigator.investigate(claim, visitor=visitor)
    guard.record(decision.estimated_cost)
    on_done()
    return decision


def _progress_events(investigator: ClaimInvestigator, claim: Claim, guard: UsageGuard, visitor: str, on_done=lambda: None):
    """NDJSON stream: {"event": "node", ...} per completed workflow node, then {"event": "decision", ...}."""
    try:
        for kind, payload in investigator.investigate_stream(claim, visitor=visitor):
            if kind == "decision":
                guard.record(payload.estimated_cost)
                on_done()
                yield json.dumps({"event": "decision", "decision": payload.model_dump(mode="json")}) + "\n"
            else:
                yield json.dumps({"event": "node", **payload}) + "\n"
    except Exception as e:  # the HTTP status is already 200 once streaming starts; report failure in-band
        # Details stay server-side (OWASP A05: no internal error details to clients).
        log_event("analysis_stream_failed", error_type=type(e).__name__, error=str(e)[:500])
        yield json.dumps({"event": "error", "detail": "The analysis failed unexpectedly. Please try again."}) + "\n"


@app.get("/claims/{claim_id}/audit", response_model=list[ExecutionRecord])
def claim_audit(claim_id: str = PathParam(pattern=CLAIM_ID), audit: AuditStore = Depends(get_audit_store),
                visitor: str = Depends(current_visitor)):
    """Every execution of this claim made by you (this browser / cookie jar), oldest first."""
    records = audit.for_claim(claim_id, visitor=visitor)
    if not records:
        raise HTTPException(404, f"No executions of claim {claim_id} in your history")
    return records


@app.get("/executions", tags=["history"])
def execution_history(limit: int = Query(20, ge=1, le=100), claim_id: str | None = Query(None, pattern=CLAIM_ID),
                      audit: AuditStore = Depends(get_audit_store), visitor: str = Depends(current_visitor)):
    """Your recent executions, newest first: outcome, model and cost per run. Open one with /executions/{id}.
    Free-text claim fields are left out of the list; the full record is in the replay."""
    return [
        {"execution_id": r.execution_id, "claim_id": r.claim_id, "timestamp": r.timestamp,
         "recommendation": r.recommendation, "human_review_required": r.human_review_required,
         "risk_level": r.risk_level, "deterministic_outcome": r.deterministic_outcome, "confidence": r.confidence,
         "model": r.model, "llm_provider": r.llm_provider, "latency_ms": r.latency_ms,
         "estimated_cost": r.estimated_cost}
        for r in audit.recent(visitor, limit=limit, claim_id=claim_id)
    ]


@app.get("/executions/{execution_id}", response_model=ExecutionRecord)
def replay_execution(request: Request, execution_id: str = PathParam(pattern=EXECUTION_ID),
                     audit: AuditStore = Depends(get_audit_store), analytics: Analytics = Depends(get_analytics)):
    """Decision replay: evidence, policy versions, tool results, model/prompt/workflow versions and routing trail.
    Anyone with the (unguessable) execution ID can open it, so a single decision can be shared by link."""
    record = audit.get(execution_id)
    if record is None:
        raise HTTPException(404, f"Execution {execution_id} not found")
    track(request, analytics, "replay_view")
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
def demo_ui(request: Request, analytics: Analytics = Depends(get_analytics)):
    """Single-page demo workbench (plain HTML/CSS/JS in app/static). It only calls the public API."""
    shared = "execution" in request.query_params
    track(request, analytics, "shared_link_open" if shared else "page_view", with_referrer=True)
    return FileResponse(STATIC_DIR / "index.html")


# --------------------------------------------------------------------------- private usage dashboard


def require_admin(request: Request) -> None:
    """Bearer-token gate. Disabled (404) unless ADMIN_TOKEN is set and strong; constant-time comparison."""
    token = get_settings().admin_token
    if admin_status() != "enabled":
        raise HTTPException(404, "Not Found")
    auth = request.headers.get("authorization", "")
    supplied = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not hmac.compare_digest(supplied.encode(), token.encode()):
        log_event("admin_auth_failed")
        raise HTTPException(401, "Invalid admin token", headers={"WWW-Authenticate": "Bearer"})


@app.get("/admin", include_in_schema=False)
def admin_page():
    if admin_status() != "enabled":
        raise HTTPException(404, "Not Found")
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/admin/stats", include_in_schema=False, dependencies=[Depends(require_admin)])
def admin_stats(days: int = Query(30, ge=1, le=365), analytics: Analytics = Depends(get_analytics)):
    return analytics.summary(days)


@app.post("/admin/notrack", include_in_schema=False, dependencies=[Depends(require_admin)])
def admin_notrack(request: Request):
    """Stop counting this browser (e.g. the owner's own visits)."""
    response = JSONResponse({"tracking": "disabled for this browser"})
    response.set_cookie(NOTRACK_COOKIE, "1", max_age=365 * 24 * 3600, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", path="/")
    return response


@app.delete("/admin/notrack", include_in_schema=False, dependencies=[Depends(require_admin)])
def admin_track_again():
    response = JSONResponse({"tracking": "enabled for this browser"})
    response.delete_cookie(NOTRACK_COOKIE, path="/")
    return response


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
