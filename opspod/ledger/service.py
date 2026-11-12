"""Append-only ledger: idempotent refunds/credits and compensating reversals.

Only this module writes money rows. Functions run in the caller's transaction and never
commit, so an action row and its ledger entry are always written (or rolled back) together.
Idempotency key = (case_id, action_type, version), enforced by a UNIQUE constraint, so
retries and duplicate webhooks can never pay twice, even under concurrency.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from opspod.core.errors import LedgerError
from opspod.db.models import Action, Case, LedgerEntry
from opspod.domain.enums import LedgerKind
from opspod.domain.money import Money

PAYOUT_KINDS = (LedgerKind.REFUND, LedgerKind.CREDIT)


@dataclass(frozen=True)
class ActionResult:
    action_id: uuid.UUID
    amount: Money
    replayed: bool  # True when the idempotency key already existed (no new money moved)


def idempotency_key(case_id: uuid.UUID, action_type: str, version: int) -> str:
    return f"{case_id}:{action_type}:{version}"


async def _insert_action(
    session: AsyncSession,
    case_id: uuid.UUID,
    action_type: str,
    key: str,
    args: dict[str, Any],
) -> uuid.UUID | None:
    stmt = (
        insert(Action)
        .values(
            id=uuid.uuid4(),
            case_id=case_id,
            type=action_type,
            idempotency_key=key,
            args_json=args,
            status="executed",
        )
        .on_conflict_do_nothing(index_elements=[Action.idempotency_key])
        .returning(Action.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _existing(session: AsyncSession, key: str, args: dict[str, Any]) -> ActionResult:
    action = (
        await session.execute(select(Action).where(Action.idempotency_key == key))
    ).scalar_one()
    if (action.args_json.get("amount"), action.args_json.get("currency")) != (
        args["amount"],
        args["currency"],
    ) or action.args_json.get("account") != args["account"]:
        raise LedgerError(f"idempotency key {key} reused with different payload")
    return ActionResult(action.id, Money(args["amount"], args["currency"]), replayed=True)


async def record_payout(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    kind: LedgerKind,
    version: int,
    account: str,
    amount: Money,
) -> ActionResult:
    """Pay a refund or credit once per (case, kind, version)."""
    if kind not in PAYOUT_KINDS:
        raise LedgerError(f"record_payout only handles refund/credit, got {kind}")
    if amount.minor <= 0:
        raise LedgerError("payout amount must be positive")
    key = idempotency_key(case_id, kind.value, version)
    args = {"account": account, "amount": amount.minor, "currency": amount.currency}
    action_id = await _insert_action(session, case_id, kind.value, key, args)
    if action_id is None:
        return await _existing(session, key, args)
    session.add(
        LedgerEntry(
            account=account,
            amount=amount.minor,
            currency=amount.currency,
            action_id=action_id,
            kind=kind.value,
        )
    )
    await session.flush()
    return ActionResult(action_id, amount, replayed=False)


async def reverse_action(session: AsyncSession, action_id: uuid.UUID) -> ActionResult:
    """Compensate an executed refund/credit with a linked reversal entry. Idempotent."""
    original = (
        await session.execute(select(Action).where(Action.id == action_id).with_for_update())
    ).scalar_one_or_none()
    if original is None:
        raise LedgerError(f"unknown action {action_id}")
    if original.type not in {k.value for k in PAYOUT_KINDS}:
        raise LedgerError(f"cannot reverse action of type {original.type}")
    entry = (
        await session.execute(select(LedgerEntry).where(LedgerEntry.action_id == action_id))
    ).scalar_one()
    amount = Money(entry.amount, entry.currency)
    key = f"{original.case_id}:reversal:{action_id}"
    args = {
        "account": entry.account,
        "amount": entry.amount,
        "currency": entry.currency,
        "reverses": str(action_id),
    }
    new_id = await _insert_action(session, original.case_id, LedgerKind.REVERSAL.value, key, args)
    if new_id is None:
        existing = (
            await session.execute(select(Action).where(Action.idempotency_key == key))
        ).scalar_one()
        return ActionResult(existing.id, amount, replayed=True)
    session.add(
        LedgerEntry(
            account=entry.account,
            amount=entry.amount,
            currency=entry.currency,
            action_id=new_id,
            kind=LedgerKind.REVERSAL.value,
        )
    )
    original.reversed_by_action_id = new_id
    await session.flush()
    return ActionResult(new_id, amount, replayed=False)


async def net_paid(
    session: AsyncSession, *, case_id: uuid.UUID | None = None, account: str | None = None
) -> int:
    """Net money paid out (refunds + credits - reversals), in minor units."""
    paid = await _sum(session, [k.value for k in PAYOUT_KINDS], case_id, account)
    reversed_ = await _sum(session, [LedgerKind.REVERSAL.value], case_id, account)
    return paid - reversed_


async def _sum(
    session: AsyncSession, kinds: list[str], case_id: uuid.UUID | None, account: str | None
) -> int:
    stmt = select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(LedgerEntry.kind.in_(kinds))
    if case_id is not None:
        stmt = stmt.join(Action, Action.id == LedgerEntry.action_id).where(
            Action.case_id == case_id
        )
    if account is not None:
        stmt = stmt.where(LedgerEntry.account == account)
    return int((await session.execute(stmt)).scalar_one())


async def day_spend(session: AsyncSession, *, region: str, since: datetime) -> int:
    """Net payouts for a region since `since` (typically start of the UTC day)."""
    total = 0
    for kinds, sign in (([k.value for k in PAYOUT_KINDS], 1), ([LedgerKind.REVERSAL.value], -1)):
        stmt = (
            select(func.coalesce(func.sum(LedgerEntry.amount), 0))
            .join(Action, Action.id == LedgerEntry.action_id)
            .join(Case, Case.id == Action.case_id)
            .where(
                Case.region == region, LedgerEntry.created_at >= since, LedgerEntry.kind.in_(kinds)
            )
        )
        total += sign * int((await session.execute(stmt)).scalar_one())
    return total


async def payout_count(session: AsyncSession, *, account: str, since: datetime) -> int:
    """How many refunds/credits an account received since `since` (frequency cap input)."""
    stmt = (
        select(func.count())
        .select_from(LedgerEntry)
        .where(
            LedgerEntry.account == account,
            LedgerEntry.created_at >= since,
            LedgerEntry.kind.in_([k.value for k in PAYOUT_KINDS]),
        )
    )
    return int((await session.execute(stmt)).scalar_one())
