"""The model misbehaves; the system must not."""

from app.models.domain import Recommendation
from tests.conftest import ScriptedLLM

MRI_V2_EXCERPT = "MRI knee procedures under GOLD_PPO require prior authorization when the billed amount exceeds $1,500."


def llm_says(recommendation, confidence=0.95, citations=None, missing=None):
    return ScriptedLLM({
        "recommendation": recommendation, "confidence": confidence, "reasoning_summary": "Scripted.",
        "citations": citations if citations is not None else [{"policy_id": "POL-MRI-001", "excerpt": MRI_V2_EXCERPT}],
        "missing_information": missing or [],
    })


def test_low_confidence_triggers_human_review(make_investigator, demo_claim):
    d = make_investigator(llm_says("DENY", confidence=0.6)).investigate(demo_claim("02_authorization_missing"))

    assert d.recommendation == Recommendation.HUMAN_REVIEW
    assert any("confidence 0.60 is below" in r for r in d.human_review_reasons)


def test_llm_cannot_override_a_failed_business_rule(make_investigator, demo_claim):
    d = make_investigator(llm_says("APPROVE")).investigate(demo_claim("02_authorization_missing"))

    assert d.recommendation == Recommendation.HUMAN_REVIEW
    assert any("cannot override" in r for r in d.human_review_reasons)


def test_hallucinated_citation_is_detected(make_investigator, demo_claim):
    fake = [{"policy_id": "POL-MRI-001", "excerpt": "Knee MRI never requires prior authorization."}]
    d = make_investigator(llm_says("DENY", citations=fake)).investigate(demo_claim("02_authorization_missing"))

    assert d.recommendation == Recommendation.HUMAN_REVIEW
    assert any("Ungrounded citation" in r for r in d.human_review_reasons)
    assert d.policy_evidence == []  # unverified evidence is never shown as evidence


def test_citation_of_unretrieved_policy_is_detected(make_investigator, demo_claim):
    ghost = [{"policy_id": "POL-XYZ-999", "excerpt": "Anything."}]
    d = make_investigator(llm_says("DENY", citations=ghost)).investigate(demo_claim("02_authorization_missing"))

    assert any("not in the retrieved evidence" in r for r in d.human_review_reasons)


def test_invalid_model_output_falls_back_to_human_review(make_investigator, demo_claim):
    d = make_investigator(ScriptedLLM("Sure! I think this claim is fine.")).investigate(demo_claim("01_obvious_approval"))

    assert d.recommendation == Recommendation.HUMAN_REVIEW
    assert any("LLM analysis unavailable or invalid" in r for r in d.human_review_reasons)
    assert d.reasoning_summary.startswith("Deterministic findings")


def test_model_may_be_more_conservative_than_rules(make_investigator, demo_claim):
    d = make_investigator(llm_says("HUMAN_REVIEW", citations=[
        {"policy_id": "POL-LAB-009", "excerpt": "Routine laboratory services such as complete blood count and lipid panel do not require prior authorization."}
    ])).investigate(demo_claim("01_obvious_approval"))

    assert d.recommendation == Recommendation.HUMAN_REVIEW
    assert any("more conservative" in r for r in d.human_review_reasons)


def test_too_short_excerpt_is_not_accepted_as_evidence(make_investigator, demo_claim):
    d = make_investigator(llm_says("DENY", citations=[{"policy_id": "POL-MRI-001", "excerpt": "prior authorization"}])
                          ).investigate(demo_claim("02_authorization_missing"))

    assert d.recommendation == Recommendation.HUMAN_REVIEW
    assert any("too short to verify" in r for r in d.human_review_reasons)


def test_schema_invalid_output_falls_back_to_human_review(make_investigator, demo_claim):
    d = make_investigator(llm_says("APPROVE", confidence=1.7)).investigate(demo_claim("01_obvious_approval"))

    assert d.recommendation == Recommendation.HUMAN_REVIEW
    assert any("schema validation" in r for r in d.human_review_reasons)


def test_llm_outage_falls_back_to_human_review(make_investigator, demo_claim):
    class DownLLM:
        provider, model = "down", "down"

        def complete(self, system, user):
            raise TimeoutError("gateway timeout")

    d = make_investigator(DownLLM()).investigate(demo_claim("01_obvious_approval"))

    assert d.recommendation == Recommendation.HUMAN_REVIEW
    assert any("TimeoutError" in r for r in d.human_review_reasons)


def test_llm_cannot_decide_when_rules_are_indeterminate(make_investigator, demo_claim):
    d = make_investigator(llm_says("APPROVE")).investigate(demo_claim("04_ambiguous_emergency_unknown"))

    assert d.recommendation == Recommendation.HUMAN_REVIEW
    assert any("INDETERMINATE" in r for r in d.human_review_reasons)


# ---------------------------------------------------------------- exhaustive routing property


def test_safety_matrix_no_combination_lets_the_model_override_rules():
    """Every combination of rule verdict x model recommendation x confidence x grounding x risk.
    An autonomous decision is only possible when every control agrees; otherwise there is a stated reason."""
    import itertools

    from app.models.domain import Citation, LLMAnalysis, RetrievedPolicy, RuleOutcome, ToolResult
    from app.workflows import guardrails

    policy = RetrievedPolicy(policy_id="POL-MRI-001", version="2.0", title="t", score=1.0, content=MRI_V2_EXCERPT)
    good = [Citation(policy_id="POL-MRI-001", excerpt=MRI_V2_EXCERPT)]
    bad = [Citation(policy_id="POL-MRI-001", excerpt="MRI never requires any prior authorization at all.")]

    for rules, rec, conf, cites, high_value, missing in itertools.product(
        RuleOutcome, Recommendation, (0.5, 0.95), (good, bad, []), (False, True), ([], ["diagnosis_code"])
    ):
        tools = [ToolResult(tool="calculate_financial_threshold", outcome=RuleOutcome.PASS, detail="",
                            data={"high_value": high_value})]
        analysis = LLMAnalysis(recommendation=rec, confidence=conf, reasoning_summary="x", citations=cites)
        risk = guardrails.evaluate(rule_outcome=rules, tool_results=tools, missing_fields=missing, analysis=analysis,
                                   llm_error=None, retrieved=[policy], confidence_threshold=0.8)

        autonomous = not risk.review_reasons
        if autonomous:
            assert rules != RuleOutcome.INDETERMINATE
            assert rec == guardrails.EXPECTED_FROM_RULES[rules]
            assert conf >= 0.8 and cites is good and not high_value and not missing
