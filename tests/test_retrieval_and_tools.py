from datetime import date

from app.models.domain import Claim, RuleOutcome
from app.retrieval.policy_store import build_query, get_policy_store
from app.tools import claim_tools


def mri_claim(**kw):
    base = dict(claim_id="T-1", member_id="MBR-10023", provider="X", procedure="MRI_KNEE", amount=1840,
                diagnosis_code="M25.561", plan="GOLD_PPO", prior_authorization=False, date_of_service=date(2026, 9, 10))
    return Claim(**{**base, **kw})


def test_policy_version_is_selected_by_date_of_service():
    store = get_policy_store()
    for dos, version in ((date(2025, 10, 1), "1.0"), (date(2026, 9, 10), "2.0")):
        claim = mri_claim(date_of_service=dos)
        hits = store.retrieve(build_query(claim), claim)
        assert ("POL-MRI-001", version) in {(p.policy_id, p.version) for p in hits}


def test_threshold_comes_from_the_policy_version_in_force():
    store = get_policy_store()
    old = mri_claim(date_of_service=date(2025, 10, 1))  # v1: threshold $2,000 -> $1,840 needs no PA
    new = mri_claim()                                   # v2: threshold $1,500 -> PA required

    assert claim_tools.check_prior_authorization(old, store.applicable_policies(old)).outcome == RuleOutcome.PASS
    result = claim_tools.check_prior_authorization(new, store.applicable_policies(new))
    assert result.outcome == RuleOutcome.FAIL and result.policy_refs == ["POL-MRI-001@2.0"]


def test_retrieval_filters_out_policies_for_other_plans():
    claim = mri_claim()
    ids = {p.policy_id for p in get_policy_store().retrieve(build_query(claim), claim, top_k=10)}
    assert "POL-PT-007" not in ids and "POL-NET-008" not in ids


def test_unknown_emergency_status_is_not_treated_as_no():
    store = get_policy_store()
    claim = mri_claim(emergency_indicator=None)
    r = claim_tools.check_emergency_exemption(claim, store.applicable_policies(claim), waives="prior_authorization")
    assert r.outcome == RuleOutcome.INDETERMINATE and r.data["missing"] == ["emergency_indicator"]


def test_valid_prior_authorization_on_file_passes():
    store = get_policy_store()
    claim = mri_claim(member_id="MBR-10045", prior_authorization=True, date_of_service=date(2026, 7, 1))
    assert claim_tools.check_prior_authorization(claim, store.applicable_policies(claim)).data["auth_on_file"] == "PA-55001"


def test_visit_limit_exhausted_fails():
    store = get_policy_store()
    claim = Claim(claim_id="T-2", member_id="MBR-20002", procedure="PT_VISIT", amount=140, plan="SILVER_HMO",
                  date_of_service=date(2026, 8, 1))
    assert claim_tools.get_claim_history(claim, store.applicable_policies(claim)).outcome == RuleOutcome.FAIL
