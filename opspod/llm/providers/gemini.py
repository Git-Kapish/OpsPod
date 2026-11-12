from typing import Any

import httpx

from opspod.core.errors import LLMError
from opspod.llm.providers.http import post_json
from opspod.llm.types import RawResponse

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiProvider:
    name = "gemini"

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
        gen: dict[str, Any] = {"temperature": temperature}
        if schema is not None:
            gen["responseMimeType"] = "application/json"
            gen["responseJsonSchema"] = schema
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": gen,
        }
        data = await post_json(
            self._client,
            f"{self._base}/models/{model}:generateContent",
            headers={"x-goog-api-key": self._key},
            body=body,
            timeout=timeout,
        )
        try:
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("malformed gemini response (blocked or empty)") from exc
        usage = data.get("usageMetadata") or {}
        return RawResponse(
            text, usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0)
        )
