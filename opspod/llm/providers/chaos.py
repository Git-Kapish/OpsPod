from opspod.core.chaos import ChaosSource
from opspod.core.errors import LLMRateLimited, LLMTimeout
from opspod.llm.types import Provider, RawResponse


class ChaosProvider:
    """Wraps a provider and injects failures while the matching chaos flag is on."""

    def __init__(self, inner: Provider, chaos: ChaosSource) -> None:
        self._inner, self._chaos = inner, chaos
        self.name = inner.name

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
        c = await self._chaos.get()
        if c.llm_provider not in (None, self.name):
            return await self._inner.complete(
                model=model,
                system=system,
                user=user,
                schema=schema,
                temperature=temperature,
                timeout=timeout,
            )
        if c.llm_timeout:
            raise LLMTimeout("chaos: injected timeout")
        if c.llm_429:
            raise LLMRateLimited("chaos: injected 429", retry_after=1.0)
        if c.llm_malformed:
            return RawResponse("<<chaos: not json>>", 5, 5)
        return await self._inner.complete(
            model=model,
            system=system,
            user=user,
            schema=schema,
            temperature=temperature,
            timeout=timeout,
        )
