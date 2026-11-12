from opspod.core.config import LLMMode, Settings


def test_defaults_are_safe(monkeypatch):
    monkeypatch.delenv("LLM_MODE", raising=False)
    assert Settings(_env_file=None).llm_mode is LLMMode.FAKE
