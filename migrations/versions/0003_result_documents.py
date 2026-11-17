"""Link acquired documents back to research search results."""

import sqlalchemy as sa
from alembic import op

revision = "0003_result_documents"
down_revision = "0002_document_object_uri"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("result_documents"):
        return
    op.create_table(
        "result_documents",
        sa.Column("research_id", sa.String(length=36), nullable=False),
        sa.Column("result_id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("version_id", sa.String(length=36), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(
            ["research_id"], ["research_tasks.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["result_id"], ["search_results.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["version_id"], ["document_versions.id"]),
        sa.PrimaryKeyConstraint("research_id", "result_id"),
    )
    op.create_index(
        op.f("ix_result_documents_document_id"),
        "result_documents",
        ["document_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_result_documents_version_id"),
        "result_documents",
        ["version_id"],
        unique=False,
    )


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("result_documents"):
        return
    op.drop_index(op.f("ix_result_documents_version_id"), table_name="result_documents")
    op.drop_index(op.f("ix_result_documents_document_id"), table_name="result_documents")
    op.drop_table("result_documents")
