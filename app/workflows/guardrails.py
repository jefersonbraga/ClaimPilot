"""Guardrails: evidence grounding, rule/LLM conflict detection, risk scoring and escalation rules.

Design principle: the LLM can make a decision *more* conservative (ask for review), never *less*.
Every trigger produces a human-readable reason so the analyst knows exactly why a case landed on their desk.
"""

import re
from dataclasses import dataclass, field

from app.models.domain import (
    LLMAnalysis,
    PolicyEvidence,
    Recommendation,
    RetrievedPolicy,
    RiskLevel,
    RuleOutcome,
    ToolResult,
)

EXPECTED_FROM_RULES = {
    RuleOutcome.PASS: Recommendation.APPROVE,
    RuleOutcome.FAIL: Recommendation.DENY,
    RuleOutcome.INDETERMINATE: Recommendation.HUMAN_REVIEW,
}

# A citation must quote enough text to actually support something; "prior authorization" alone is not evidence.
MIN_EXCERPT_WORDS = 5

# Deterministic risk signals: (tool, data flag) -> description. A signal never fails a rule; it forces review.
RISK_SIGNALS = {
    ("calculate_financial_threshold", "high_value"): lambda v: "High-value claim (above financial review threshold).",
    ("get_claim_history", "possible_duplicate_of"): lambda v: f"Possible duplicate of {', '.join(v)}.",
    ("check_prior_authorization", "policy_conflict"): lambda v: "Conflicting policy evidence on prior authorization requirement.",
}


def _norm(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text.strip(" \"'“”‘’").removesuffix("...").removesuffix("…").rstrip(" .")


def risk_factors(tool_results: list[ToolResult]) -> list[str]:
    return [describe(t.data[flag]) for t in tool_results for (tool, flag), describe in RISK_SIGNALS.items()
            if t.tool == tool and t.data.get(flag)]


def findings(tool_results: list[ToolResult]) -> list[ToolResult]:
    """Tool results that surfaced something an analyst would need to read policy about:
    a non-PASS outcome or a deterministic risk signal."""
    return [t for t in tool_results
            if t.outcome != RuleOutcome.PASS or any(t.tool == tool and t.data.get(flag) for tool, flag in RISK_SIGNALS)]


_CITED_ID = re.compile(r"^\s*(?P<id>[A-Z]+-[A-Z0-9]+-\d+)\s*(?:(?:@|\s)v?(?P<version>\d+(?:\.\d+)*))?\s*$")


def _parse_cited_id(raw: str) -> tuple[str, str | None]:
    """Models write ids as they appear in the prompt ("POL-MRI-001 v2.0") or bare ("POL-MRI-001")."""
    m = _CITED_ID.match(raw)
    return (m.group("id"), m.group("version")) if m else (raw.strip(), None)


def ground_citations(analysis: LLMAnalysis, retrieved: list[RetrievedPolicy]) -> tuple[list[PolicyEvidence], list[str]]:
    """A citation is grounded only if it names a retrieved policy (and, when a version is cited, that exact version)
    AND its excerpt appears verbatim in that policy."""
    by_id = {p.policy_id: p for p in retrieved}
    grounded, problems = [], []
    for c in analysis.citations:
        cited_id, cited_version = _parse_cited_id(c.policy_id)
        policy = by_id.get(cited_id)
        if policy is None:
            problems.append(f"cited {c.policy_id}, which was not in the retrieved evidence")
        elif cited_version is not None and cited_version != policy.version:
            problems.append(f"cited {cited_id} v{cited_version}, but the version in force is v{policy.version}")
        elif len(_norm(c.excerpt).split()) < MIN_EXCERPT_WORDS:
            problems.append(f"excerpt attributed to {c.policy_id} is too short to verify (< {MIN_EXCERPT_WORDS} words)")
        elif _norm(c.excerpt) not in _norm(policy.content):
            problems.append(f"excerpt attributed to {c.policy_id} does not appear in the policy text")
        else:
            grounded.append(PolicyEvidence(policy_id=policy.policy_id, version=policy.version, excerpt=c.excerpt.strip()))
    return grounded, problems


def aggregate_rule_outcome(effective: list[RuleOutcome]) -> RuleOutcome:
    if RuleOutcome.FAIL in effective:
        return RuleOutcome.FAIL
    if RuleOutcome.INDETERMINATE in effective:
        return RuleOutcome.INDETERMINATE
    return RuleOutcome.PASS


@dataclass
class RiskAssessment:
    risk_level: RiskLevel
    risk_factors: list[str] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)
    evidence: list[PolicyEvidence] = field(default_factory=list)
    grounded: bool = False
    grounding_problems: list[str] = field(default_factory=list)


