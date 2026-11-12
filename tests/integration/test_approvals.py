import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import select

from opspod.api.app import create_app
from opspod.db.models import Action, Approval, Case
from opspod.domain.enums import AutonomyMode
from opspod.pipeline.replay import replay_case
from opspod.pipeline.runner import run_case

from .test_pipeline import BY_TAG, analysis, make_deps, paid, set_mode, submit

pytestmark = pytest.mark.integration
APPROVER = {"X-API-Key": "dev-approver", "X-Role": "approver"}
ANALYST = {"X-API-Key": "dev-analyst"}
ADMIN = {"X-API-Key": "dev-admin", "X-Role": "admin"}
AGENT = {"X-API-Key": "dev-agent"}


@pytest.fixture
async def env(sessionmaker_):
    deps, fake = make_deps(sessionmaker_)

    async def enqueue(_: str) -> None:
        return None

    app = create_app(deps, enqueue)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c, deps, sessionmaker_


def money_case():
    return next(
        c
        for c in BY_TAG["clear_cut"]
        if c["expected"]["route"] == "auto" and c["expected"]["amount"] > 100
    )


async def pending_approval(env, mode=AutonomyMode.ASSISTED):
    c, deps, sm = env
    await set_mode(sm, mode)
    case = money_case()
    cid = await submit(sm, case)
    await run_case(deps, cid)
    async with sm() as s:
        appr = (await s.execute(select(Approval).where(Approval.case_id == cid))).scalar_one()
    return cid, appr.id, case["expected"]["amount"]


async def test_approve_pays_once_and_records_who(env):
    c, deps, sm = env
    cid, aid, amount = await pending_approval(env)
    listing = await c.get("/v1/approvals?status=pending", headers=ANALYST)
    assert str(aid) in [r["id"] for r in listing.json()]
    r = await c.post(
        f"/v1/approvals/{aid}/decision",
        json={"decision": "approve", "note": "ok"},
        headers=APPROVER,
    )
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert await paid(sm, cid) == amount
    async with sm() as s:
        appr = await s.get(Approval, aid)
        assert appr.decided_by == "approver:dev-approver" and appr.decided_at and appr.note == "ok"
        assert (await s.get(Case, cid)).status == "resolved"
    assert deps.extras["outbox"]
    again = await c.post(
        f"/v1/approvals/{aid}/decision", json={"decision": "approve"}, headers=APPROVER
    )
    assert again.status_code == 409 and await paid(sm, cid) == amount


async def test_only_approvers_may_decide(env):
    c, _, sm = env
    cid, aid, _ = await pending_approval(env)
    for h in (ANALYST, AGENT, {}):
        r = await c.post(f"/v1/approvals/{aid}/decision", json={"decision": "approve"}, headers=h)
        assert r.status_code in (401, 403)
    assert await paid(sm, cid) == 0


async def test_edit_amount_can_only_reduce(env):
    c, _, sm = env
    cid, aid, amount = await pending_approval(env)
    for bad in (amount + 1, 0, -5):
        r = await c.post(
            f"/v1/approvals/{aid}/decision",
            json={"decision": "approve", "edited_amount": bad},
            headers=APPROVER,
        )
        assert r.status_code == 422
    assert await paid(sm, cid) == 0  # failed attempts left the approval pending
    r = await c.post(
        f"/v1/approvals/{aid}/decision",
        json={"decision": "approve", "edited_amount": amount - 1},
        headers=APPROVER,
    )
    assert r.json()["status"] == "edited" and await paid(sm, cid) == amount - 1


async def test_reject_moves_no_money(env):
    c, _, sm = env
    cid, aid, _ = await pending_approval(env)
    r = await c.post(
        f"/v1/approvals/{aid}/decision", json={"decision": "reject", "note": "no"}, headers=APPROVER
    )
    assert r.json()["status"] == "rejected" and await paid(sm, cid) == 0
    async with sm() as s:
        assert (await s.get(Case, cid)).status == "rejected"
    assert (
        await c.post(
            f"/v1/approvals/{aid}/decision", json={"decision": "approve"}, headers=APPROVER
        )
    ).status_code == 409


