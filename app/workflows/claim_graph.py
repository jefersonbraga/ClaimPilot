"""The claim investigation workflow as a small LangGraph state machine.

validate_claim ─┬─> retrieve_policy -> check_eligibility -> check_authorization -> refine_retrieval
                └─(identity fields missing)──────────────────────────────────────────────> escalate_to_human
refine_retrieval -> analyze_claim -> evaluate_risk -> human_review_router ─┬─> generate_recommendation ─┐
                                                                          └─> escalate_to_human ───────┴─> record_audit

Deterministic nodes decide facts. The LLM node interprets. The router decides who gets the final say.
Retrieval runs twice when needed: first from the claim itself, then (refine_retrieval) from what the deterministic
tools discovered — e.g. a duplicate found in claims history pulls in the duplicate-claims policy.
"""

import operator
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from app.config import WORKFLOW_VERSION, Settings
from app.llm.client import LLMClient, analyze
from app.llm.prompts import PROMPT_VERSION, build_user_prompt
from app.models.domain import (
    REQUIRED_CLAIM_FIELDS,
    Claim,
    ClaimDecision,
    ExecutionRecord,
    LLMAnalysis,
    LLMUsage,
    Recommendation,
    RetrievedPolicy,
    RiskLevel,
    RuleOutcome,
    ToolResult,
)
from app.observability.audit import AuditStore, log_event
from app.retrieval.policy_store import PolicyStore, build_query
from app.tools import claim_tools
from app.workflows import guardrails

IDENTITY_FIELDS = ("member_id", "procedure")


class ClaimState(TypedDict, total=False):
    execution_id: str
    started_at: float
    claim: Claim
    missing_fields: list[str]
    region: str | None
    applicable_policy_refs: list[str]
    retrieved: list[RetrievedPolicy]
    retrieval_queries: Annotated[list[str], operator.add]
    supplemental_policy_ids: list[str]
    tool_results: Annotated[list[ToolResult], operator.add]
    effective_outcomes: Annotated[list[RuleOutcome], operator.add]
    rule_outcome: RuleOutcome
    analysis: LLMAnalysis | None
    llm_usage: LLMUsage | None
    llm_error: str | None
    risk: guardrails.RiskAssessment
    decision: ClaimDecision
    routing_trail: Annotated[list[str], operator.add]


