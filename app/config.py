from __future__ import annotations

from datetime import date
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    api_key: str
    base_url: str
    primary_model: str
    fast_model: str
    fast_reasoning_effort: str | None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    database_url: str = "postgresql://postgres:postgres@localhost:55432/peoplelens"

    # LLM provider flag: "grok" (xAI) or "openai". Both use the OpenAI-compatible Chat Completions API.
    llm_provider: Literal["grok", "openai"] = "grok"

    xai_api_key: str = ""
    xai_base_url: str = "https://api.x.ai/v1"
    grok_primary_model: str = "grok-4"
    grok_fast_model: str = "grok-3-mini"

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_primary_model: str = "gpt-5.5"
    openai_fast_model: str = "gpt-5.4-mini"
    # Sent only on calls without tools (routing, text-to-SQL); OpenAI rejects reasoning_effort combined with function
    # tools on Chat Completions for some models. Empty = model default.
    openai_fast_reasoning_effort: str = "low"

    llm_timeout_s: float = 60
    llm_max_retries: int = 2

    jwt_secret: str = "dev-only-secret-change-me-please-32bytes"
    app_today: str = ""

    embed_model: str = "BAAI/bge-small-en-v1.5"
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    rag_min_rerank_score: float = -4.0
    rag_top_k: int = 5

    agent_max_steps: int = 8
    history_turns: int = 6

    api_url: str = "http://localhost:8000"
    cors_origins: str = "http://localhost:8501,http://127.0.0.1:8501"

    # USD per 1M tokens, used only for cost estimates in traces
    price_in_per_m: float = 3.0
    price_out_per_m: float = 15.0

    @property
    def llm(self) -> "LLMConfig":
        """Connection settings for the provider selected by LLM_PROVIDER."""
        if self.llm_provider == "openai":
            return LLMConfig("openai", self.openai_api_key, self.openai_base_url, self.openai_primary_model,
                             self.openai_fast_model, self.openai_fast_reasoning_effort or None)
        return LLMConfig("grok", self.xai_api_key, self.xai_base_url, self.grok_primary_model, self.grok_fast_model, None)

    @property
    def today(self) -> date:
        return date.fromisoformat(self.app_today) if self.app_today else date.today()


@lru_cache
def get_settings() -> Settings:
    return Settings()
