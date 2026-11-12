"""Build a Gateway for LLM_MODE: live | replay (cassettes) | fake. Tests/CI never use live."""

from collections.abc import Callable
from pathlib import Path

import httpx

from opspod.core.chaos import ChaosSource
from opspod.core.config import LLMMode, Settings
from opspod.llm.config import Candidate, LLMConfig, ProviderLimits
from opspod.llm.gateway import CallEvent, Gateway
from opspod.llm.providers.cassette import CassetteProvider
from opspod.llm.providers.chaos import ChaosProvider
from opspod.llm.providers.fake import FakeProvider
from opspod.llm.providers.gemini import GeminiProvider
from opspod.llm.providers.openrouter import OpenRouterProvider
from opspod.llm.types import Provider

CASSETTE_DIR = Path(__file__).resolve().parents[2] / "evals" / "cassettes"
TASKS = ("classify", "analyze", "draft_message")


def build_gateway(
    mode: LLMMode,
    config: LLMConfig,
    settings: Settings,
    *,
    fake: FakeProvider | None = None,
    record_cassettes: bool = False,
    cassette_dir: Path = CASSETTE_DIR,
    on_event: Callable[[CallEvent], None] | None = None,
    chaos: ChaosSource | None = None,
) -> Gateway:
    names = {c.provider for chain in config.tasks.values() for c in chain}
    providers: dict[str, Provider]
    if mode is LLMMode.FAKE:
        fake = fake or FakeProvider()
        providers = {"fake": fake}
        for extra in names - {"fake"}:  # multi-provider demos/incident drills
            providers[extra] = FakeProvider(fake.script, name=extra)
        if not any(config.tasks.values()):  # no probe results needed for tests
            config = config.model_copy(
                update={"tasks": {t: [Candidate(provider="fake", model="fake-1")] for t in TASKS}}
            )
    elif mode is LLMMode.REPLAY:
        providers = {n: CassetteProvider(cassette_dir, n) for n in names}
    else:
        client = httpx.AsyncClient()
        live: dict[str, Provider] = {}
        if settings.openrouter_api_key.get_secret_value():
            live["openrouter"] = OpenRouterProvider(
                settings.openrouter_api_key.get_secret_value(), client
            )
        if settings.gemini_api_key.get_secret_value():
            live["gemini"] = GeminiProvider(settings.gemini_api_key.get_secret_value(), client)
        providers = (
            {n: CassetteProvider(cassette_dir, n, inner=p) for n, p in live.items()}
            if record_cassettes
            else live
        )
    if mode is LLMMode.FAKE:  # fakes have no real limit unless the config sets one
        limits = {n: ProviderLimits(requests_per_minute=6000, daily_calls=10**9) for n in providers}
        config = config.model_copy(update={"limits": {**limits, **config.limits}})
    if chaos is not None:
        providers = {n: ChaosProvider(p, chaos) for n, p in providers.items()}
    return Gateway(config, providers, on_event=on_event)
