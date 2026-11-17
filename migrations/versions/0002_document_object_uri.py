"""Reconcile object storage metadata for databases created by early v0.1 builds."""

import sqlalchemy as sa
from alembic import op

revision = "0002_document_object_uri"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("document_versions")}
    if "object_uri" not in columns:
        op.add_column("document_versions", sa.Column("object_uri", sa.Text(), nullable=True))


def downgrade() -> None:
    # This is an idempotent reconciliation revision. Some databases created by the
    # original dynamic 0001 migration already contain this column, so removing it
    # during downgrade would destroy data and make those databases inconsistent.
    pass