async def test_concurrent_approvals_pay_exactly_once(env):
    c, _, sm = env
    cid, aid, amount = await pending_approval(env)
    rs = await asyncio.gather(
        *[
            c.post(f"/v1/approvals/{aid}/decision", json={"decision": "approve"}, headers=APPROVER)
            for _ in range(6)
        ]
    )
    assert sorted(r.status_code for r in rs) == [200] + [409] * 5
    assert await paid(sm, cid) == amount


async def test_unknown_approval_404(env):
    c, *_ = env
    r = await c.post(
        f"/v1/approvals/{uuid.uuid4()}/decision", json={"decision": "approve"}, headers=APPROVER
    )
    assert r.status_code == 404


async def test_reverse_refund_is_compensating_and_idempotent(env):
    c, deps, sm = env
    await set_mode(sm, AutonomyMode.AUTO_LOW_RISK)
    cid = await submit(sm, money_case())
    await run_case(deps, cid)
    async with sm() as s:
        action = (
            await s.execute(select(Action).where(Action.case_id == cid, Action.type == "refund"))
        ).scalar_one()
    assert (await c.post(f"/v1/actions/{action.id}/reverse", headers=ANALYST)).status_code == 403
    r = await c.post(f"/v1/actions/{action.id}/reverse", headers=APPROVER)
    assert r.status_code == 200 and not r.json()["replayed"] and await paid(sm, cid) == 0
    again = await c.post(f"/v1/actions/{action.id}/reverse", headers=APPROVER)
    assert again.json()["replayed"] and await paid(sm, cid) == 0
    async with sm() as s:
        assert (await s.get(Case, cid)).status == "reversed"
        refund = await s.get(Action, action.id)
        assert refund.reversed_by_action_id is not None  # linked compensating entry
    assert (
        await c.post(f"/v1/actions/{uuid.uuid4()}/reverse", headers=APPROVER)
    ).status_code == 404


async def test_replay_reproduces_decisions_from_audit_only(env):
    c, deps, sm = env
    await set_mode(sm, AutonomyMode.AUTO_LOW_RISK)
    cases = [money_case(), BY_TAG["ambiguous"][0], BY_TAG["injection"][0], BY_TAG["fraud"][0]]
    for case in cases:
        cid = await submit(sm, case)
        await run_case(deps, cid)
        r = await c.get(f"/v1/cases/{cid}/replay", headers=ANALYST)
        assert r.status_code == 200 and r.json()["identical"], (case["tags"], r.json()["diffs"])
    # tampering with policy after the fact is detected
    cid = await submit(sm, money_case())
    await run_case(deps, cid)
    deps.policies["us"] = deps.policies["us"].model_copy(update={"version": 2})
    out = await replay_case(deps, cid)
    assert not out["identical"] and any("policy version" in d for d in out["diffs"])
    assert (await c.get(f"/v1/cases/{uuid.uuid4()}/replay", headers=ANALYST)).status_code == 404


async def test_admin_flags_and_kill_switch(env):
    c, deps, sm = env
    assert (await c.get("/v1/admin/flags", headers=APPROVER)).status_code == 403
    r = await c.put(
        "/v1/admin/flags",
        json={"autonomy": {"default": "AUTO_LOW_RISK", "canary_percent": 100}, "kill_switch": True},
        headers=ADMIN,
    )
    assert r.status_code == 200 and r.json()["kill_switch"] is True
    cid = await submit(sm, money_case())
    state = await run_case(deps, cid)
    assert state.final_status == "escalated" and await paid(sm, cid) == 0
    await c.put("/v1/admin/flags", json={"kill_switch": False}, headers=ADMIN)
    got = (await c.get("/v1/admin/flags", headers=ADMIN)).json()
    assert got["kill_switch"] is False and got["autonomy"]["default"] == "AUTO_LOW_RISK"
    bad = await c.put("/v1/admin/flags", json={"autonomy": {"default": "YOLO"}}, headers=ADMIN)
    assert bad.status_code == 422


async def test_canary_percent_zero_sends_everything_to_approval(env):
    c, deps, sm = env
    await c.put(
        "/v1/admin/flags",
        json={"autonomy": {"default": "AUTO_LOW_RISK", "canary_percent": 0}, "kill_switch": False},
        headers=ADMIN,
    )
    cid = await submit(sm, money_case())
    state = await run_case(deps, cid)
    assert state.final_status == "awaiting_approval" and await paid(sm, cid) == 0


