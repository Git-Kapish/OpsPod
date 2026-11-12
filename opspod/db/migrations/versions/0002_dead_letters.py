"""dead letter queue table

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.create_table(
        "dead_letters",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("case_id", UUID(as_uuid=True), nullable=False),
        sa.Column("step", sa.String, nullable=False),
        sa.Column("error", sa.Text, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_by", sa.String),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"]),
    )
    op.create_index("ix_dead_letters_case_id", "dead_letters", ["case_id"])


def downgrade() -> None:
    op.drop_table("dead_letters")
