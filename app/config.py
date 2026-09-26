"""Runtime configuration. Everything that a business owner might want to tune lives here."""

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"

WORKFLOW_VERSION = "claim-investigation-wf/1.2.0"  # 1.1.0: findings-driven retrieval · 1.2.0: retro-auth condition on emergency exemption


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _env(*names: str) -> str | None:
    return next((v for n in names if (v := os.getenv(n))), None)


# OpenAI-compatible provider profiles. Each has its own key variable, so switching provider is one line
# (LLM_PROVIDER=groq) and keys for several providers can sit side by side in .env.
# Prices are USD per 1K tokens (list prices; verify on the provider's pricing page). LLM_PRICE_* overrides them.
PROVIDERS = {
    "openai": {  # generic: OpenAI, Azure OpenAI, a model gateway, vLLM, Ollama... configured with LLM_*
        "key_envs": ("LLM_API_KEY", "OPENAI_API_KEY"), "model_env": "LLM_MODEL", "base_url_env": "LLM_BASE_URL",
        "base_url": None, "model": "gpt-4.1-mini", "price_in": 0.0004, "price_out": 0.0016,
    },
    "deepseek": {
        "key_envs": ("DEEPSEEK_API_KEY",), "model_env": "DEEPSEEK_MODEL", "base_url_env": None,
        "base_url": "https://api.deepseek.com", "model": "deepseek-flash", "price_in": 0.0003, "price_out": 0.0012,
    },
    "groq": {
        "key_envs": ("GROQ_API_KEY",), "model_env": "GROQ_MODEL", "base_url_env": None,
        "base_url": "https://api.groq.com/openai/v1", "model": "openai/gpt-oss-120b",
        "price_in": 0.00015, "price_out": 0.00075,
    },
    # Self-hosted, OpenAI-compatible server inside your own network (vLLM, Ollama, NIM, llama.cpp...), e.g. a DGX
    # Spark reached over a private tailnet. Claims never leave the boundary; marginal model cost is zero.
    # Offered only when LOCAL_BASE_URL is set and its health endpoint answers; never auto-selected.
    "local": {
        "key_envs": ("LOCAL_API_KEY",), "model_env": "LOCAL_MODEL", "base_url_env": "LOCAL_BASE_URL",
        "base_url": None, "model": "qwen", "price_in": 0.0, "price_out": 0.0, "key_optional": True,
    },
}
# With LLM_PROVIDER=auto, the first key found decides the provider.
AUTO_ORDER = (("LLM_API_KEY", "openai"), ("DEEPSEEK_API_KEY", "deepseek"), ("GROQ_API_KEY", "groq"), ("OPENAI_API_KEY", "openai"))


@dataclass(frozen=True)
class Settings:
    # LLM — auto | mock | openai | deepseek | groq. Fields left as None are resolved from the provider profile.
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "auto"))
    llm_model: str | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_timeout_s: float = field(default_factory=lambda: _float("LLM_TIMEOUT_S", 30))
    llm_max_output_tokens: int = field(default_factory=lambda: int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "600")))
    # USD per 1K tokens, used for cost estimation and the daily budget.
    price_input_per_1k: float | None = None
    price_output_per_1k: float | None = None
    # Provider-specific request fields (OpenAI SDK extra_body), e.g. turning off Qwen3 "thinking" on vLLM.
    llm_extra_body: dict | None = None

    # Human-in-the-loop thresholds
    confidence_threshold: float = field(default_factory=lambda: _float("CONFIDENCE_THRESHOLD", 0.80))
    retrieval_top_k: int = field(default_factory=lambda: int(os.getenv("RETRIEVAL_TOP_K", "4")))
    # Second-pass retrieval driven by tool findings: small k and a relevance floor keep it from adding noise.
    supplemental_top_k: int = field(default_factory=lambda: int(os.getenv("SUPPLEMENTAL_RETRIEVAL_TOP_K", "2")))
    supplemental_min_score: float = field(default_factory=lambda: _float("SUPPLEMENTAL_RETRIEVAL_MIN_SCORE", 0.15))

    # Public-demo protection (API layer only; the eval harness is not limited)
    rate_limit_per_minute: int = field(default_factory=lambda: int(os.getenv("RATE_LIMIT_PER_MINUTE", "10")))
    general_rate_limit_per_minute: int = field(default_factory=lambda: int(os.getenv("GENERAL_RATE_LIMIT_PER_MINUTE", "300")))
    daily_max_analyses: int = field(default_factory=lambda: int(os.getenv("DAILY_MAX_ANALYSES", "300")))
    daily_llm_budget_usd: float = field(default_factory=lambda: _float("DAILY_LLM_BUDGET_USD", 1.0))

    # Private usage dashboard at /admin (disabled unless a strong token is configured)
    admin_token: str | None = field(default_factory=lambda: os.getenv("ADMIN_TOKEN") or None)

    # Storage
    audit_db_path: str = field(default_factory=lambda: os.getenv("AUDIT_DB_PATH", str(ROOT_DIR / "claimpilot.db")))

    def __post_init__(self):
        provider = self.llm_provider
        if provider == "auto":
            provider = next((p for env, p in AUTO_ORDER if os.getenv(env)), "mock")
        if provider not in PROVIDERS and provider != "mock":
            raise ValueError(f"Unknown LLM_PROVIDER '{provider}'. Use: auto, mock, {', '.join(PROVIDERS)}.")
        profile = PROVIDERS.get(provider, PROVIDERS["openai"])  # mock borrows default prices for its estimates
        resolved = {
            "llm_provider": provider,
            "llm_api_key": self.llm_api_key or (_env(*profile["key_envs"]) if provider != "mock" else None)
                           or ("not-needed" if profile.get("key_optional") else None),
            "llm_model": self.llm_model or _env(profile["model_env"]) or profile["model"],
            "llm_base_url": self.llm_base_url or (_env(profile["base_url_env"]) if profile["base_url_env"] else None)
                            or profile["base_url"],
            "price_input_per_1k": self.price_input_per_1k if self.price_input_per_1k is not None
                                  else _float("LLM_PRICE_INPUT_PER_1K", profile["price_in"]),
            "price_output_per_1k": self.price_output_per_1k if self.price_output_per_1k is not None
                                   else _float("LLM_PRICE_OUTPUT_PER_1K", profile["price_out"]),
        }
        if self.llm_extra_body is None and provider == "local":
            # Qwen3-style models on vLLM reason before answering; for this structured task the rules already did the
            # heavy lifting, so thinking only burns the output budget (measured: ~8 s and truncated JSON vs <1 s).
            thinking = os.getenv("LOCAL_ENABLE_THINKING", "false").lower() == "true"
            resolved["llm_extra_body"] = {"chat_template_kwargs": {"enable_thinking": thinking}}
        for name, value in resolved.items():
            object.__setattr__(self, name, value)

    @property
    def resolved_provider(self) -> str:
        return self.llm_provider


def local_health_url(settings: Settings) -> str | None:
    """Health endpoint of the self-hosted server: LOCAL_HEALTH_URL, else <base>/health (vLLM, TGI, NIM)."""
    if explicit := os.getenv("LOCAL_HEALTH_URL"):
        return explicit
    if not settings.llm_base_url:
        return None
    return settings.llm_base_url.rstrip("/").removesuffix("/v1") + "/health"


def get_settings() -> Settings:
    return Settings()