async def test_console_pages_and_htmx_decision(env):
    c, deps, sm = env
    r = await c.get("/console/queue")
    assert r.status_code == 303 and r.headers["location"] == "/console/login"
    cid, aid, amount = await pending_approval(env)
    c.cookies.set("opspod_key", "dev-analyst")
    c.cookies.set("opspod_role", "analyst")
    q = await c.get("/console/queue")
    assert q.status_code == 200 and "view only" in q.text and str(cid) in q.text
    deny = await c.post(f"/console/approvals/{aid}/decision", data={"decision": "approve"})
    assert deny.status_code in (401, 403) or "error" in deny.text
    assert await paid(sm, cid) == 0
    c.cookies.set("opspod_key", "dev-approver")
    c.cookies.set("opspod_role", "approver")
    q = await c.get("/console/queue")
    assert "Approve" in q.text
    done = await c.post(
        f"/console/approvals/{aid}/decision", data={"decision": "approve", "note": "hx"}
    )
    assert done.status_code == 200 and "approved" in done.text and await paid(sm, cid) == amount
    page = await c.get(f"/console/cases/{cid}")
    assert page.status_code == 200 and "replay identical" in page.text and "Reverse" in page.text
    dash = await c.get("/console/dashboard")
    assert dash.status_code == 200 and "Auto-resolve rate" in dash.text
    assert (await c.get("/console/dlq")).status_code == 200
    bad = await c.post(f"/console/approvals/{aid}/decision", data={"decision": "approve"})
    assert "error" in bad.text  # already decided
    bad2 = await c.post(
        f"/console/approvals/{uuid.uuid4()}/decision",
        data={"decision": "approve", "edited_amount": "abc"},
    )
    assert "error" in bad2.text


async def test_llm_case_review_flow(env):
    """Ambiguous case: LLM proposes, human approves, amount stays code-computed."""
    c, deps, sm = env
    await set_mode(sm, AutonomyMode.AUTO_LOW_RISK)
    deps2, _ = make_deps(sm, script=lambda s, u: analysis())
    case = next(x for x in BY_TAG["ambiguous"] if x["expected"]["outcome"] == "PARTIAL_REFUND")
    cid = await submit(sm, case)
    await run_case(deps2, cid)
    async with sm() as s:
        appr = (await s.execute(select(Approval).where(Approval.case_id == cid))).scalar_one()
    r = await c.post(
        f"/v1/approvals/{appr.id}/decision", json={"decision": "approve"}, headers=APPROVER
    )
    assert r.status_code == 200 and await paid(sm, cid) == case["expected"]["amount"]


async def test_dlq_retry_resumes_the_case_and_is_single_use(env):
    from opspod.db.models import DeadLetter
    from opspod.pipeline.worker import dead_letter

    c, deps, sm = env
    queued: list[tuple[str, bool]] = []

    async def enqueue(cid: str, force: bool = False) -> None:
        queued.append((cid, force))

    app = create_app(deps, enqueue)
    await set_mode(sm, AutonomyMode.AUTO_LOW_RISK)
    cid = await submit(sm, money_case())
    await dead_letter(deps, cid, "RuntimeError: boom", 3)
    async with sm() as s:
        dl = (await s.execute(select(DeadLetter).where(DeadLetter.case_id == cid))).scalar_one()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
        assert (await cl.post(f"/v1/admin/dlq/{dl.id}/retry", headers=ANALYST)).status_code == 403
        r = await cl.post(f"/v1/admin/dlq/{dl.id}/retry", headers=APPROVER)
        assert r.status_code == 200 and queued == [(str(cid), True)]
        assert (await cl.post(f"/v1/admin/dlq/{dl.id}/retry", headers=APPROVER)).status_code == 409
        assert (
            await cl.post(f"/v1/admin/dlq/{uuid.uuid4()}/retry", headers=APPROVER)
        ).status_code == 404
    async with sm() as s:
        assert (await s.get(Case, cid)).status == "processing"
    state = await run_case(deps, cid)  # the "worker" picks it up and finishes
    assert state.final_status == "resolved" and await paid(sm, cid) == money_amount()


def money_amount() -> int:
    return money_case()["expected"]["amount"]
