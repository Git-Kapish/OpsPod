"""Dashboard numbers straight from the DB (also feeds README results)."""

from typing import Any

from sqlalchemy import func, select

from opspod.db.models import Action, Approval, Case, DeadLetter, Decision
from opspod.pipeline.deps import Deps


async def dashboard_stats(deps: Deps) -> dict[str, Any]:
    async with deps.sessionmaker() as s:

        async def count(*where: Any, table: Any = Approval) -> int:
            stmt = select(func.count()).select_from(table).where(*where)
            return int((await s.execute(stmt)).scalar_one())

        by_status = dict(
            (await s.execute(select(Case.status, func.count()).group_by(Case.status))).all()
        )
        by_route = dict(
            (await s.execute(select(Decision.route, func.count()).group_by(Decision.route))).all()
        )
        auto_resolved = int(
            (
                await s.execute(
                    select(func.count())
                    .select_from(Case)
                    .join(Decision, Decision.case_id == Case.id)
                    .where(Case.status == "resolved", Decision.route == "AUTO_EXECUTE")
                )
            ).scalar_one()
        )
        pending = await count(Approval.status == "pending")
        dlq = await count(DeadLetter.resolved_at.is_(None), table=DeadLetter)
        reversals = await count(Action.type == "reversal", table=Action)
        payouts = await count(Action.type.in_(["refund", "credit"]), table=Action)
        wait = (
            await s.execute(
                select(
                    func.avg(func.extract("epoch", Approval.decided_at - Approval.requested_at))
                ).where(Approval.decided_at.is_not(None))
            )
        ).scalar_one()
    decided = sum(by_route.values())
    return {
        "total_cases": sum(by_status.values()),
        "by_status": by_status,
        "by_route": by_route,
        "auto_resolve_rate": auto_resolved / decided if decided else 0.0,
        "escalation_rate": by_route.get("ESCALATE", 0) / decided if decided else 0.0,
        "pending_approvals": pending,
        "dlq_depth": dlq,
        "reversal_rate": reversals / payouts if payouts else 0.0,
        "avg_approval_wait_s": round(float(wait), 2) if wait is not None else None,
    }
