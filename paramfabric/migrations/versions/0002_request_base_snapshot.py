"""Store the immutable fabric snapshot from which each request was proposed.

Revision ID: 0002_request_base_snapshot
Revises: 0001_initial
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_request_base_snapshot"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "parameter_change_requests",
        sa.Column("base_fabric", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("parameter_change_requests", "base_fabric")
