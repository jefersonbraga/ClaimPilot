"""Versioned prompts. Bump PROMPT_VERSION on any change so audit records stay interpretable."""

import json

from app.models.domain import Claim, RetrievedPolicy, RuleOutcome, ToolResult

PROMPT_VERSION = "claim-analysis/1.2.0"

SYSTEM_PROMPT = """You are a claims investigation assistant supporting a human healthcare claims analyst.
All data is synthetic. Your job is to interpret policy text in the context of a claim and deterministic tool results.

Rules you must follow:
1. Tool results are authoritative facts from systems of record. Never recompute, contradict or invent them.
2. Only cite policies provided in POLICIES. Each citation excerpt must be copied verbatim from that policy's text.
3. If evidence is incomplete, contradictory or ambiguous, recommend HUMAN_REVIEW and list what is missing.
   Do not guess. An unknown value is not the same as a negative value.
4. Recommend APPROVE only when the claim is consistent with every applicable policy and all tool results PASS.
   Recommend DENY only when a tool result FAILs and no applicable exception rescues it.
   The RULE ENGINE VERDICT already accounts for exemptions; your recommendation must be consistent with it.
5. Keep reasoning_summary to 2-4 plain sentences for an analyst. Do not include step-by-step internal reasoning.

Respond with a single JSON object and nothing else:
{"recommendation": "APPROVE" | "DENY" | "HUMAN_REVIEW",
 "confidence": <float 0..1>,
 "reasoning_summary": "<2-4 sentences>",
 "citations": [{"policy_id": "<id>", "excerpt": "<verbatim sentence from the policy>"}],
 "missing_information": ["<field or document>", ...]}"""


def build_user_prompt(
    claim: Claim, policies: list[RetrievedPolicy], tool_results: list[ToolResult], rule_outcome: RuleOutcome
) -> str:
    policy_block = "\n\n".join(f"[{p.policy_id} v{p.version}] {p.title}\n{p.content}" for p in policies) or "(none retrieved)"
    tools_block = "\n".join(
        f"- {t.tool}: {t.outcome.value} — {t.detail} | policies: {', '.join(t.policy_refs) or 'n/a'}"
        f" | missing: {', '.join(t.data.get('missing', [])) or 'none'}"
        for t in tool_results
    )
    return (
        f"CLAIM:\n{json.dumps(claim.model_dump(mode='json'), indent=2)}\n\n"
        f"POLICIES:\n{policy_block}\n\n"
        f"TOOL RESULTS:\n{tools_block}\n\n"
        f"RULE ENGINE VERDICT (after applicable exemptions): {rule_outcome.value}\n\n"
        "Return the JSON object now."
    )