def build_graph(*, llm: LLMClient, policy_store: PolicyStore, audit: AuditStore, settings: Settings):
    # ------------------------------------------------------------------ nodes

    def validate_claim(state: ClaimState) -> dict:
        claim = state["claim"]
        missing = [f for f in REQUIRED_CLAIM_FIELDS if getattr(claim, f) is None]
        return {"missing_fields": missing, "routing_trail": [f"validate_claim: missing={missing or 'none'}"]}

    def after_validation(state: ClaimState) -> str:
        if any(f in state["missing_fields"] for f in IDENTITY_FIELDS):
            return "escalate_to_human"
        return "retrieve_policy"

    def retrieve_policy(state: ClaimState) -> dict:
        claim = state["claim"]
        member = claim_tools.find_member(claim.member_id)
        region = claim.region or (member or {}).get("region")
        applicable = policy_store.applicable_policies(claim, region)
        query = build_query(claim)
        retrieved = policy_store.retrieve(query, claim, top_k=settings.retrieval_top_k, region=region)
        return {
            "region": region,
            "applicable_policy_refs": [p.ref for p in applicable],
            "retrieved": retrieved,
            "retrieval_queries": [query],
            "routing_trail": [f"retrieve_policy: {[f'{p.policy_id}@{p.version}' for p in retrieved]}"],
        }

    def _applicable(state: ClaimState):
        return policy_store.applicable_policies(state["claim"], state.get("region"))

    def check_eligibility(state: ClaimState) -> dict:
        claim, policies = state["claim"], _applicable(state)
        elig = claim_tools.check_member_eligibility(claim, policies)
        history = claim_tools.get_claim_history(claim, policies)
        return {"tool_results": [elig, history], "effective_outcomes": [elig.outcome, history.outcome],
                "routing_trail": [f"check_eligibility: {elig.outcome.value}, history: {history.outcome.value}"]}

    def check_authorization(state: ClaimState) -> dict:
        claim, policies = state["claim"], _applicable(state)
        results, effective = [], []

        for check, waives in ((claim_tools.check_prior_authorization, "prior_authorization"),
                              (claim_tools.check_network, "network_restriction")):
            result = check(claim, policies)
            results.append(result)
            if result.outcome == RuleOutcome.FAIL:
                # A failed requirement may be rescued by an exemption; the exemption's outcome becomes authoritative.
                exemption = claim_tools.check_emergency_exemption(claim, policies, waives=waives)
                results.append(exemption)
                effective.append(exemption.outcome)
            else:
                effective.append(result.outcome)

        financial = claim_tools.calculate_financial_threshold(claim, policies)
        results.append(financial)
        effective.append(financial.outcome)

        rule_outcome = guardrails.aggregate_rule_outcome(state.get("effective_outcomes", []) + effective)
        return {"tool_results": results, "effective_outcomes": effective, "rule_outcome": rule_outcome,
                "routing_trail": [f"check_authorization: {[f'{r.tool}={r.outcome.value}' for r in results]} -> rules={rule_outcome.value}"]}

    def refine_retrieval(state: ClaimState) -> dict:
        """Second, conditional retrieval pass driven by deterministic findings (failed/indeterminate checks and
        risk signals). The first pass only knows the claim; this one knows what the tools discovered."""
        found = guardrails.findings(state["tool_results"])
        if not found:
            return {"supplemental_policy_ids": [], "routing_trail": ["refine_retrieval: skipped (no deterministic findings)"]}
        query = " ".join(t.detail for t in found)
        have = {(p.policy_id, p.version) for p in state["retrieved"]}
        extra = [p for p in policy_store.retrieve(query, state["claim"], top_k=settings.supplemental_top_k,
                                                  region=state.get("region"), min_score=settings.supplemental_min_score)
                 if (p.policy_id, p.version) not in have]
        return {"retrieved": state["retrieved"] + extra, "retrieval_queries": [query],
                "supplemental_policy_ids": [p.policy_id for p in extra],
                "routing_trail": [f"refine_retrieval: findings={[t.tool for t in found]} added={[f'{p.policy_id}@{p.version}' for p in extra]}"]}

    def analyze_claim(state: ClaimState) -> dict:
        prompt = build_user_prompt(state["claim"], state["retrieved"], state["tool_results"], state["rule_outcome"])
        analysis, usage, error = analyze(llm, prompt, settings)
        trail = f"analyze_claim: {analysis.recommendation.value} @ {analysis.confidence:.2f}" if analysis else f"analyze_claim: ERROR {error}"
        return {"analysis": analysis, "llm_usage": usage, "llm_error": error, "routing_trail": [trail]}

    def evaluate_risk(state: ClaimState) -> dict:
        risk = guardrails.evaluate(
            rule_outcome=state["rule_outcome"],
            tool_results=state["tool_results"],
            missing_fields=state["missing_fields"],
            analysis=state.get("analysis"),
            llm_error=state.get("llm_error"),
            retrieved=state["retrieved"],
            confidence_threshold=settings.confidence_threshold,
        )
        return {"risk": risk, "routing_trail": [f"evaluate_risk: {risk.risk_level.value}, triggers={len(risk.review_reasons)}"]}

    def human_review_router(state: ClaimState) -> str:
        return "escalate_to_human" if state["risk"].review_reasons else "generate_recommendation"

    def generate_recommendation(state: ClaimState) -> dict:
        rec = guardrails.EXPECTED_FROM_RULES[state["rule_outcome"]]  # rules and model agree; rules are the source
        return {"decision": _decision(state, rec, []), "routing_trail": [f"generate_recommendation: {rec.value}"]}

    def escalate_to_human(state: ClaimState) -> dict:
        if "risk" in state:
            reasons = state["risk"].review_reasons
        else:  # short-circuit from validation
            reasons = [f"Required identifying information is missing: {', '.join(state['missing_fields'])}; "
                       "the claim cannot be investigated automatically."]
        return {"decision": _decision(state, Recommendation.HUMAN_REVIEW, reasons),
                "routing_trail": ["escalate_to_human: HUMAN_REVIEW_REQUIRED"]}

    def record_audit(state: ClaimState) -> dict:
        record = _execution_record(state, llm)
        audit.save(record)
        log_event("claim_investigated", execution_id=record.execution_id, claim_id=record.claim_id,
                  recommendation=record.recommendation.value, human_review=record.human_review_required,
                  risk=record.risk_level.value, rule_outcome=record.deterministic_outcome.value,
                  retrieved=record.retrieved_policy_ids, tools=record.tools_executed,
                  latency_ms=record.latency_ms, llm_latency_ms=record.llm_latency_ms,
                  tokens=record.input_tokens + record.output_tokens, cost=record.estimated_cost)
        return {}

    # ------------------------------------------------------------------ wiring

    g = StateGraph(ClaimState)
    for name, fn in [("validate_claim", validate_claim), ("retrieve_policy", retrieve_policy),
                     ("check_eligibility", check_eligibility), ("check_authorization", check_authorization),
                     ("refine_retrieval", refine_retrieval), ("analyze_claim", analyze_claim), ("evaluate_risk", evaluate_risk),
                     ("generate_recommendation", generate_recommendation), ("escalate_to_human", escalate_to_human),
                     ("record_audit", record_audit)]:
        g.add_node(name, fn)

    g.add_edge(START, "validate_claim")
    g.add_conditional_edges("validate_claim", after_validation, ["retrieve_policy", "escalate_to_human"])
    g.add_edge("retrieve_policy", "check_eligibility")
    g.add_edge("check_eligibility", "check_authorization")
    g.add_edge("check_authorization", "refine_retrieval")
    g.add_edge("refine_retrieval", "analyze_claim")
    g.add_edge("analyze_claim", "evaluate_risk")
    g.add_conditional_edges("evaluate_risk", human_review_router, ["generate_recommendation", "escalate_to_human"])
    g.add_edge("generate_recommendation", "record_audit")
    g.add_edge("escalate_to_human", "record_audit")
    g.add_edge("record_audit", END)
    return g.compile()


