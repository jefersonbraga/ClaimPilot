"""Versioned policy store + a small TF-IDF retriever.

Two distinct uses, on purpose:
  * `applicable_policies()` — deterministic metadata match. Used by the rule tools, never by ranking.
  * `retrieve()`            — ranked retrieval of policy text for the LLM context (what an analyst would read).

MVP trade-off: an in-memory TF-IDF index is enough for ~10 documents and needs no infrastructure.
Production path: pgvector + embeddings, same interface.
"""

import math
import re
from collections import Counter
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml

from app.config import DATA_DIR
from app.models.domain import Claim, Policy, RetrievedPolicy

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = {"the", "a", "an", "of", "for", "and", "or", "to", "is", "are", "be", "on", "in", "as", "by", "with", "when", "must", "not", "under", "such", "this", "that", "any", "at", "than"}


def _tokens(text: str) -> list[str]:
    # Split identifiers like MRI_KNEE / GOLD_PPO into their parts so they match prose.
    return [t for t in _TOKEN.findall(text.lower().replace("_", " ")) if t not in _STOPWORDS]


def load_policies(policy_dir: Path = DATA_DIR / "policies") -> list[Policy]:
    policies = []
    for path in sorted(policy_dir.glob("*.md")):
        _, front, body = path.read_text().split("---", 2)
        meta = yaml.safe_load(front)
        policies.append(Policy(**meta, content=body.strip()))
    return policies


def _matches(values: list[str] | None, value: str | None) -> bool:
    """Empty scope means 'applies to all'."""
    return not values or (value is not None and value in values)


class PolicyStore:
    def __init__(self, policies: list[Policy]):
        self.policies = policies
        self._doc_tokens = [Counter(_tokens(f"{p.title} {p.content}")) for p in policies]
        n = len(policies)
        df = Counter(t for toks in self._doc_tokens for t in toks)
        self._idf = {t: math.log((1 + n) / (1 + c)) + 1 for t, c in df.items()}
        self._doc_vecs = [self._vector(toks) for toks in self._doc_tokens]

    # ------------------------------------------------------------------ metadata filtering

    def effective_policies(self, on: date | None) -> list[Policy]:
        on = on or date.today()
        return [p for p in self.policies if p.is_effective_on(on)]

    def applicable_policies(self, claim: Claim, region: str | None = None) -> list[Policy]:
        """Policies whose scope (plan / procedure / region) covers this claim on its date of service."""
        region = region or claim.region
        return [
            p
            for p in self.effective_policies(claim.date_of_service)
            if _matches(p.applies_to.get("plans"), claim.plan)
            and _matches(p.applies_to.get("procedures"), claim.procedure)
            and _matches(p.applies_to.get("regions"), region)
        ]

    def get(self, policy_id: str, version: str) -> Policy | None:
        return next((p for p in self.policies if p.policy_id == policy_id and p.version == version), None)

    # ------------------------------------------------------------------ ranked retrieval

    def _vector(self, toks: Counter) -> dict[str, float]:
        vec = {t: c * self._idf.get(t, 0.0) for t, c in toks.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / norm for t, v in vec.items()}

    def retrieve(self, query: str, claim: Claim, top_k: int = 4, region: str | None = None,
                 min_score: float = 0.0) -> list[RetrievedPolicy]:
        """Rank policies in scope for the claim by similarity to the investigation query."""
        candidates = {id(p) for p in self.applicable_policies(claim, region)}
        q = self._vector(Counter(_tokens(query)))
        scored = []
        for p, dvec in zip(self.policies, self._doc_vecs):
            if id(p) not in candidates:
                continue
            score = sum(w * dvec.get(t, 0.0) for t, w in q.items())
            if score > min_score:
                scored.append((score, p))
        scored.sort(key=lambda s: s[0], reverse=True)
        return [
            RetrievedPolicy(policy_id=p.policy_id, version=p.version, title=p.title, score=round(s, 4), content=p.content)
            for s, p in scored[:top_k]
        ]


def build_query(claim: Claim) -> str:
    """Turn a claim into an investigation query — what an analyst would search for."""
    parts = [claim.procedure or "", claim.plan or "", "coverage eligibility"]
    if claim.prior_authorization is not True:
        parts.append("prior authorization required billed amount exceeds exceptions emergency")
    if claim.emergency_indicator:
        parts.append("emergency services exemption")
    if claim.provider_network == "OUT":
        parts.append("out-of-network providers network restriction")
    if claim.amount and claim.amount > 5000:
        parts.append("high-value claims billed amount review")
    return " ".join(parts)


@lru_cache(maxsize=1)
def get_policy_store() -> PolicyStore:
    return PolicyStore(load_policies())
