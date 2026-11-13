import os
import uuid

import httpx
import pytest
from arq import create_pool
from arq.connections import RedisSettings
from arq.worker import Worker
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select

from opspod.api.app import create_app
from opspod.core.config import get_settings
from opspod.db.models import Case, DeadLetter
from opspod.domain.enums import AutonomyMode
from opspod.pipeline.runner import run_case
from opspod.pipeline.worker import MAX_TRIES, process_case

from .test_pipeline import BY_TAG, make_deps, set_mode, submit

pytestmark = pytest.mark.integration
AGENT = {"X-API-Key": "dev-agent"}


def body(case):
    i = case["input"]
    return {
        "external_id": i["external_id"],
        "case_type": case["case_type"],
        "region": case["region"],
        "rider_ref": f"{i['rider_ref']}-{uuid.uuid4().hex[:6]}",
        "complaint_text": i["complaint_text"],
    }


@pytest.fixture
async def api(sessionmaker_):
    deps, _ = make_deps(sessionmaker_)
    queued: list[str] = []

    async def enqueue(cid: str) -> None:
        queued.append(cid)

    app = create_app(deps, enqueue)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c, deps, queued


async def test_auth_and_validation(api):
    c, _, queued = api
    case = BY_TAG["clear_cut"][0]
    h = {**AGENT, "Idempotency-Key": f"k-{uuid.uuid4()}"}
    assert (await c.post("/v1/cases", json=body(case))).status_code == 401
    assert (
        await c.post("/v1/cases", json=body(case), headers={"X-API-Key": "nope"})
    ).status_code == 401
    assert (
        await c.post("/v1/cases", json=body(case), headers=AGENT)
    ).status_code == 400  # no idem key
    assert (
        await c.post("/v1/cases", json=body(case), headers={**h, "X-Role": "admin"})
    ).status_code == 403
    assert (
        await c.post("/v1/cases", json={**body(case), "region": "zz"}, headers=h)
    ).status_code == 422
    assert (await c.post("/v1/cases", json={"region": "us"}, headers=h)).status_code == 422
    # approver-only key cannot create cases; agent-service key cannot read as approver
    assert (
        await c.post(
            "/v1/cases",
            json=body(case),
            headers={"X-API-Key": "dev-approver", "X-Role": "approver", **{"Idempotency-Key": "x"}},
        )
    ).status_code == 403
    assert queued == []


async def test_intake_is_idempotent_and_get_shows_timeline(api, sessionmaker_):
    c, deps, queued = api
    await set_mode(sessionmaker_, AutonomyMode.AUTO_LOW_RISK)
    case = next(
        x
        for x in BY_TAG["clear_cut"]
        if x["expected"]["route"] == "auto" and x["expected"]["amount"] > 0
    )
    h = {**AGENT, "Idempotency-Key": f"k-{uuid.uuid4()}"}
    first = await c.post("/v1/cases", json=body(case), headers=h)
    second = await c.post("/v1/cases", json=body(case), headers=h)
    assert first.status_code == second.status_code == 202
    assert first.json()["created"] and not second.json()["created"]
    assert first.json()["case_id"] == second.json()["case_id"]
    assert queued == [first.json()["case_id"]] * 2  # queue dedupes by job id == case id
    await run_case(deps, uuid.UUID(first.json()["case_id"]))
    got = await c.get(f"/v1/cases/{first.json()['case_id']}", headers={"X-API-Key": "dev-analyst"})
    j = got.json()
    assert j["status"] == "resolved" and j["decisions"][0]["route"] == "AUTO_EXECUTE"
    assert [s["step"] for s in j["timeline"]][-1] == "finalize"
    assert "action.executed" in {e["event"] for e in j["events"]}
    assert (
        await c.get(f"/v1/cases/{uuid.uuid4()}", headers={"X-API-Key": "dev-analyst"})
    ).status_code == 404


async def test_health_endpoints(api):
    c, *_ = api
    assert (await c.get("/healthz")).json() == {"status": "ok"}
    assert (await c.get("/readyz")).json() == {"status": "ready"}


async def test_exhausted_retries_dead_letter_and_never_auto_execute(sessionmaker_):
    await set_mode(sessionmaker_, AutonomyMode.AUTO_LOW_RISK)
    deps, _ = make_deps(sessionmaker_)
    cid = await submit(sessionmaker_, BY_TAG["clear_cut"][0])

    def boom(step):
        raise RuntimeError("db exploded")

    deps.before_step = boom
    ctx = {"deps": deps, "job_try": 1}
    from arq import Retry

    with pytest.raises(Retry):
        await process_case(ctx, str(cid))
    ctx["job_try"] = MAX_TRIES
    assert await process_case(ctx, str(cid)) == "dead_letter"
    async with sessionmaker_() as s:
        dl = (await s.execute(select(DeadLetter).where(DeadLetter.case_id == cid))).scalar_one()
        assert dl.attempts == MAX_TRIES and "RuntimeError" in dl.error
        assert (await s.get(Case, cid)).status == "dead_letter"


async def test_arq_worker_processes_job_and_dedupes_duplicates(sessionmaker_):
    await set_mode(sessionmaker_, AutonomyMode.AUTO_LOW_RISK)
    deps, _ = make_deps(sessionmaker_)
    case = next(
        x
        for x in BY_TAG["clear_cut"]
        if x["expected"]["route"] == "auto" and x["expected"]["amount"] > 0
    )
    cid = await submit(sessionmaker_, case)
    rs = RedisSettings.from_dsn(get_settings().redis_url)
    try:
        pool = await create_pool(rs)
    except (OSError, RedisConnectionError) as exc:
        if os.getenv("CI"):
            raise  # CI provides Redis; a missing service must not hide
        pytest.skip(f"redis not reachable (run `make up`): {exc}")
    jobs = [await pool.enqueue_job("process_case", str(cid), _job_id=str(cid)) for _ in range(3)]
    assert jobs[0] is not None and jobs[1] is None and jobs[2] is None  # duplicates collapse

    async def startup(ctx):
        ctx["deps"] = deps

    worker = Worker(
        functions=[process_case],
        redis_settings=rs,
        burst=True,
        on_startup=startup,
        max_tries=MAX_TRIES,
        handle_signals=False,
    )
    await worker.main()
    await worker.pool.aclose()
    async with sessionmaker_() as s:
        assert (await s.get(Case, cid)).status == "resolved"
    await pool.aclose()
