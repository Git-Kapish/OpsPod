"""Each chaos flag has a test and a documented expected behavior (docs/runbook.md).

Common contract: an injected failure may slow or escalate cases, but never pays wrongly, never
loses a case, never pays twice, and clears cleanly once the flag is switched off.
"""

import asyncio
import time
import uuid

import httpx
import pytest
from sqlalchemy import select

from opspod.api.app import create_app
from opspod.core.chaos import Chaos, ChaosError, StaticChaos
from opspod.db.models import AuditLog, Case, CaseStep, DeadLetter
from opspod.domain.enums import AutonomyMode
from opspod.evals.stubs import good
from opspod.llm.config import Candidate, LLMConfig, ProviderLimits
from opspod.llm.gateway import Gateway
from opspod.llm.providers.chaos import ChaosProvider
from opspod.llm.providers.fake import FakeProvider
from opspod.pipeline.runner import CaseLocked, run_case
from opspod.pipeline.worker import MAX_TRIES, process_case
from opspod.tools.registry import default_registry
from tests.integration.test_pipeline import BY_TAG, make_deps, paid, set_mode, submit

pytestmark = pytest.mark.integration
ADMIN = {"X-API-Key": "dev-admin", "X-Role": "admin"}
AGENT = {"X-API-Key": "dev-agent"}


async def no_sleep(_: float) -> None:
    return None


def chaos_deps(sm, chaos: StaticChaos, breaker_threshold: int = 100):
    deps, fake = make_deps(sm)
    cfg = LLMConfig(
        tasks={
            t: [Candidate(provider="fake", model="m")]
            for t in ("classify", "analyze", "draft_message")
        },
        breaker_threshold=breaker_threshold,
        backoff_base_s=0.0,
        limits={"fake": ProviderLimits(requests_per_minute=6000)},
    )
    inner = FakeProvider(good)
    deps.gateway = Gateway(cfg, {"fake": ChaosProvider(inner, chaos)}, sleep=no_sleep)
    deps.tools = default_registry(chaos)
    deps.chaos = chaos
    return deps, inner


def auto_case():
    return next(
        c
        for c in BY_TAG["clear_cut"]
        if c["expected"]["route"] == "auto" and c["expected"]["amount"] > 0
    )


def ambiguous_case():
    return next(c for c in BY_TAG["ambiguous"] if c["expected"]["outcome"] == "PARTIAL_REFUND")


@pytest.mark.parametrize("flag", ["llm_timeout", "llm_429", "llm_malformed"])
async def test_llm_failures_escalate_ambiguous_cases_and_spare_clear_cut_ones(sessionmaker_, flag):
    """Expected: ambiguous cases go to a human (llm_unavailable); rule-decided cases are unaffected."""
    await set_mode(sessionmaker_, AutonomyMode.AUTO_LOW_RISK)
    chaos = StaticChaos(Chaos(**{flag: True}))
    deps, inner = chaos_deps(sessionmaker_, chaos)
    amb = await submit(sessionmaker_, ambiguous_case())
    state = await run_case(deps, amb)
    assert state.path == "llm_unavailable" and state.final_status == "escalated"
    assert await paid(sessionmaker_, amb) == 0 and inner.calls == []
    clear = auto_case()
    cid = await submit(sessionmaker_, clear)
    ok = await run_case(deps, cid)
    assert (
        ok.final_status == "resolved"
        and await paid(sessionmaker_, cid) == clear["expected"]["amount"]
    )
    chaos.state = Chaos()  # recovery: the next ambiguous case is handled normally
    cid2 = await submit(sessionmaker_, ambiguous_case())
    assert (await run_case(deps, cid2)).final_status == "awaiting_approval"