def evaluate(
    *,
    rule_outcome: RuleOutcome,
    tool_results: list[ToolResult],
    missing_fields: list[str],
    analysis: LLMAnalysis | None,
    llm_error: str | None,
    retrieved: list[RetrievedPolicy],
    confidence_threshold: float,
) -> RiskAssessment:
    reasons: list[str] = []
    factors = risk_factors(tool_results)

    # --- missing information (claim fields + anything tools or the LLM flagged)
    missing = list(dict.fromkeys(
        missing_fields
        + [m for t in tool_results for m in t.data.get("missing", [])]
        + (analysis.missing_information if analysis else [])
    ))
    if missing:
        reasons.append(f"Required information is missing: {', '.join(missing)}.")

    # --- deterministic rules
    if rule_outcome == RuleOutcome.INDETERMINATE:
        reasons.append("Deterministic rule verdict is INDETERMINATE; the rules alone cannot decide this claim.")
    for t in tool_results:
        if t.outcome == RuleOutcome.INDETERMINATE and not t.data.get("missing"):
            reasons.append(f"{t.tool} could not reach a deterministic result: {t.detail}")
    for f in factors:
        reasons.append(f"HIGH risk: {f}")

    # --- LLM output guardrails
    evidence: list[PolicyEvidence] = []
    problems: list[str] = []
    grounded = False
    if llm_error or analysis is None:
        reasons.append(f"LLM analysis unavailable or invalid; falling back to human review ({llm_error or 'no output'}).")
    else:
        evidence, problems = ground_citations(analysis, retrieved)
        grounded = bool(evidence) and not problems
        if problems:
            reasons.append("Ungrounded citation detected (possible hallucination): " + "; ".join(problems) + ".")
        if not evidence:
            reasons.append("Recommendation is not supported by any verifiable policy citation.")
        if analysis.confidence < confidence_threshold:
            reasons.append(f"Model confidence {analysis.confidence:.2f} is below the {confidence_threshold:.2f} threshold.")

        expected = EXPECTED_FROM_RULES[rule_outcome]
        if analysis.recommendation == Recommendation.HUMAN_REVIEW and expected != Recommendation.HUMAN_REVIEW:
            reasons.append("The model requested human review (a more conservative outcome is always honored).")
        elif analysis.recommendation != Recommendation.HUMAN_REVIEW and analysis.recommendation != expected:
            reasons.append(
                f"Model recommended {analysis.recommendation.value} but deterministic rules indicate "
                f"{rule_outcome.value}; the model cannot override business rules."
            )

    if factors:
        level = RiskLevel.HIGH
    elif reasons or rule_outcome != RuleOutcome.PASS:
        level = RiskLevel.MEDIUM
    else:
        level = RiskLevel.LOW

    # Safety invariant (defence in depth): these states must never be decided autonomously, even if a future
    # change to the checks above forgets to add a reason.
    if not reasons and (level == RiskLevel.HIGH or rule_outcome == RuleOutcome.INDETERMINATE or not grounded):
        reasons.append("Safety invariant: autonomous decision blocked (high risk, indeterminate rules or ungrounded output).")

    return RiskAssessment(risk_level=level, risk_factors=factors, review_reasons=list(dict.fromkeys(reasons)),
                          evidence=evidence, grounded=grounded, grounding_problems=problems)
