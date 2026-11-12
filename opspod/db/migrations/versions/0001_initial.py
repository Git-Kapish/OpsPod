"""initial schema: core tables, append-only triggers

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0001"
down_revision = None

PK = dict(primary_key=True)
NOW = sa.text("now()")


def _uuid(name: str = "id", **kw):  # type: ignore[no-untyped-def]
    return sa.Column(name, UUID(as_uuid=True), **kw)


def upgrade() -> None:
    op.create_table(
        "cases",
        _uuid(primary_key=True),
        sa.Column("external_id", sa.String, nullable=False, unique=True),
        sa.Column("type", sa.String, nullable=False),
        sa.Column("region", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW),
        sa.Column("payload_json", JSONB, nullable=False),
        sa.Column("redacted_json", JSONB, nullable=False),
    )
    op.create_table(
        "case_steps",
        _uuid(primary_key=True),
        _uuid("case_id", nullable=False),
        sa.Column("step", sa.String, nullable=False),
        sa.Column("state_json", JSONB, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=NOW),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
        sa.UniqueConstraint("case_id", "step", "attempt"),
    )
    op.create_index("ix_case_steps_case_id", "case_steps", ["case_id"])
    op.create_table(
        "decisions",
        _uuid(primary_key=True),
        _uuid("case_id", nullable=False),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("outcome", sa.String, nullable=False),
        sa.Column("amount", sa.BigInteger, nullable=False),
        sa.Column("confidence", sa.Float),
        sa.Column("rationale", sa.Text, nullable=False),
        sa.Column("policy_rules_fired", JSONB, nullable=False),
        sa.Column("route", sa.String, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
    )
    op.create_index("ix_decisions_case_id", "decisions", ["case_id"])
    op.create_table(
        "approvals",
        _uuid(primary_key=True),
        _uuid("case_id", nullable=False),
        _uuid("decision_id", nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=NOW),
        sa.Column("decided_by", sa.String),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("edited_amount", sa.BigInteger),
        sa.Column("note", sa.Text),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
        sa.ForeignKeyConstraint(["decision_id"], ["decisions.id"]),
    )
    op.create_index("ix_approvals_case_id", "approvals", ["case_id"])
    op.create_table(
        "actions",
        _uuid(primary_key=True),
        _uuid("case_id", nullable=False),
        sa.Column("type", sa.String, nullable=False),
        sa.Column("idempotency_key", sa.String, nullable=False, unique=True),
        sa.Column("args_json", JSONB, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), server_default=NOW),
        _uuid("reversed_by_action_id"),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
        sa.ForeignKeyConstraint(["reversed_by_action_id"], ["actions.id"]),
    )
    op.create_index("ix_actions_case_id", "actions", ["case_id"])
    op.create_table(
        "ledger_entries",
        _uuid(primary_key=True),
        sa.Column("account", sa.String, nullable=False),
        sa.Column("amount", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        _uuid("action_id", nullable=False),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW),
        sa.ForeignKeyConstraint(["action_id"], ["actions.id"]),
        sa.CheckConstraint("amount > 0", name="ledger_amount_positive"),
        sa.CheckConstraint("kind in ('refund','credit','reversal')", name="ledger_kind_valid"),
    )
    op.create_index("ix_ledger_entries_account", "ledger_entries", ["account"])
    op.create_index("ix_ledger_entries_action_id", "ledger_entries", ["action_id"])
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        _uuid("case_id"),
        sa.Column("event", sa.String, nullable=False),
        sa.Column("actor", sa.String, nullable=False),
        sa.Column("payload_json", JSONB, nullable=False),
        sa.Column("trace_id", sa.String),
        sa.Column("prompt_version", sa.String),
        sa.Column("model", sa.String),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW),
    )
    op.create_index("ix_audit_log_case_id", "audit_log", ["case_id"])
    op.create_table(
        "flags",
        sa.Column("key", sa.String, primary_key=True),
        sa.Column("value_json", JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW),
        sa.Column("updated_by", sa.String, nullable=False),
    )

    # Append-only enforcement: no UPDATE/DELETE/TRUNCATE on ledger and audit tables.
    op.execute(
        """
        CREATE FUNCTION forbid_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only (% not allowed)', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table in ("ledger_entries", "audit_log"):
        op.execute(
            f"CREATE TRIGGER {table}_no_mutate BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION forbid_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation()"
        )


def downgrade() -> None:
    for table in ("audit_log", "ledger_entries"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_mutate ON {table}")
    op.execute("DROP FUNCTION IF EXISTS forbid_mutation()")
    for table in (
        "flags",
        "audit_log",
        "ledger_entries",
        "actions",
        "approvals",
        "decisions",
        "case_steps",
        "cases",
    ):
        op.drop_table(table)