async def test_provider_failures_open_breaker_and_stop_hammering(sessionmaker_):
    """Expected: after N failures the breaker opens and later cases fail fast without provider calls."""
    await set_mode(sessionmaker_, AutonomyMode.AUTO_LOW_RISK)
    chaos = StaticChaos(Chaos(llm_timeout=True))
    deps, _ = chaos_deps(sessionmaker_, chaos, breaker_threshold=3)
    events = []
    deps.gateway._on_event = events.append
    for _ in range(3):
        await run_case(deps, await submit(sessionmaker_, ambiguous_case()))
    assert any(e.outcome == "breaker_open" for e in events)
    timeouts = sum(e.outcome == "timeout" for e in events)
    await run_case(deps, await submit(sessionmaker_, ambiguous_case()))
    assert sum(e.outcome == "timeout" for e in events) == timeouts  # no new provider attempts


async def test_tool_500_never_pays_then_recovers_without_double_pay(sessionmaker_):
    """Expected: execute step fails and retries; nothing paid meanwhile; after recovery paid once."""
    await set_mode(sessionmaker_, AutonomyMode.AUTO_LOW_RISK)
    chaos = StaticChaos(Chaos(tool_500=True))
    deps, _ = chaos_deps(sessionmaker_, chaos)
    case = auto_case()
    cid = await submit(sessionmaker_, case)
    for _ in range(2):
        with pytest.raises(ChaosError):
            await run_case(deps, cid)
    assert await paid(sessionmaker_, cid) == 0
    async with sessionmaker_() as s:
        failed = (
            (
                await s.execute(
                    select(CaseStep).where(CaseStep.case_id == cid, CaseStep.status == "failed")
                )
            )
            .scalars()
            .all()
        )
    assert len(failed) == 2 and all(f.step == "execute" for f in failed)
    chaos.state = Chaos()
    state = await run_case(deps, cid)  # resumes at execute, earlier steps not repeated
    assert (
        state.final_status == "resolved"
        and await paid(sessionmaker_, cid) == case["expected"]["amount"]
    )


async def test_tool_500_exhausts_retries_into_dlq_not_into_a_payment(sessionmaker_):
    """Expected: after MAX_TRIES the case is dead-lettered for a human; no money moved."""
    await set_mode(sessionmaker_, AutonomyMode.AUTO_LOW_RISK)
    chaos = StaticChaos(Chaos(tool_500=True))
    deps, _ = chaos_deps(sessionmaker_, chaos)
    cid = await submit(sessionmaker_, auto_case())
    from arq import Retry

    with pytest.raises(Retry):
        await process_case({"deps": deps, "job_try": 1}, str(cid))
    assert await process_case({"deps": deps, "job_try": MAX_TRIES}, str(cid)) == "dead_letter"
    assert await paid(sessionmaker_, cid) == 0
    async with sessionmaker_() as s:
        assert (await s.get(Case, cid)).status == "dead_letter"
        assert (await s.execute(select(DeadLetter).where(DeadLetter.case_id == cid))).scalar_one()


async def test_slow_db_slows_cases_but_they_still_complete(sessionmaker_):
    """Expected: latency rises ~slow_db_ms per step (SLO alert); correctness unchanged."""
    await set_mode(sessionmaker_, AutonomyMode.AUTO_LOW_RISK)
    chaos = StaticChaos(Chaos(slow_db_ms=40))
    deps, _ = chaos_deps(sessionmaker_, chaos)
    case = auto_case()
    cid = await submit(sessionmaker_, case)
    t0 = time.perf_counter()
    state = await run_case(deps, cid)
    assert time.perf_counter() - t0 >= 8 * 0.04 * 0.9
    assert (
        state.final_status == "resolved"
        and await paid(sessionmaker_, cid) == case["expected"]["amount"]
    )


