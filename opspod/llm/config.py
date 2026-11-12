"""LLM routing config. Model names and limits live in config/llm.yaml (written by `make probe`)."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

LLM_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "llm.yaml"


class Candidate(BaseModel):
    provider: str
    model: str
    native_schema: bool = True  # False: rely on prompt-only JSON + Pydantic validation


class ProviderLimits(BaseModel):
    requests_per_minute: float = 10
    daily_calls: int = 900
    daily_tokens: int | None = None


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tasks: dict[str, list[Candidate]] = {}  # task -> primary first, then ordered fallbacks
    limits: dict[str, ProviderLimits] = {}
    timeout_s: float = 30.0
    breaker_threshold: int = 5
    breaker_reset_s: float = 30.0
    backoff_base_s: float = 0.5
    max_wait_s: float = 30.0  # cap on Retry-After / backoff sleeps
    queue_wait_s: float = 5.0  # shed load instead of waiting longer than this for a rate slot


def load_llm_config(path: Path = LLM_CONFIG_PATH) -> LLMConfig:
    return LLMConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
