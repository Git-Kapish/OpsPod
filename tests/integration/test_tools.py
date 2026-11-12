import uuid

import pytest

from opspod.core.errors import ConfigError, PermissionDenied
from opspod.domain.enums import Role
from opspod.policy.loader import load_policy
from opspod.tools.registry import (
    InvalidToolArgs,
    PayoutArgs,
    Principal,
    ToolContext,
    ToolSpec,
    UnknownTool,
    agent_principal,
    approver_principal,
    default_registry,
)

from .conftest import make_case

pytestmark = pytest.mark.integration
POLICY = load_policy("us")
REG = default_registry()


def args(cid, amount=1000, version=1):
    return {
        "case_id": str(cid),
        "version": version,
        "account": "rider:9",
        "amount": amount,
        "currency": "USD",
    }


async def call(sm, name, principal, a):
    async with sm() as s, s.begin():
        return await REG.call(name, principal, a, ToolContext(s, "us"))


async def test_agent_can_refund_within_cap_and_is_idempotent(sessionmaker_):
    cid = await make_case(sessionmaker_)
    agent = agent_principal(POLICY)
    one = await call(sessionmaker_, "issue_refund", agent, args(cid))
    two = await call(sessionmaker_, "issue_refund", agent, args(cid))
    assert not one["replayed"] and two["replayed"]


async def test_agent_blocked_above_auto_limit_even_if_called_directly(sessionmaker_):
    cid = await make_case(sessionmaker_)
    with pytest.raises(PermissionDenied, match="per-case cap"):
        await call(
            sessionmaker_,
            "issue_refund",
            agent_principal(POLICY),
            args(cid, POLICY.limits.auto_limit + 1),
        )


async def test_approver_can_exceed_agent_cap_but_not_hard_cap(sessionmaker_):
    cid = await make_case(sessionmaker_)
    boss = approver_principal(POLICY, "alice")
    await call(sessionmaker_, "issue_refund", boss, args(cid, POLICY.limits.auto_limit + 1))
    with pytest.raises(PermissionDenied):
        await call(
            sessionmaker_, "issue_refund", boss, args(cid, POLICY.limits.per_case_cap + 1, 2)
        )


async def test_missing_scope_rejected(sessionmaker_):
    cid = await make_case(sessionmaker_)
    analyst = Principal(
        name="bob",
        role=Role.ANALYST,
        scopes=frozenset({"case:read"}),
        per_case_cap=10**9,
        per_day_cap=10**9,
    )
    with pytest.raises(PermissionDenied, match="lacks scope"):
        await call(sessionmaker_, "issue_refund", analyst, args(cid))
    with pytest.raises(PermissionDenied):  # agent has no reverse scope
        await call(
            sessionmaker_,
            "reverse_refund",
            agent_principal(POLICY),
            {"action_id": str(uuid.uuid4())},
        )


async def test_day_cap_enforced(sessionmaker_):
    cid1, cid2 = await make_case(sessionmaker_), await make_case(sessionmaker_)
    tight = agent_principal(POLICY).model_copy(update={"per_day_cap": 10**12})
    spent_before = None
    from datetime import UTC, datetime

    from opspod.ledger.service import day_spend

    async with sessionmaker_() as s:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        spent_before = await day_spend(s, region="us", since=start)
    tight = tight.model_copy(update={"per_day_cap": spent_before + 1500})
    await call(sessionmaker_, "issue_refund", tight, args(cid1, 1000))
    with pytest.raises(PermissionDenied, match="per-day cap"):
        await call(sessionmaker_, "issue_refund", tight, args(cid2, 1000))


async def test_unknown_tool_and_bad_args(sessionmaker_):
    cid = await make_case(sessionmaker_)
    agent = agent_principal(POLICY)
    with pytest.raises(UnknownTool):
        await call(sessionmaker_, "wire_money_offshore", agent, args(cid))
    with pytest.raises(InvalidToolArgs):
        await call(sessionmaker_, "issue_refund", agent, {**args(cid), "amount": "ten dollars"})
    with pytest.raises(InvalidToolArgs):
        await call(sessionmaker_, "issue_refund", agent, {**args(cid), "amount": -5})
    with pytest.raises(InvalidToolArgs):
        await call(
            sessionmaker_,
            "issue_refund",
            agent,
            {k: v for k, v in args(cid).items() if k != "version"},
        )


async def test_approver_reverses_agent_refund(sessionmaker_):
    cid = await make_case(sessionmaker_)
    res = await call(sessionmaker_, "issue_refund", agent_principal(POLICY), args(cid))
    out = await call(
        sessionmaker_,
        "reverse_refund",
        approver_principal(POLICY, "alice"),
        {"action_id": res["action_id"]},
    )
    assert not out["replayed"]


def test_registration_rules():
    reg = default_registry()
    with pytest.raises(ConfigError):
        reg.register(reg._tools["issue_refund"])
    bad = ToolSpec(
        "x", PayoutArgs, "s", lambda a, c: None, idempotent=False, reversible=False, money=True
    )  # type: ignore[arg-type,return-value]
    with pytest.raises(ConfigError):
        reg.register(bad)
    assert {t["name"] for t in reg.describe()} == {"issue_refund", "issue_credit", "reverse_refund"}
