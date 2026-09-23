"""Business behaviour of the investigation workflow (MockLLM stands in for the model)."""

from app.models.domain import Recommendation, RiskLevel, RuleOutcome


def evidence_ids(decision):
    return {(e.policy_id, e.version) for e in decision.policy_evidence}


def test_eligible_and_policy_compliant_claim_is_approved(make_investigator, demo_claim):
    d = make_investigator().investigate(demo_claim("01_obvious_approval"))

    assert d.recommendation == Recommendation.APPROVE
    assert d.human_review_required is False
    assert d.risk_level == RiskLevel.LOW
    assert ("POL-LAB-009", "1.0") in evidence_ids(d)


def test_missing_prior_authorization_is_denied_citing_the_versioned_policy(make_investigator, demo_claim):
    d = make_investigator().investigate(demo_claim("02_authorization_missing"))

    assert d.recommendation == Recommendation.DENY
    assert d.deterministic_outcome == RuleOutcome.FAIL
    assert ("POL-MRI-001", "2.0") in evidence_ids(d)
    assert any(t.tool == "check_emergency_exemption" and t.outcome == RuleOutcome.FAIL for t in d.tool_results)


def test_insufficient_information_requires_human_review_and_names_missing_fields(make_investigator, demo_claim):
    d = make_investigator().investigate(demo_claim("05_incomplete"))

    assert d.recommendation == Recommendation.HUMAN_REVIEW
    assert {"amount", "diagnosis_code"} <= set(d.missing_information)
    assert any("missing" in r for r in d.human_review_reasons)


def test_missing_identity_short_circuits_to_human_review_without_calling_llm(make_investigator, demo_claim):
    d = make_investigator().investigate(demo_claim("01_obvious_approval", member_id=None))

    assert d.recommendation == Recommendation.HUMAN_REVIEW
    assert d.estimated_tokens == 0
    assert "member_id" in d.human_review_reasons[0]


def test_conflicting_policies_trigger_human_review(make_investigator, demo_claim):
    d = make_investigator().investigate(demo_claim("06_conflicting_policy"))

    assert d.recommendation == Recommendation.HUMAN_REVIEW
    assert d.risk_level == RiskLevel.HIGH
    assert any("disagree" in r for r in d.human_review_reasons)
    assert {"POL-MRI-001", "POL-IMG-010"} <= {e.policy_id for e in d.policy_evidence}


def test_high_value_claim_is_never_auto_approved(make_investigator, demo_claim):
    d = make_investigator().investigate(demo_claim("03_high_value"))

    assert d.recommendation == Recommendation.HUMAN_REVIEW
    assert d.risk_level == RiskLevel.HIGH
    assert any("High-value" in r for r in d.human_review_reasons)


def test_ambiguous_emergency_case_escalates_then_resolves_when_information_is_supplied(make_investigator, demo_claim):
    investigator = make_investigator()

    first = investigator.investigate(demo_claim("04_ambiguous_emergency_unknown"))
    assert first.recommendation == Recommendation.HUMAN_REVIEW
    assert first.missing_information == ["emergency_indicator"]
    assert {"POL-MRI-001", "POL-EMRG-002"} <= {e.policy_id for e in first.policy_evidence}

    second = investigator.investigate(demo_claim("04_ambiguous_emergency_unknown", emergency_indicator=True))
    assert second.recommendation == Recommendation.APPROVE
    assert {("POL-MRI-001", "2.0"), ("POL-EMRG-002", "1.0")} <= evidence_ids(second)

    third = investigator.investigate(demo_claim("04_ambiguous_emergency_unknown", emergency_indicator=False))
    assert third.recommendation == Recommendation.DENY


def test_inactive_member_is_denied(make_investigator, demo_claim):
    d = make_investigator().investigate(demo_claim("01_obvious_approval", member_id="MBR-10024"))

    assert d.recommendation == Recommendation.DENY
    assert any(t.tool == "check_member_eligibility" and t.outcome == RuleOutcome.FAIL for t in d.tool_results)


def test_every_execution_is_audited_with_versions(make_investigator, demo_claim, audit):
    d = make_investigator().investigate(demo_claim("04_ambiguous_emergency_unknown"))
    record = audit.get(d.execution_id)

    assert record.recommendation == d.recommendation
    assert record.retrieved_policy_versions["POL-MRI-001"] == "2.0"
    assert record.prompt_version and record.workflow_version and record.model
    assert "check_prior_authorization" in record.tools_executed
    assert record.human_review_reason == d.human_review_reasons
    assert record.routing_trail[-1].startswith("escalate_to_human")
