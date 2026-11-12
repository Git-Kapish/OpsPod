import asyncio

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from opspod.core.errors import LedgerError
from opspod.domain.enums import LedgerKind
from opspod.domain.money import Money
from opspod.ledger.service import net_paid, record_payout, reverse_action

from .conftest import TEST_URL, make_case

pytestmark = pytest.mark.integration
ACCT = "rider:1"


async def pay(sm, case_id, amount=1000, version=1, kind=LedgerKind.REFUND):
    async with sm() as s, s.begin():
        return await record_payout(
            s,
            case_id=case_id,
            kind=kind,
            version=version,
            account=ACCT,
            amount=Money(amount, "USD"),
        )


async def test_payout_is_idempotent(sessionmaker_):
    cid = await make_case(sessionmaker_)
    first, second = await pay(sessionmaker_, cid), await pay(sessionmaker_, cid)
    assert not first.replayed and second.replayed and first.action_id == second.action_id
    async with sessionmaker_() as s:
        assert await net_paid(s, case_id=cid) == 1000


async def test_same_key_different_amount_rejected(sessionmaker_):
    cid = await make_case(sessionmaker_)
    await pay(sessionmaker_, cid, 1000)
    with pytest.raises(LedgerError):
        await pay(sessionmaker_, cid, 999)


async def test_concurrent_duplicates_pay_once(sessionmaker_):
    cid = await make_case(sessionmaker_)
    results = await asyncio.gather(*[pay(sessionmaker_, cid) for _ in range(10)])
    assert sum(not r.replayed for r in results) == 1
    async with sessionmaker_() as s:
        assert await net_paid(s, case_id=cid) == 1000


async def test_reversal_compensates_and_is_idempotent(sessionmaker_):
    cid = await make_case(sessionmaker_)
    res = await pay(sessionmaker_, cid)
    async with sessionmaker_() as s, s.begin():
        rev = await reverse_action(s, res.action_id)
    async with sessionmaker_() as s, s.begin():
        again = await reverse_action(s, res.action_id)
    assert not rev.replayed and again.replayed and rev.action_id == again.action_id
    async with sessionmaker_() as s:
        assert await net_paid(s, case_id=cid) == 0


async def test_cannot_reverse_a_reversal_or_unknown(sessionmaker_):
    import uuid

    cid = await make_case(sessionmaker_)
    res = await pay(sessionmaker_, cid)
    async with sessionmaker_() as s, s.begin():
        rev = await reverse_action(s, res.action_id)
    for bad in (rev.action_id, uuid.uuid4()):
        with pytest.raises(LedgerError):
            async with sessionmaker_() as s, s.begin():
                await reverse_action(s, bad)


async def test_rejects_bad_payouts(sessionmaker_):
    cid = await make_case(sessionmaker_)
    with pytest.raises(LedgerError):
        await pay(sessionmaker_, cid, kind=LedgerKind.REVERSAL)
    with pytest.raises(LedgerError):
        await pay(sessionmaker_, cid, amount=0)


@pytest.mark.parametrize("table", ["ledger_entries", "audit_log"])
@pytest.mark.parametrize("stmt", ["update {t} set {c} = {c}", "delete from {t}", "truncate {t}"])
async def test_append_only(sessionmaker_, table, stmt):
    cid = await make_case(sessionmaker_)
    if table == "ledger_entries":
        await pay(sessionmaker_, cid)
        col = "amount"
    else:
        async with sessionmaker_() as s, s.begin():
            await s.execute(
                text(
                    "insert into audit_log (event, actor, payload_json) "
                    "values ('t','test','{}'::jsonb)"
                )
            )
        col = "event"
    with pytest.raises(DBAPIError, match="append-only"):
        async with sessionmaker_() as s, s.begin():
            await s.execute(text(stmt.format(t=table, c=col)))


op_strategy = st.lists(
    st.tuples(
        st.sampled_from(["pay", "reverse"]),
        st.integers(1, 3),  # version, small range to force key collisions
        st.integers(1, 5000),
    ),
    max_size=12,
)


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(ops=op_strategy)
def test_ledger_invariants_property(ops):
    """No double pay; net paid always equals model; never negative; one entry per unique key."""

    async def run() -> None:
        engine = create_async_engine(TEST_URL, poolclass=NullPool)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        cid = await make_case(sm)
        paid: dict[int, tuple[int, object]] = {}  # version -> (amount, action_id)
        reversed_versions: set[int] = set()
        for op, version, amount in ops:
            if op == "pay":
                try:
                    res = await pay(sm, cid, amount, version)
                except LedgerError:
                    assert version in paid and paid[version][0] != amount
                    continue
                if version in paid:
                    assert res.replayed
                else:
                    assert not res.replayed
                    paid[version] = (amount, res.action_id)
            elif version in paid:
                async with sm() as s, s.begin():
                    await reverse_action(s, paid[version][1])  # type: ignore[arg-type]
                reversed_versions.add(version)
        async with sm() as s:
            got = await net_paid(s, case_id=cid)
            n = (
                await s.execute(
                    text(
                        "select count(*) from ledger_entries l join actions a on a.id=l.action_id "
                        "where a.case_id=:c"
                    ),
                    {"c": cid},
                )
            ).scalar_one()
        expected = sum(a for v, (a, _) in paid.items() if v not in reversed_versions)
        assert got == expected >= 0
        assert n == len(paid) + len(reversed_versions)
        await engine.dispose()

    asyncio.run(run())
