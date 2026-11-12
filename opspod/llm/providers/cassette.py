"""Record/replay cassettes keyed by (model, prompt, schema, params): CI replays at zero cost."""

import hashlib
import json
from pathlib import Path

from opspod.core.errors import LLMError
from opspod.llm.types import Provider, RawResponse


class CassetteMiss(LLMError):
    """Replay mode saw a request that was never recorded. Re-record with `make eval-live`."""


def request_key(
    model: str, system: str, user: str, schema: dict[str, object] | None, temperature: float
) -> str:
    blob = json.dumps(
        {"m": model, "s": system, "u": user, "j": schema, "t": temperature}, sort_keys=True
    )
    return hashlib.sha256(blob.encode()).hexdigest()


class CassetteProvider:
    """replay (inner=None): serve from disk. record (inner set): call live, then write to disk."""

    def __init__(self, directory: Path, name: str, inner: Provider | None = None) -> None:
        self._dir, self.name, self._inner = directory, name, inner

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
        key = request_key(model, system, user, schema, temperature)
        path = self._dir / f"{key}.json"
        if self._inner is None:
            if not path.is_file():
                raise CassetteMiss(f"no cassette for model={model} key={key[:12]}")
            rec = json.loads(path.read_text(encoding="utf-8"))
            return RawResponse(rec["text"], rec["tokens_in"], rec["tokens_out"])
        resp = await self._inner.complete(
            model=model,
            system=system,
            user=user,
            schema=schema,
            temperature=temperature,
            timeout=timeout,
        )
        self._dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "provider": self.name,
                    "model": model,
                    "text": resp.text,
                    "tokens_in": resp.tokens_in,
                    "tokens_out": resp.tokens_out,
                },
                indent=1,
            ),
            encoding="utf-8",
            newline="\n",
        )
        return resp