async def test_duplicate_webhook_delivers_thrice_pays_once(sessionmaker_):
    """Expected: one case, one action, however many times the webhook/queue delivers it."""
    await set_mode(sessionmaker_, AutonomyMode.AUTO_LOW_RISK)
    chaos = StaticChaos(Chaos(duplicate_webhook=True))
    deps, _ = chaos_deps(sessionmaker_, chaos)
    queued: list[str] = []

    async def enqueue(cid: str) -> None:
        queued.append(cid)

    app = create_app(deps, enqueue, app_env="dev")
    case = auto_case()
    body = {
        "external_id": case["input"]["external_id"],
        "case_type": case["case_type"],
        "region": case["region"],
        "rider_ref": f"r-{uuid.uuid4().hex[:6]}",
        "complaint_text": case["input"]["complaint_text"],
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/v1/cases", json=body, headers={**AGENT, "Idempotency-Key": f"k-{uuid.uuid4()}"}
        )
    assert r.status_code == 202 and len(queued) == 3 and len(set(queued)) == 1
    cid = uuid.UUID(queued[0])
    outcomes = await asyncio.gather(
        *[run_case(deps, cid) for _ in queued], return_exceptions=True
    )  # racing workers, same case
    assert any(not isinstance(o, Exception) for o in outcomes)  # one worker wins
    assert all(isinstance(o, CaseLocked) for o in outcomes if isinstance(o, Exception))
    await run_case(deps, cid)  # a later redelivery is a harmless no-op resume
    assert await paid(sessionmaker_, cid) == case["expected"]["amount"]


async def test_poisoned_text_is_escalated_never_paid(sessionmaker_):
    """Expected: injected instructions in complaint text force escalation; no money, no LLM call."""
    await set_mode(sessionmaker_, AutonomyMode.AUTO_LOW_RISK)
    chaos = StaticChaos(Chaos(poisoned_text=True))
    deps, inner = chaos_deps(sessionmaker_, chaos)

    async def enqueue(_: str) -> None:
        return None

    app = create_app(deps, enqueue, app_env="dev")
    case = auto_case()
    body = {
        "external_id": case["input"]["external_id"],
        "case_type": case["case_type"],
        "region": case["region"],
        "rider_ref": f"r-{uuid.uuid4().hex[:6]}",
        "complaint_text": case["input"]["complaint_text"],
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/v1/cases", json=body, headers={**AGENT, "Idempotency-Key": f"k-{uuid.uuid4()}"}
        )
    cid = uuid.UUID(r.json()["case_id"])
    state = await run_case(deps, cid)
    assert (
        state.final_status == "escalated"
        and "P009_injection_detected" in state.decision.rules_fired
    )
    assert await paid(sessionmaker_, cid) == 0 and inner.calls == []


async def test_chaos_admin_endpoint_is_dev_only_and_admin_only(sessionmaker_):
    deps, _ = make_deps(sessionmaker_)

    async def enqueue(_: str) -> None:
        return None

    flags = {"llm_429": True, "slow_db_ms": 5}
    for env, expect in (("dev", 200), ("prod", 404)):
        app = create_app(deps, enqueue, app_env=env)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            assert (
                await c.post("/v1/admin/chaos", json=flags, headers=ADMIN)
            ).status_code == expect
            assert (
                await c.post("/v1/admin/chaos", json=flags, headers={"X-API-Key": "dev-analyst"})
            ).status_code == 403
            assert (
                await c.post("/v1/admin/chaos", json={"bogus": True}, headers=ADMIN)
            ).status_code in (404, 422)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(deps, enqueue, "dev")), base_url="http://t"
    ) as c:
        assert (await c.get("/v1/admin/chaos", headers=ADMIN)).json()["llm_429"] is True
        await c.post("/v1/admin/chaos", json={}, headers=ADMIN)  # reset all flags
        assert (await c.get("/v1/admin/chaos", headers=ADMIN)).json()["llm_429"] is False
    async with sessionmaker_() as s:
        events = (
            (await s.execute(select(AuditLog.event).where(AuditLog.event == "chaos.changed")))
            .scalars()
            .all()
        )
    assert events  # every toggle is audited
