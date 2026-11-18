"""Persist provider attempts for cross-window reliability diagnostics."""

import sqlalchemy as sa
from alembic import op

revision = "0005_provider_attempts"
down_revision = "0004_query_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "provider_attempts" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "provider_attempts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_id", sa.String(length=80), nullable=False),
        sa.Column("adapter_id", sa.String(length=100), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("code", sa.String(length=80), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("query_hash", sa.String(length=64), nullable=False),
        sa.Column("query_language", sa.String(length=35), nullable=True),
        sa.Column("process_id", sa.Integer(), nullable=False),
    )
    for column in ("occurred_at", "source_id", "adapter_id", "outcome", "code", "query_hash"):
        op.create_index(op.f(f"ix_provider_attempts_{column}"), "provider_attempts", [column])


def downgrade() -> None:
    op.drop_table("provider_attempts")
