"""ClaimPilot evaluation harness.

Runs every case in evals/cases.json through the real workflow and computes metrics from the audit records.
Nothing here is hardcoded: change the model, prompt, policies or thresholds and the numbers move.

    python -m evals.run                    # offline, deterministic MockLLM (validates the controls)
    python -m evals.run --provider openai  # real OpenAI-compatible model (measures model behaviour)
"""

import argparse
import json
import logging
from pathlib import Path
from statistics import mean

from app.config import Settings
from app.llm.client import get_llm
from app.models.domain import Claim, Recommendation
from app.observability.audit import AuditStore
from app.retrieval.policy_store import get_policy_store
from app.workflows.claim_graph import ClaimInvestigator

EVAL_DIR = Path(__file__).parent


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "n/a"


def run(provider: str, cases_path: Path) -> dict:
    logging.getLogger("claimpilot").setLevel(logging.WARNING)
    settings = Settings(llm_provider=provider, audit_db_path=":memory:")
    audit = AuditStore(":memory:")
    llm = get_llm(settings)
    investigator = ClaimInvestigator(llm=llm, policy_store=get_policy_store(), audit=audit, settings=settings)

    rows = []
    for case in json.loads(cases_path.read_text()):
        decision = investigator.investigate(Claim(**case["claim"]))
        record = audit.get(decision.execution_id)
        expected = Recommendation(case["expected_recommendation"])
        llm_ran = record.llm_recommendation is not None
        rows.append({
            "case_id": case["case_id"],
            "expected": expected.value,
            "actual": decision.recommendation.value,
            "correct": decision.recommendation == expected,
            "retrieval_ok": set(case["expected_policy_ids"]) <= set(record.retrieved_policy_ids),
            "llm_ran": llm_ran,
            "grounded": llm_ran and bool(record.policy_evidence)
                        and not any("Ungrounded" in r for r in record.human_review_reason),
            "escalation_ok": decision.human_review_required == (expected == Recommendation.HUMAN_REVIEW),
            # Unsafe = the system acted autonomously and got it wrong (incl. acting when it should have escalated).
            "unsafe": decision.recommendation != Recommendation.HUMAN_REVIEW and decision.recommendation != expected,
            "latency_ms": record.latency_ms,
            "llm_latency_ms": record.llm_latency_ms,
            "tokens": record.input_tokens + record.output_tokens,
            "cost": record.estimated_cost,
            "retrieved": record.retrieved_policy_ids,
            "reasons": record.human_review_reason,
        })

    n = len(rows)
    llm_rows = [r for r in rows if r["llm_ran"]]
    summary = {
        "provider": llm.provider,
        "model": llm.model,
        "cases": n,
        "recommendation_accuracy": sum(r["correct"] for r in rows) / n,
        "policy_retrieval_rate": sum(r["retrieval_ok"] for r in rows) / n,
        "grounded_response_rate": (sum(r["grounded"] for r in llm_rows) / len(llm_rows)) if llm_rows else None,
        "correct_escalation_rate": sum(r["escalation_ok"] for r in rows) / n,
        "unsafe_autonomous_actions": sum(r["unsafe"] for r in rows),
        "human_review_rate": sum(r["actual"] == "HUMAN_REVIEW" for r in rows) / n,
        "avg_latency_ms": mean(r["latency_ms"] for r in rows),
        "avg_llm_latency_ms": mean(r["llm_latency_ms"] for r in rows),
        "avg_tokens": mean(r["tokens"] for r in rows),
        "avg_cost_usd": mean(r["cost"] for r in rows),
    }
    return {"summary": summary, "cases": rows}


def print_report(result: dict) -> None:
    s, rows = result["summary"], result["cases"]
    n = s["cases"]
    llm_rows = [r for r in rows if r["llm_ran"]]
    print("\nClaimPilot Evaluation")
    print(f"Provider / model:          {s['provider']} / {s['model']}\n")
    print(f"Cases evaluated:           {n}")
    print(f"Recommendation accuracy:   {pct(sum(r['correct'] for r in rows), n)}")
    print(f"Correct policy retrieval:  {pct(sum(r['retrieval_ok'] for r in rows), n)}")
    print(f"Grounded responses:        {pct(sum(r['grounded'] for r in llm_rows), len(llm_rows))} (of {len(llm_rows)} LLM-analyzed cases)")
    print(f"Correct escalation:        {pct(sum(r['escalation_ok'] for r in rows), n)}")
    print(f"Unsafe autonomous actions: {s['unsafe_autonomous_actions']}")
    print(f"Human review rate:         {s['human_review_rate']:.0%}")
    print(f"Average latency:           {s['avg_latency_ms'] / 1000:.3f}s (LLM {s['avg_llm_latency_ms'] / 1000:.3f}s)")
    print(f"Average tokens/case:       {s['avg_tokens']:.0f}")
    print(f"Average cost/case:         ${s['avg_cost_usd']:.5f}")

    failures = [r for r in rows if not (r["correct"] and r["retrieval_ok"] and r["escalation_ok"])]
    if failures:
        print("\nCases needing attention:")
        for r in failures:
            issues = [k for k in ("correct", "retrieval_ok", "escalation_ok") if not r[k]]
            print(f"  - {r['case_id']}: expected {r['expected']}, got {r['actual']} [{', '.join(issues)}] retrieved={r['retrieved']}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the ClaimPilot evaluation suite.")
    parser.add_argument("--provider", default="mock", choices=["mock", "openai", "auto"])
    parser.add_argument("--cases", default=str(EVAL_DIR / "cases.json"))
    parser.add_argument("--out", default=str(EVAL_DIR / "results" / "latest.json"))
    args = parser.parse_args()

    result = run(args.provider, Path(args.cases))
    print_report(result)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, default=str))
    print(f"Full results written to {out}")


if __name__ == "__main__":
    main()
