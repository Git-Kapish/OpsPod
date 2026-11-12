"""Shared HTTP error mapping for the REST providers (OpenRouter, Gemini)."""

from typing import Any

import httpx

from opspod.core.errors import LLMError, LLMRateLimited, LLMTimeout


def parse_retry_after(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    try:
        resp = await client.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise LLMTimeout(str(exc) or "timeout") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"transport error: {type(exc).__name__}") from exc
    if resp.status_code == 429:
        raise LLMRateLimited(retry_after=parse_retry_after(resp.headers.get("retry-after")))
    if resp.status_code >= 500:
        raise LLMError(f"provider {resp.status_code}")
    if resp.status_code >= 400:
        raise LLMError(f"provider rejected request: {resp.status_code}")
    data: dict[str, Any] = resp.json()
    return data
