from typing import Any

import httpx

from opspod.core.errors import LLMError, LLMRateLimited
from opspod.llm.providers.http import post_json
from opspod.llm.types import RawResponse

BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider:
    name = "openrouter"

    def __init__(self, api_key: str, client: httpx.AsyncClient, base_url: str = BASE_URL) -> None:
        self._key, self._client, self._base = api_key, client, base_url

    async def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict[str, object] | None,
        temperature: float,
        timeout: float,
    ) -> RawResponse:
        body: dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": schema, "strict": True},
            }
        data = await post_json(
            self._client,
            f"{self._base}/chat/completions",
            headers={"Authorization": f"Bearer {self._key}"},
            body=body,
            timeout=timeout,
        )
        if "error" in data:  # OpenRouter reports some upstream failures as 200 + error body
            err = data["error"]
            code = err.get("code") if isinstance(err, dict) else None
            if code == 429:
                raise LLMRateLimited()
            raise LLMError(f"openrouter error code={code}")
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("malformed openrouter response") from exc
        usage = data.get("usage") or {}
        return RawResponse(text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
