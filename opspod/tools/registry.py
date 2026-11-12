"""Tool registry: every callable action declares scope, idempotency and reversibility.

The registry is the only door to side effects. It checks, in order: tool exists (unknown names
are counted as hallucinated), principal holds the scope, args validate against the schema,
and for money tools the per-case and per-day spend caps. Any failure raises; nothing executes.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from opspod.core.errors import ConfigError, OpsPodError, PermissionDenied
from opspod.domain.enums import LedgerKind, Role
from opspod.domain.money import Money
from opspod.ledger import service as ledger
from opspod.policy.models import RegionPolicy


class UnknownTool(OpsPodError):
    """Model or caller asked for a tool that is not registered (hallucinated tool call)."""


class InvalidToolArgs(OpsPodError):
    pass


class Principal(BaseModel):
    name: str
    role: Role
    scopes: frozenset[str]
    per_case_cap: int = Field(ge=0)  # max minor units per money call
    per_day_cap: int = Field(ge=0)  # max region net payouts per UTC day


def agent_principal(policy: RegionPolicy) -> Principal:
    """Service principal for the pipeline: capped at the auto-execute limit (defense in depth)."""
    return Principal(
        name="opspod-agent",
        role=Role.AGENT_SERVICE,
        scopes=frozenset({"case:read", "refund:write", "credit:write"}),
        per_case_cap=policy.limits.auto_limit,
        per_day_cap=policy.limits.per_day_cap,
    )


def approver_principal(policy: RegionPolicy, name: str) -> Principal:
    return Principal(
        name=name,
        role=Role.APPROVER,
        scopes=frozenset({"case:read", "refund:write", "credit:write", "refund:reverse"}),
        per_case_cap=policy.limits.per_case_cap,
        per_day_cap=policy.limits.per_day_cap,
    )


@dataclass
class ToolContext:
    session: AsyncSession
    region: str


class PayoutArgs(BaseModel):
    case_id: uuid.UUID
    version: int = Field(ge=1)  # part of the idempotency key
    account: str
    amount: int = Field(gt=0)
    currency: str


class ReverseArgs(BaseModel):
    action_id: uuid.UUID
    version: int = Field(default=1, ge=1)


Handler = Callable[[Any, ToolContext], Awaitable[Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    args_model: type[BaseModel]
    scope: str
    handler: Handler
    idempotent: bool
    reversible: bool
    money: bool = False  # amount-bearing: spend caps apply


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ConfigError(f"duplicate tool {spec.name}")
        if spec.money and not spec.idempotent:
            raise ConfigError(f"money tool {spec.name} must be idempotent")
        if spec.idempotent and "version" not in spec.args_model.model_fields:
            raise ConfigError(f"idempotent tool {spec.name} needs a version arg for its key")
        self._tools[spec.name] = spec

    def describe(self) -> list[dict[str, Any]]:
        """Machine-readable listing (also what an MCP server would expose)."""
        return [
            {
                "name": t.name,
                "scope": t.scope,
                "idempotent": t.idempotent,
                "reversible": t.reversible,
                "schema": t.args_model.model_json_schema(),
            }
            for t in self._tools.values()
        ]

    async def call(
        self, name: str, principal: Principal, raw_args: dict[str, Any], ctx: ToolContext
    ) -> Any:
        spec = self._tools.get(name)
        if spec is None:
            raise UnknownTool(name)
        if spec.scope not in principal.scopes:
            raise PermissionDenied(f"{principal.name} lacks scope {spec.scope} for {name}")
        try:
            args = spec.args_model.model_validate(raw_args)
        except ValidationError as exc:
            raise InvalidToolArgs(f"{name}: {exc.error_count()} invalid args") from exc
        if spec.money:
            await self._check_caps(args, principal, ctx)
        return await spec.handler(args, ctx)

    @staticmethod
    async def _check_caps(args: Any, principal: Principal, ctx: ToolContext) -> None:
        if args.amount > principal.per_case_cap:
            raise PermissionDenied(
                f"{principal.name}: {args.amount} exceeds per-case cap {principal.per_case_cap}"
            )
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        spent = await ledger.day_spend(ctx.session, region=ctx.region, since=start)
        if spent + args.amount > principal.per_day_cap:
            raise PermissionDenied(
                f"{principal.name}: per-day cap {principal.per_day_cap} exceeded"
            )


def _payout(kind: LedgerKind) -> Handler:
    async def handler(args: PayoutArgs, ctx: ToolContext) -> dict[str, Any]:
        res = await ledger.record_payout(
            ctx.session,
            case_id=args.case_id,
            kind=kind,
            version=args.version,
            account=args.account,
            amount=Money(args.amount, args.currency),
        )
        return {"action_id": str(res.action_id), "replayed": res.replayed}

    return handler


async def _reverse(args: ReverseArgs, ctx: ToolContext) -> dict[str, Any]:
    res = await ledger.reverse_action(ctx.session, args.action_id)
    return {"action_id": str(res.action_id), "replayed": res.replayed}


def default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            "issue_refund",
            PayoutArgs,
            "refund:write",
            _payout(LedgerKind.REFUND),
            idempotent=True,
            reversible=True,
            money=True,
        )
    )
    reg.register(
        ToolSpec(
            "issue_credit",
            PayoutArgs,
            "credit:write",
            _payout(LedgerKind.CREDIT),
            idempotent=True,
            reversible=True,
            money=True,
        )
    )
    reg.register(
        ToolSpec(
            "reverse_refund",
            ReverseArgs,
            "refund:reverse",
            _reverse,
            idempotent=True,
            reversible=False,
        )
    )
    return reg
