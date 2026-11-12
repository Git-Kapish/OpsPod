"""Typed settings. Keys come from the environment only; never commit them."""

from enum import StrEnum
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMMode(StrEnum):
    LIVE = "live"
    REPLAY = "replay"
    FAKE = "fake"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://opspod:opspod@localhost:5433/opspod"
    redis_url: str = "redis://localhost:6381/0"
    openrouter_api_key: SecretStr = SecretStr("")
    gemini_api_key: SecretStr = SecretStr("")
    llm_mode: LLMMode = LLMMode.FAKE
    llm_daily_call_budget: int = 900
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    app_env: str = "dev"
    # "key=role1|role2;key2=role": API key -> roles it may assume (X-Role header selects one)
    api_keys: str = (
        "dev-analyst=analyst;dev-approver=analyst|approver;"
        "dev-admin=analyst|approver|admin;dev-agent=agent-service"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