# ---------------------------------------------------------------------- output assembly


def _decision(state: ClaimState, rec: Recommendation, reasons: list[str]) -> ClaimDecision:
    risk = state.get("risk")
    analysis = state.get("analysis")
    usage = state.get("llm_usage")
    tool_results = state.get("tool_results", [])
    missing = list(dict.fromkeys(state.get("missing_fields", []) + [m for t in tool_results for m in t.data.get("missing", [])]
                                 + (analysis.missing_information if analysis else [])))

    # Only show the model's summary if its evidence was verified; otherwise fall back to deterministic facts.
    if analysis and risk and risk.evidence:
        summary = analysis.reasoning_summary
    elif tool_results:
        summary = "Deterministic findings: " + " ".join(f"{t.tool}: {t.detail}" for t in tool_results)
    else:
        summary = "Claim could not be investigated automatically."

    return ClaimDecision(
        execution_id=state["execution_id"],
        claim_id=state["claim"].claim_id,
        recommendation=rec,
        confidence=analysis.confidence if analysis else 0.0,
        risk_level=risk.risk_level if risk else RiskLevel.MEDIUM,
        reasoning_summary=summary,
        policy_evidence=risk.evidence if risk else [],
        tool_results=tool_results,
        deterministic_outcome=state.get("rule_outcome", RuleOutcome.INDETERMINATE),
        human_review_required=rec == Recommendation.HUMAN_REVIEW,
        human_review_reasons=reasons,
        missing_information=missing,
        estimated_tokens=(usage.input_tokens + usage.output_tokens) if usage else 0,
        estimated_cost=usage.estimated_cost if usage else 0.0,
        latency_ms=round((time.perf_counter() - state["started_at"]) * 1000, 2),
    )


