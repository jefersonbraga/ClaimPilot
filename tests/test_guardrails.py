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
