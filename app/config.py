"""Runtime configuration. Everything that a business owner might want to tune lives here."""

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"

WORKFLOW_VERSION = "claim-investigation-wf/1.1.0"  # 1.1.0: findings-driven supplemental retrieval


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    # LLM
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "auto"))  # auto | mock | openai
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4.1-mini"))
    llm_base_url: str | None = field(default_factory=lambda: os.getenv("LLM_BASE_URL") or None)
    llm_api_key: str | None = field(default_factory=lambda: os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or None)
    llm_timeout_s: float = field(default_factory=lambda: _float("LLM_TIMEOUT_S", 30))
    # USD per 1K tokens, used for cost estimation only.
    price_input_per_1k: float = field(default_factory=lambda: _float("LLM_PRICE_INPUT_PER_1K", 0.0004))
    price_output_per_1k: float = field(default_factory=lambda: _float("LLM_PRICE_OUTPUT_PER_1K", 0.0016))

    # Human-in-the-loop thresholds
    confidence_threshold: float = field(default_factory=lambda: _float("CONFIDENCE_THRESHOLD", 0.80))
    retrieval_top_k: int = field(default_factory=lambda: int(os.getenv("RETRIEVAL_TOP_K", "4")))
    # Second-pass retrieval driven by tool findings: small k and a relevance floor keep it from adding noise.
    supplemental_top_k: int = field(default_factory=lambda: int(os.getenv("SUPPLEMENTAL_RETRIEVAL_TOP_K", "2")))
    supplemental_min_score: float = field(default_factory=lambda: _float("SUPPLEMENTAL_RETRIEVAL_MIN_SCORE", 0.15))

    # Storage
    audit_db_path: str = field(default_factory=lambda: os.getenv("AUDIT_DB_PATH", str(ROOT_DIR / "claimpilot.db")))

    @property
    def resolved_provider(self) -> str:
        if self.llm_provider == "auto":
            return "openai" if self.llm_api_key else "mock"
        return self.llm_provider


def get_settings() -> Settings:
    return Settings()
