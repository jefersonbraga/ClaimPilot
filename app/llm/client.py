"""OpenAI-compatible LLM abstraction.

Any endpoint speaking the OpenAI chat-completions protocol works (OpenAI, Azure OpenAI, an enterprise model
gateway, vLLM, Ollama...). `MockLLM` is a deterministic stand-in so tests and evals run offline and reproducibly.
"""

import json
import re
import time
from typing import Protocol

from pydantic import ValidationError

from app.config import Settings
from app.llm.prompts import SYSTEM_PROMPT
from app.models.domain import LLMAnalysis, LLMUsage


class LLMError(Exception):
    """Raised when the model output can't be turned into a valid LLMAnalysis. The workflow falls back to human review."""


class LLMClient(Protocol):
    provider: str
    model: str

    def complete(self, system: str, user: str) -> tuple[str, int, int]:
        """Return (raw_text, input_tokens, output_tokens)."""
        ...


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def analyze(llm: LLMClient, user_prompt: str, settings: Settings) -> tuple[LLMAnalysis | None, LLMUsage, str | None]:
    """Call the model and validate its structured output. Never raises: errors come back as the third element."""
    start = time.perf_counter()
    error = None
    analysis = None
    in_tok = out_tok = 0
    try:
        raw, in_tok, out_tok = llm.complete(SYSTEM_PROMPT, user_prompt)
        analysis = parse_analysis(raw)
    except LLMError as e:
        error = str(e)
    except Exception as e:  # network, auth, timeout... degrade to human review, don't 500
        error = f"LLM call failed: {type(e).__name__}: {e}"
    usage = LLMUsage(
        model=llm.model,
        provider=llm.provider,
        input_tokens=in_tok,
        output_tokens=out_tok,
        latency_ms=round((time.perf_counter() - start) * 1000, 2),
        estimated_cost=round(in_tok / 1000 * settings.price_input_per_1k + out_tok / 1000 * settings.price_output_per_1k, 6),
    )
    return analysis, usage, error


def parse_analysis(raw: str) -> LLMAnalysis:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise LLMError("Model output did not contain a JSON object.")
    try:
        return LLMAnalysis.model_validate(json.loads(match.group(0)))
    except (json.JSONDecodeError, ValidationError) as e:
        raise LLMError(f"Model output failed schema validation: {e.__class__.__name__}") from e


# --------------------------------------------------------------------------- implementations


class OpenAICompatibleLLM:
    provider = "openai-compatible"

    def __init__(self, settings: Settings):
        from openai import OpenAI

        self.model = settings.llm_model
        self._client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url, timeout=settings.llm_timeout_s)

    def complete(self, system: str, user: str) -> tuple[str, int, int]:
        resp = self._client.chat.completions.create(
            model=self.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        usage = resp.usage
        text = resp.choices[0].message.content or ""
        return text, (usage.prompt_tokens if usage else estimate_tokens(system + user)), (
            usage.completion_tokens if usage else estimate_tokens(text))


class MockLLM:
    """Deterministic 'simulated analyst'. It reads only what a real model would see (the prompt) and follows the
    system prompt's rules literally. It exists to exercise the workflow and its guardrails offline — it is NOT
    evidence of model quality. Run evals with a real provider to measure that."""

    provider = "mock"
    model = "mock-analyst-v1"

    def complete(self, system: str, user: str) -> tuple[str, int, int]:
        policies = dict(re.findall(r"^\[(\S+) v[^\]]+\] [^\n]*\n(.+?)(?=\n\n\[|\n\nTOOL RESULTS:)", user, re.DOTALL | re.MULTILINE))
        tools = re.findall(r"^- (\w+): (PASS|FAIL|INDETERMINATE) — (.*?) \| policies: (.*?) \| missing: (.*?)$", user, re.MULTILINE)
        verdict = re.search(r"^RULE ENGINE VERDICT[^:]*: (\w+)", user, re.MULTILINE).group(1)

        # Cite the first sentence of each retrieved policy that a tool actually relied on.
        used = {ref.split("@")[0] for t in tools for ref in t[3].split(", ") if ref != "n/a"}
        citations = [{"policy_id": pid, "excerpt": text.strip().split("\n")[0]} for pid, text in policies.items() if pid in used]

        missing = []
        if verdict == "INDETERMINATE":
            rec, conf = "HUMAN_REVIEW", 0.55
            missing = sorted({m for t in tools if t[4] != "none" for m in t[4].split(", ")})
            summary = "Evidence is incomplete or conflicting: " + " ".join(t[2] for t in tools if t[1] == "INDETERMINATE")
        elif verdict == "FAIL":
            rec, conf = "DENY", 0.9
            summary = "A required policy condition is not met: " + " ".join(t[2] for t in tools if t[1] == "FAIL")
        else:
            rec, conf = "APPROVE", 0.92
            summary = "All required conditions are met after applicable exemptions: " + " ".join(
                t[2] for t in tools if t[0] in ("check_emergency_exemption", "check_prior_authorization", "check_member_eligibility"))

        out = json.dumps({"recommendation": rec, "confidence": conf, "reasoning_summary": summary[:1200],
                          "citations": citations, "missing_information": missing})
        return out, estimate_tokens(system + user), estimate_tokens(out)


def get_llm(settings: Settings) -> LLMClient:
    return OpenAICompatibleLLM(settings) if settings.resolved_provider == "openai" else MockLLM()
