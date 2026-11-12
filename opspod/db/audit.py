"""Append-only audit writer (S6). Callers pass the session so audit commits with the action."""

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from opspod.db.models import AuditLog


def snapshot_hash(obj: Any) -> str:
    """Stable hash of an input snapshot, so replays can prove they saw the same input."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


async def write_audit(
    session: AsyncSession,
    *,
    case_id: uuid.UUID | None,
    event: str,
    actor: str,
    payload: dict[str, Any],
    prompt_version: str | None = None,
    model: str | None = None,
) -> None:
    session.add(
        AuditLog(
            case_id=case_id,
            event=event,
            actor=actor,
            payload_json=payload,
            trace_id=case_id.hex if case_id else None,  # case id == trace correlation id
            prompt_version=prompt_version,
            model=model,
        )
    )
    await session.flush()
