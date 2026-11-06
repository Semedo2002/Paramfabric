"""Create enterprise governance tables.

Revision ID: 0001_initial
Revises:
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "parameter_change_requests",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("author", sa.String(length=255), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("target_branch", sa.String(length=255), nullable=False),
        sa.Column("base_revision", sa.String(length=128), nullable=True),
        sa.Column("base_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("proposed_fabric", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "parameter_change_reviews",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "request_id",
            sa.String(length=36),
            sa.ForeignKey("parameter_change_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reviewer", sa.String(length=255), nullable=False),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("request_id", "reviewer", name="uq_review_request_reviewer"),
    )
    op.create_table(
        "parameter_baselines",
        sa.Column("tag", sa.String(length=255), primary_key=True),
        sa.Column("fabric_hash", sa.String(length=64), nullable=False),
        sa.Column("fabric_json", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "parameter_fabric_states",
        sa.Column("branch", sa.String(length=255), primary_key=True),
        sa.Column("fabric_json", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("parameter_fabric_states")
    op.drop_table("parameter_baselines")
    op.drop_table("parameter_change_reviews")
    op.drop_table("parameter_change_requests")
