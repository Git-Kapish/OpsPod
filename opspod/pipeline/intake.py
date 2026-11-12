"""Idempotent case intake: the Idempotency-Key is the case's external_id (UNIQUE)."""

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from opspod.db.audit import write_audit
from opspod.db.models import Case
from opspod.pipeline.state import CaseIn


async def create_case(
    session: AsyncSession, payload: CaseIn, idempotency_key: str
) -> tuple[uuid.UUID, bool]:
    """Returns (case_id, created). A duplicate key returns the existing case, creating nothing."""
    stmt = (
        insert(Case)
        .values(
            id=uuid.uuid4(),
            external_id=idempotency_key,
            type=payload.case_type.value if payload.case_type else "unclassified",
            region=payload.region,
            status="received",
            payload_json=payload.model_dump(mode="json"),
            redacted_json={},
        )
        .on_conflict_do_nothing(index_elements=[Case.external_id])
        .returning(Case.id)
    )
    new_id = (await session.execute(stmt)).scalar_one_or_none()
    if new_id is not None:
        await write_audit(
            session,
            case_id=new_id,
            event="case.received",
            actor="intake",
            payload={"idempotency_key": idempotency_key},
        )
        return new_id, True
    existing = (
        await session.execute(select(Case.id).where(Case.external_id == idempotency_key))
    ).scalar_one()
    return existing, False
