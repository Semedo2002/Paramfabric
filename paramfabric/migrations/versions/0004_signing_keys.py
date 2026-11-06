"""Organization public signing keys (Ed25519) for reports and approval locks.

Revision ID: 0004_signing_keys
Revises: 0003_multi_tenant
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_signing_keys"
down_revision = "0003_multi_tenant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signing_keys",
        sa.Column("organization_id", sa.String(63), sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("key_id", sa.String(32), primary_key=True),
        sa.Column("algorithm", sa.String(16), nullable=False, server_default="Ed25519"),
        sa.Column("public_key_pem", sa.Text(), nullable=False),
        sa.Column("purpose", sa.String(64), nullable=False, server_default="reports,approvals"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("signing_keys")