def _execution_record(state: ClaimState, llm: LLMClient) -> ExecutionRecord:
    d: ClaimDecision = state["decision"]
    usage = state.get("llm_usage")
    risk = state.get("risk")
    analysis = state.get("analysis")
    retrieved = state.get("retrieved", [])
    return ExecutionRecord(
        execution_id=d.execution_id,
        claim_id=d.claim_id,
        timestamp=datetime.now(timezone.utc),
        model=llm.model,
        llm_provider=llm.provider,
        prompt_version=PROMPT_VERSION,
        workflow_version=WORKFLOW_VERSION,
        claim=state["claim"],
        retrieved_policy_ids=[p.policy_id for p in retrieved],
        retrieved_policy_versions={p.policy_id: p.version for p in retrieved},
        supplemental_policy_ids=state.get("supplemental_policy_ids", []),
        retrieval_queries=state.get("retrieval_queries", []),
        policy_evidence=d.policy_evidence,
        tools_executed=[t.tool for t in d.tool_results],
        tool_summary=[f"{t.tool}: {t.outcome.value} — {t.detail}" for t in d.tool_results],
        tool_results=d.tool_results,
        deterministic_outcome=d.deterministic_outcome,
        llm_recommendation=analysis.recommendation if analysis else None,
        llm_error=state.get("llm_error"),
        evidence_grounded=risk.grounded if risk else False,
        grounding_failures=risk.grounding_problems if risk else [],
        confidence=d.confidence,
        risk_level=d.risk_level,
        risk_factors=risk.risk_factors if risk else [],
        recommendation=d.recommendation,
        reasoning_summary=d.reasoning_summary,
        human_review_required=d.human_review_required,
        human_review_reason=d.human_review_reasons,
        missing_information=d.missing_information,
        routing_trail=state.get("routing_trail", []),
        latency_ms=d.latency_ms,
        llm_latency_ms=usage.latency_ms if usage else 0.0,
        input_tokens=usage.input_tokens if usage else 0,
        output_tokens=usage.output_tokens if usage else 0,
        estimated_cost=d.estimated_cost,
    )


class ClaimInvestigator:
    """Thin facade used by the API and the eval harness."""

    def __init__(self, *, llm: LLMClient, policy_store: PolicyStore, audit: AuditStore, settings: Settings):
        self.llm = llm
        self.graph = build_graph(llm=llm, policy_store=policy_store, audit=audit, settings=settings)

    @staticmethod
    def _initial_state(claim: Claim) -> dict:
        return {"execution_id": f"EXE-{uuid.uuid4().hex[:12]}", "started_at": time.perf_counter(), "claim": claim,
                "tool_results": [], "effective_outcomes": [], "retrieval_queries": [], "routing_trail": []}

    def investigate(self, claim: Claim) -> ClaimDecision:
        return self.graph.invoke(self._initial_state(claim))["decision"]

    def investigate_stream(self, claim: Claim):
        """Same run as `investigate`, but yields ("node", {node, trail}) as each LangGraph node completes and
        finally ("decision", ClaimDecision). Lets a UI show real progress instead of a simulated animation."""
        decision = None
        for update in self.graph.stream(self._initial_state(claim), stream_mode="updates"):
            for node, delta in update.items():
                delta = delta or {}
                trail = delta.get("routing_trail") or []
                yield "node", {"node": node, "trail": trail[-1] if trail else None}
                decision = delta.get("decision", decision)
        yield "decision", decision
