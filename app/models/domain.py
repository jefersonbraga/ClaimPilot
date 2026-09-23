"""Domain models. These are the contracts between the API, the workflow, the LLM and the audit log."""

from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Recommendation(str, Enum):
    APPROVE = "APPROVE"
    DENY = "DENY"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class RuleOutcome(str, Enum):
    """Outcome of the deterministic rule engine. This is authoritative; the LLM cannot override it."""

    PASS = "PASS"
    FAIL = "FAIL"
    INDETERMINATE = "INDETERMINATE"


# --------------------------------------------------------------------------- claim


class Claim(BaseModel):
    """A synthetic claim. Business-required fields are Optional on purpose: a missing field is a
    business condition (route to a human and say what's missing), not an HTTP validation error."""

    claim_id: str = Field(min_length=1)
    member_id: str | None = None
    provider: str | None = None
    procedure: str | None = None
    amount: float | None = Field(default=None, ge=0)
    diagnosis_code: str | None = None
    plan: str | None = None
    prior_authorization: bool | None = None
    date_of_service: date | None = None
    emergency_indicator: bool | None = None  # None == unknown, which is different from False
    provider_network: Literal["IN", "OUT"] | None = "IN"
    region: str | None = None


REQUIRED_CLAIM_FIELDS = (
    "member_id",
    "provider",
    "procedure",
    "amount",
    "diagnosis_code",
    "plan",
    "prior_authorization",
    "date_of_service",
)


# --------------------------------------------------------------------------- policies


class Policy(BaseModel):
    policy_id: str
    version: str
    title: str
    effective_from: date
    effective_to: date | None = None
    source: str
    applies_to: dict[str, list[str]] = Field(default_factory=dict)  # {"plans": [...], "procedures": [...], "regions": [...]}
    rule: dict[str, Any] | None = None  # machine-readable rule consumed by deterministic tools
    content: str

    @property
    def ref(self) -> str:
        return f"{self.policy_id}@{self.version}"

    def is_effective_on(self, d: date) -> bool:
        return self.effective_from <= d and (self.effective_to is None or d <= self.effective_to)


class RetrievedPolicy(BaseModel):
    policy_id: str
    version: str
    title: str
    score: float
    content: str


class PolicyEvidence(BaseModel):
    policy_id: str
    version: str
    excerpt: str


# --------------------------------------------------------------------------- tools


class ToolResult(BaseModel):
    tool: str
    outcome: RuleOutcome
    detail: str
    data: dict[str, Any] = Field(default_factory=dict)
    policy_refs: list[str] = Field(default_factory=list)  # e.g. ["POL-MRI-001@2.0"]


# --------------------------------------------------------------------------- LLM


class Citation(BaseModel):
    policy_id: str
    excerpt: str


class LLMAnalysis(BaseModel):
    """What the model is allowed to return. No free-form chain-of-thought — only a short summary."""

    recommendation: Recommendation
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning_summary: str = Field(max_length=1200)
    citations: list[Citation] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)


class LLMUsage(BaseModel):
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    estimated_cost: float = 0.0


# --------------------------------------------------------------------------- outputs


class ClaimDecision(BaseModel):
    """API response for POST /claims/analyze."""

    execution_id: str
    claim_id: str
    recommendation: Recommendation
    confidence: float
    risk_level: RiskLevel
    reasoning_summary: str
    policy_evidence: list[PolicyEvidence]
    tool_results: list[ToolResult]
    deterministic_outcome: RuleOutcome
    human_review_required: bool
    human_review_reasons: list[str]
    missing_information: list[str]
    estimated_tokens: int
    estimated_cost: float
    latency_ms: float


class ExecutionRecord(BaseModel):
    """Everything needed to replay how a decision was produced (without chain-of-thought)."""

    execution_id: str
    claim_id: str
    timestamp: datetime
    model: str
    llm_provider: str
    prompt_version: str
    workflow_version: str
    claim: Claim
    retrieved_policy_ids: list[str]
    retrieved_policy_versions: dict[str, str]
    policy_evidence: list[PolicyEvidence]
    tools_executed: list[str]
    tool_results: list[ToolResult]
    deterministic_outcome: RuleOutcome
    llm_recommendation: Recommendation | None
    llm_error: str | None
    confidence: float
    risk_level: RiskLevel
    risk_factors: list[str]
    recommendation: Recommendation
    reasoning_summary: str
    human_review_required: bool
    human_review_reason: list[str]
    missing_information: list[str]
    routing_trail: list[str]
    latency_ms: float
    llm_latency_ms: float
    input_tokens: int
    output_tokens: int
    estimated_cost: float
