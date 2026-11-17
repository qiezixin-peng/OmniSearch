"""Persist query-level execution checkpoints for crash-safe research continuation."""

import sqlalchemy as sa
from alembic import op

revision = "0004_query_checkpoints"
down_revision = "0003_result_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("query_variants")}
    additions = [
        (
            "status",
            sa.Column(
                "status", sa.String(length=20), nullable=False, server_default="pending"
            ),
        ),
        ("attempts", sa.Column("attempts", sa.Integer(), nullable=False, server_default="0")),
        (
            "result_count",
            sa.Column("result_count", sa.Integer(), nullable=False, server_default="0"),
        ),
        (
            "failure_count",
            sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        ),
        ("completed_at", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)),
    ]
    for name, column in additions:
        if name not in columns:
            op.add_column("query_variants", column)
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("query_variants")}
    index_name = op.f("ix_query_variants_status")
    if index_name not in indexes:
        op.create_index(index_name, "query_variants", ["status"], unique=False)
    bind.execute(
        sa.text(
            """
            UPDATE query_variants
            SET result_count = (
                    SELECT COUNT(*)
                    FROM query_result_discoveries
                    WHERE query_result_discoveries.query_id = query_variants.id
                ),
                status = 'completed',
                completed_at = CURRENT_TIMESTAMP
            WHERE research_id IN (
                SELECT id FROM research_tasks WHERE status IN ('completed', 'partial')
            )
            """
        )
    )


def downgrade() -> None:
    # Checkpoint data is intentionally retained; removing it would make a partially
    # executed research task unsafe to resume.
    pass
