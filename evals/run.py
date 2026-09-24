"""ClaimPilot evaluation harness.

Runs every case in evals/cases.json through the real workflow and computes metrics from the audit records the
workflow wrote. Nothing is hardcoded: change the model, prompt, policies or thresholds and the numbers move.

    python -m evals.run                    # offline, deterministic MockLLM (validates the controls)
    python -m evals.run --provider openai  # real OpenAI-compatible model (needs LLM_API_KEY)

Outputs a console report (with diagnostics for every case that needs attention) and a machine-readable
summary at evals/results/latest.json.
"""

import argparse
import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from app.config import Settings
from app.llm.client import LLMClient, get_llm
from app.models.domain import Claim, Recommendation, RuleOutcome
from app.observability.audit import AuditStore
from app.retrieval.policy_store import get_policy_store
from app.workflows.claim_graph import ClaimInvestigator

EVAL_DIR = Path(__file__).parent


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile: simple, and always an observed value."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


# --------------------------------------------------------------------------- execution


def run_cases(llm: LLMClient, settings: Settings, cases: list[dict]) -> list[dict]:
    """Run each case through the real graph; read results back from the audit record it produced."""
    audit = AuditStore(":memory:")
    investigator = ClaimInvestigator(llm=llm, policy_store=get_policy_store(), audit=audit, settings=settings)
    rows = []
    for case in cases:
        decision = investigator.investigate(Claim(**case["claim"]))
        r = audit.get(decision.execution_id)
        expected = Recommendation(case["expected_recommendation"])
        llm_called = any(step.startswith("analyze_claim") for step in r.routing_trail)
        autonomous = r.recommendation != Recommendation.HUMAN_REVIEW
        rows.append({
            "case_id": case["case_id"],
            "description": case.get("description", ""),
            "execution_id": r.execution_id,
            "expected": expected.value,
            "actual": r.recommendation.value,
            "correct": r.recommendation == expected,
            "expected_policies": case["expected_policy_ids"],
            "retrieved_policies": r.retrieved_policy_ids,
            "supplemental_policies": r.supplemental_policy_ids,
            "retrieval_ok": set(case["expected_policy_ids"]) <= set(r.retrieved_policy_ids),
            "llm_called": llm_called,
            "invalid_output": llm_called and r.llm_error is not None,
            "grounded": llm_called and r.evidence_grounded,
            "grounding_failures": r.grounding_failures,
            "human_review_required": r.human_review_required,
            "escalation_ok": r.human_review_required == (expected == Recommendation.HUMAN_REVIEW),
            # Unsafe = the system acted on its own (APPROVE/DENY) and that action was not the expected outcome,
            # including acting when it should have escalated. This is the release-gating metric.
            "unsafe": autonomous and r.recommendation != expected,
            "confidence": r.confidence,
            "risk_level": r.risk_level.value,
            "deterministic_outcome": r.deterministic_outcome.value,
            "llm_recommendation": r.llm_recommendation.value if r.llm_recommendation else None,
            "llm_error": r.llm_error,
            "missing_information": r.missing_information,
            "tool_summary": r.tool_summary,
            "notable_tools": [s for s, t in zip(r.tool_summary, r.tool_results) if t.outcome != RuleOutcome.PASS],
            "review_reasons": r.human_review_reason,
            "reasoning_summary": r.reasoning_summary,
            "latency_ms": r.latency_ms,
            "llm_latency_ms": r.llm_latency_ms,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "cost": r.estimated_cost,
        })
    return rows


def needs_attention(row: dict) -> list[str]:
    issues = []
    if row["unsafe"]:
        issues.append("UNSAFE autonomous action")
    if not row["correct"]:
        issues.append("wrong recommendation")
    if not row["escalation_ok"]:
        issues.append("wrong escalation")
    if not row["retrieval_ok"]:
        issues.append("policy not retrieved")
    if row["invalid_output"]:
        issues.append("invalid structured output")
    elif row["llm_called"] and not row["grounded"]:
        issues.append("ungrounded response")
    return issues


def summarize(rows: list[dict], provider: str, model: str) -> dict:
    n = len(rows)
    llm_rows = [r for r in rows if r["llm_called"]]
    latencies = [r["latency_ms"] for r in rows]
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "model": model,
        "cases": n,
        "recommendation_accuracy": sum(r["correct"] for r in rows) / n,
        "policy_retrieval_rate": sum(r["retrieval_ok"] for r in rows) / n,
        "grounded_response_rate": sum(r["grounded"] for r in llm_rows) / len(llm_rows) if llm_rows else None,
        "correct_escalation_rate": sum(r["escalation_ok"] for r in rows) / n,
        "unsafe_autonomous_actions": sum(r["unsafe"] for r in rows),
        "invalid_outputs": sum(r["invalid_output"] for r in rows),
        "human_review_rate": sum(r["human_review_required"] for r in rows) / n,
        "llm_calls": len(llm_rows),
        "average_latency_ms": round(mean(latencies), 2),
        "p50_latency_ms": round(percentile(latencies, 50), 2),
        "p95_latency_ms": round(percentile(latencies, 95), 2),
        "average_llm_latency_ms": round(mean(r["llm_latency_ms"] for r in rows), 2),
        "average_input_tokens": round(mean(r["input_tokens"] for r in rows), 1),
        "average_output_tokens": round(mean(r["output_tokens"] for r in rows), 1),
        "estimated_average_cost": round(mean(r["cost"] for r in rows), 6),
        "cases_needing_attention": [r["case_id"] for r in rows if needs_attention(r)],
    }


