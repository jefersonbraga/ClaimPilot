"""Deterministic enterprise tools.

These stand in for real systems of record (enrollment, UM/authorization, claims history, payment integrity).
They are plain functions over synthetic JSON. The LLM never computes these results; it only reads them.
Every threshold comes from a versioned policy's machine-readable `rule` block, so each result can cite
the exact policy version it applied.
"""

import json
from datetime import date, timedelta
from functools import lru_cache

from app.config import DATA_DIR
from app.models.domain import Claim, Policy, RuleOutcome, ToolResult


@lru_cache(maxsize=None)
def _load(name: str) -> list[dict]:
    return json.loads((DATA_DIR / name).read_text())


def _rules(policies: list[Policy], rule_type: str) -> list[Policy]:
    return [p for p in policies if p.rule and p.rule.get("type") == rule_type]


def _d(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def find_member(member_id: str | None) -> dict | None:
    return next((m for m in _load("members.json") if m["member_id"] == member_id), None)


# --------------------------------------------------------------------------- tools


def check_member_eligibility(claim: Claim, policies: list[Policy]) -> ToolResult:
    refs = [p.ref for p in _rules(policies, "active_coverage")]
    if not claim.member_id or not claim.date_of_service:
        return ToolResult(tool="check_member_eligibility", outcome=RuleOutcome.INDETERMINATE,
                          detail="Cannot verify eligibility without member_id and date_of_service.",
                          data={"missing": [f for f in ("member_id", "date_of_service") if not getattr(claim, f)]},
                          policy_refs=refs)
    member = find_member(claim.member_id)
    if member is None:
        return ToolResult(tool="check_member_eligibility", outcome=RuleOutcome.INDETERMINATE,
                          detail=f"Member {claim.member_id} not found in enrollment records.",
                          data={"missing": ["member_enrollment_record"]}, policy_refs=refs)

    start, end = _d(member["coverage_start"]), _d(member["coverage_end"])
    active = start <= claim.date_of_service and (end is None or claim.date_of_service <= end)
    data = {"member_plan": member["plan"], "coverage_start": member["coverage_start"],
            "coverage_end": member["coverage_end"], "active_on_dos": active, "region": member["region"]}
    if not active:
        return ToolResult(tool="check_member_eligibility", outcome=RuleOutcome.FAIL,
                          detail=f"Member coverage not active on {claim.date_of_service} (coverage end {member['coverage_end']}).",
                          data=data, policy_refs=refs)
    if claim.plan and claim.plan != member["plan"]:
        return ToolResult(tool="check_member_eligibility", outcome=RuleOutcome.INDETERMINATE,
                          detail=f"Claim plan {claim.plan} does not match enrollment plan {member['plan']}.",
                          data={**data, "missing": ["plan_reconciliation"]}, policy_refs=refs)
    return ToolResult(tool="check_member_eligibility", outcome=RuleOutcome.PASS,
                      detail=f"Member active on {claim.date_of_service} under {member['plan']}.", data=data, policy_refs=refs)


def check_prior_authorization(claim: Claim, policies: list[Policy]) -> ToolResult:
    """Is PA required (per every applicable threshold rule)? Is there a valid PA on file?"""
    tool = "check_prior_authorization"
    if _rules(policies, "no_prior_auth_required"):
        refs = [p.ref for p in _rules(policies, "no_prior_auth_required")]
        return ToolResult(tool=tool, outcome=RuleOutcome.PASS, detail="Procedure does not require prior authorization.",
                          data={"pa_required": False}, policy_refs=refs)

    threshold_rules = _rules(policies, "prior_auth_threshold")
    refs = [p.ref for p in threshold_rules]
    if not threshold_rules:
        return ToolResult(tool=tool, outcome=RuleOutcome.PASS,
                          detail="No prior authorization rule applies to this procedure/plan.", data={"pa_required": False})
    if claim.amount is None:
        return ToolResult(tool=tool, outcome=RuleOutcome.INDETERMINATE,
                          detail="Cannot evaluate prior authorization threshold without billed amount.",
                          data={"missing": ["amount"]}, policy_refs=refs)

    per_rule = {p.ref: claim.amount > p.rule["amount"] for p in threshold_rules}
    if len(set(per_rule.values())) > 1:
        return ToolResult(tool=tool, outcome=RuleOutcome.INDETERMINATE,
                          detail="Applicable policies disagree on whether prior authorization is required: "
                                 + ", ".join(f"{ref} (threshold ${p.rule['amount']:,}) -> {'required' if per_rule[ref] else 'not required'}"
                                             for p in threshold_rules for ref in [p.ref]),
                          data={"policy_conflict": True, "per_policy_requirement": per_rule}, policy_refs=refs)

    pa_required = next(iter(per_rule.values()))
    if not pa_required:
        return ToolResult(tool=tool, outcome=RuleOutcome.PASS,
                          detail=f"Billed amount ${claim.amount:,.2f} is within the prior authorization threshold.",
                          data={"pa_required": False}, policy_refs=refs)

    auth = _find_valid_auth(claim)
    if auth:
        return ToolResult(tool=tool, outcome=RuleOutcome.PASS,
                          detail=f"Prior authorization required and valid authorization {auth['auth_id']} is on file.",
                          data={"pa_required": True, "auth_on_file": auth["auth_id"]}, policy_refs=refs)
    if claim.prior_authorization:
        return ToolResult(tool=tool, outcome=RuleOutcome.INDETERMINATE,
                          detail="Claim states prior authorization was obtained, but no valid authorization is on file.",
                          data={"pa_required": True, "auth_on_file": None, "missing": ["prior_authorization_record"]},
                          policy_refs=refs)
    return ToolResult(tool=tool, outcome=RuleOutcome.FAIL,
                      detail=f"Prior authorization required for billed amount ${claim.amount:,.2f} and none is on file.",
                      data={"pa_required": True, "auth_on_file": None}, policy_refs=refs)


def _find_valid_auth(claim: Claim) -> dict | None:
    for a in _load("prior_auths.json"):
        if (a["member_id"] == claim.member_id and a["procedure"] == claim.procedure and a["status"] == "APPROVED"
                and claim.date_of_service and _d(a["valid_from"]) <= claim.date_of_service <= _d(a["valid_to"])):
            return a
    return None


def check_emergency_exemption(claim: Claim, policies: list[Policy], waives: str) -> ToolResult:
    """Can an emergency exemption rescue a failed requirement (`waives`)? Unknown status is NOT the same as 'no'."""
    tool = "check_emergency_exemption"
    rules = [p for p in _rules(policies, "emergency_exemption") if waives in p.rule.get("waives", [])]
    refs = [p.ref for p in rules]
    if not rules:
        return ToolResult(tool=tool, outcome=RuleOutcome.FAIL, detail=f"No emergency exemption applies to {waives}.",
                          data={"waives": waives, "exemption_applies": False})
    if claim.emergency_indicator is None:
        return ToolResult(tool=tool, outcome=RuleOutcome.INDETERMINATE,
                          detail=f"An emergency exemption may waive {waives}, but the claim's emergency status is unknown.",
                          data={"waives": waives, "exemption_applies": None, "missing": ["emergency_indicator"]},
                          policy_refs=refs)
    if claim.emergency_indicator:
        # Condition attached to the exemption itself (e.g. retro-authorization within 72h when PA is waived).
        retro = [p for p in rules if waives in p.rule.get("retro_authorization_for", [])]
        if retro:
            hours = min(p.rule["retro_authorization_hours"] for p in retro)
            if claim.retro_auth_requested is None:
                return ToolResult(tool=tool, outcome=RuleOutcome.INDETERMINATE,
                                  detail=f"Emergency documented, but the exemption also requires a retrospective authorization "
                                         f"request within {hours}h and the claim does not say whether one was made.",
                                  data={"waives": waives, "exemption_applies": None, "missing": ["retro_auth_requested"]},
                                  policy_refs=refs)
            if not claim.retro_auth_requested:
                return ToolResult(tool=tool, outcome=RuleOutcome.FAIL,
                                  detail=f"Emergency documented, but no retrospective authorization was requested within {hours}h; "
                                         f"the exemption conditions are not met.",
                                  data={"waives": waives, "exemption_applies": False}, policy_refs=refs)
            detail = (f"Emergency documented and retrospective authorization requested within {hours}h; "
                      f"{waives} is waived.")
        else:
            detail = f"Emergency documented on the claim; {waives} is waived."
        return ToolResult(tool=tool, outcome=RuleOutcome.PASS, detail=detail,
                          data={"waives": waives, "exemption_applies": True}, policy_refs=refs)
    return ToolResult(tool=tool, outcome=RuleOutcome.FAIL,
                      detail=f"Claim documents a non-emergency service; the emergency exemption for {waives} does not apply.",
                      data={"waives": waives, "exemption_applies": False}, policy_refs=refs)


def check_network(claim: Claim, policies: list[Policy]) -> ToolResult:
    tool = "check_network"
    rules = _rules(policies, "network_restriction")
    refs = [p.ref for p in rules]
    if not rules or claim.provider_network != "OUT":
        return ToolResult(tool=tool, outcome=RuleOutcome.PASS, detail="No network restriction violated.",
                          data={"provider_network": claim.provider_network}, policy_refs=refs)
    return ToolResult(tool=tool, outcome=RuleOutcome.FAIL, detail=f"Out-of-network provider is not covered under {claim.plan}.",
                      data={"provider_network": "OUT"}, policy_refs=refs)


def get_claim_history(claim: Claim, policies: list[Policy]) -> ToolResult:
    """Prior claims for this member: duplicate detection and annual visit limits."""
    tool = "get_claim_history"
    history = [h for h in _load("claim_history.json") if h["member_id"] == claim.member_id]
    data: dict = {"prior_claims": [h["claim_id"] for h in history]}
    refs: list[str] = []
    outcome, notes = RuleOutcome.PASS, []

    for p in _rules(policies, "duplicate_window"):
        refs.append(p.ref)
        window = timedelta(days=p.rule["days"])
        dups = [h["claim_id"] for h in history if claim.date_of_service and h["procedure"] == claim.procedure
                and h["status"] == "PAID" and abs(_d(h["date_of_service"]) - claim.date_of_service) <= window]
        if dups:
            data["possible_duplicate_of"] = dups
            notes.append(f"Possible duplicate of {', '.join(dups)} (same procedure within {p.rule['days']} days).")

    for p in _rules(policies, "annual_visit_limit"):
        refs.append(p.ref)
        year = claim.date_of_service.year if claim.date_of_service else None
        used = sum(h.get("units", 1) for h in history if h["procedure"] == claim.procedure
                   and year and _d(h["date_of_service"]).year == year)
        data.update(visits_used=used, visit_limit=p.rule["limit"])
        if used >= p.rule["limit"]:
            outcome = RuleOutcome.FAIL
            notes.append(f"Annual limit reached: {used}/{p.rule['limit']} visits used in {year}.")
        else:
            notes.append(f"{used}/{p.rule['limit']} visits used in {year}.")

    detail = " ".join(notes) or f"{len(history)} prior claim(s); no duplicates or benefit limits triggered."
    return ToolResult(tool=tool, outcome=outcome, detail=detail, data=data, policy_refs=refs)


def calculate_financial_threshold(claim: Claim, policies: list[Policy]) -> ToolResult:
    tool = "calculate_financial_threshold"
    rules = _rules(policies, "high_value_review")
    refs = [p.ref for p in rules]
    if claim.amount is None:
        return ToolResult(tool=tool, outcome=RuleOutcome.INDETERMINATE, detail="Billed amount missing.",
                          data={"missing": ["amount"]}, policy_refs=refs)
    limit = min((p.rule["amount"] for p in rules), default=None)
    high_value = limit is not None and claim.amount > limit
    detail = (f"Billed amount ${claim.amount:,.2f} exceeds the high-value threshold of ${limit:,}." if high_value
              else f"Billed amount ${claim.amount:,.2f} is below the high-value threshold.")
    # High value is a risk signal, not a rule failure; risk evaluation decides routing.
    return ToolResult(tool=tool, outcome=RuleOutcome.PASS, detail=detail,
                      data={"amount": claim.amount, "high_value_threshold": limit, "high_value": high_value}, policy_refs=refs)
