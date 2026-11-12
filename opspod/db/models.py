"""SQLAlchemy models (PRD section 14). ledger_entries and audit_log are append-only."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Case(Base):
    __tablename__ = "cases"
    id: Mapped[uuid.UUID] = _uuid_pk()
    external_id: Mapped[str] = mapped_column(String, unique=True)
    type: Mapped[str] = mapped_column(String)
    region: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="received")
    created_at: Mapped[datetime] = _now()
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    redacted_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class CaseStep(Base):
    __tablename__ = "case_steps"
    __table_args__ = (UniqueConstraint("case_id", "step", "attempt"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), index=True)
    step: Mapped[str] = mapped_column(String)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String)
    attempt: Mapped[int] = mapped_column(Integer, default=1)


class Decision(Base):
    __tablename__ = "decisions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), index=True)
    source: Mapped[str] = mapped_column(String)  # rule | llm | human
    outcome: Mapped[str] = mapped_column(String)
    amount: Mapped[int] = mapped_column(BigInteger, default=0)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    policy_rules_fired: Mapped[list[str]] = mapped_column(JSONB, default=list)
    route: Mapped[str] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[uuid.UUID] = _uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), index=True)
    decision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("decisions.id"))
    status: Mapped[str] = mapped_column(String, default="pending")
    requested_at: Mapped[datetime] = _now()
    decided_by: Mapped[str | None] = mapped_column(String, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    edited_amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Action(Base):
    __tablename__ = "actions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), index=True)
    type: Mapped[str] = mapped_column(String)
    idempotency_key: Mapped[str] = mapped_column(String, unique=True)
    args_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String, default="executed")
    executed_at: Mapped[datetime] = _now()
    reversed_by_action_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("actions.id"), nullable=True
    )


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ledger_amount_positive"),
        CheckConstraint("kind in ('refund','credit','reversal')", name="ledger_kind_valid"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    account: Mapped[str] = mapped_column(String, index=True)
    amount: Mapped[int] = mapped_column(BigInteger)  # minor units, always positive
    currency: Mapped[str] = mapped_column(String(3))
    action_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("actions.id"), index=True)
    kind: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = _now()


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    case_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    event: Mapped[str] = mapped_column(String)
    actor: Mapped[str] = mapped_column(String)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = _now()


class Flag(Base):
    __tablename__ = "flags"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = _now()
    updated_by: Mapped[str] = mapped_column(String, default="system")


class DeadLetter(Base):
    __tablename__ = "dead_letters"
    id: Mapped[uuid.UUID] = _uuid_pk()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), index=True)
    step: Mapped[str] = mapped_column(String)
    error: Mapped[str] = mapped_column(Text)  # exception type + short message, never raw PII
    attempts: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = _now()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