# --------------------------------------------------------------------------- reporting


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def print_report(summary: dict, rows: list[dict]) -> None:
    s = summary
    print("\nClaimPilot Evaluation")
    print(f"Provider / model:            {s['provider']} / {s['model']}\n")
    print(f"Cases evaluated:             {s['cases']}")
    print(f"Recommendation accuracy:     {_pct(s['recommendation_accuracy'])}")
    print(f"Correct policy retrieval:    {_pct(s['policy_retrieval_rate'])}")
    print(f"Grounded responses:          {_pct(s['grounded_response_rate'])} (of {s['llm_calls']} LLM-analyzed cases)")
    print(f"Correct escalation:          {_pct(s['correct_escalation_rate'])}")
    print(f"Unsafe autonomous actions:   {s['unsafe_autonomous_actions']}")
    print(f"Invalid structured outputs:  {s['invalid_outputs']}")
    print(f"Human review rate:           {_pct(s['human_review_rate'])}")
    print(f"Latency avg / p50 / p95:     {s['average_latency_ms']:.1f} / {s['p50_latency_ms']:.1f} / {s['p95_latency_ms']:.1f} ms"
          f" (LLM avg {s['average_llm_latency_ms']:.1f} ms)")
    print(f"Avg tokens in / out:         {s['average_input_tokens']:.0f} / {s['average_output_tokens']:.0f}")
    print(f"Estimated avg cost/claim:    ${s['estimated_average_cost']:.6f}")

    flagged = [(r, needs_attention(r)) for r in rows if needs_attention(r)]
    if not flagged:
        print("\nAll cases passed every check.")
        return
    print(f"\nCases needing attention ({len(flagged)}):")
    for r, issues in flagged:
        missing_policies = sorted(set(r["expected_policies"]) - set(r["retrieved_policies"]))
        print(f"\n  ▸ {r['case_id']} — {r['description']}")
        print(f"    issues:              {', '.join(issues)}")
        print(f"    expected / actual:   {r['expected']} / {r['actual']}"
              f"   (rules={r['deterministic_outcome']}, model={r['llm_recommendation']}, confidence={r['confidence']:.2f})")
        print(f"    human review:        {r['human_review_required']}   risk={r['risk_level']}")
        print(f"    policies expected:   {r['expected_policies']}")
        print(f"    policies retrieved:  {r['retrieved_policies']}"
              + (f"   MISSING {missing_policies}" if missing_policies else ""))
        if r["missing_information"]:
            print(f"    missing information: {r['missing_information']}")
        for line in r["notable_tools"]:
            print(f"    tool:                {line}")
        for g in r["grounding_failures"]:
            print(f"    grounding failure:   {g}")
        if r["llm_error"]:
            print(f"    llm error:           {r['llm_error']}")
        for reason in r["review_reasons"]:
            print(f"    review reason:       {reason}")
        print(f"    summary:             {r['reasoning_summary'][:240]}")
        print(f"    replay:              execution {r['execution_id']} (in-memory for this run; see results JSON)")


# --------------------------------------------------------------------------- entrypoint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the ClaimPilot evaluation suite.")
    parser.add_argument("--provider", default="mock", choices=["mock", "openai", "auto"])
    parser.add_argument("--cases", default=str(EVAL_DIR / "cases.json"))
    parser.add_argument("--out", default=str(EVAL_DIR / "results" / "latest.json"))
    args = parser.parse_args(argv)

    logging.getLogger("claimpilot").setLevel(logging.WARNING)
    settings = Settings(llm_provider=args.provider)
    if settings.resolved_provider == "openai" and not settings.llm_api_key:
        print("LLM_API_KEY (or OPENAI_API_KEY) is not set; cannot run a real-model evaluation.\n"
              "Set it (plus LLM_MODEL / LLM_BASE_URL as needed) or run with --provider mock.", file=sys.stderr)
        return 2

    llm = get_llm(settings)
    rows = run_cases(llm, settings, json.loads(Path(args.cases).read_text()))
    summary = summarize(rows, llm.provider, llm.model)
    print_report(summary, rows)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({**summary, "results": rows}, indent=2, default=str))
    print(f"\nMachine-readable results: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
