"""Multi-tenant organizations and projects; tenant-scoped governance and task registry.

Existing rows move to the `default` organization and `default` project.
Baselines and branch states get composite primary keys
`(organization_id, project_id, tag|branch)`; every tenant-scoped table gets a
composite foreign key to `projects(organization_id, id)`.

Revision ID: 0003_multi_tenant
Revises: 0002_request_base_snapshot
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_multi_tenant"
down_revision = "0002_request_base_snapshot"
branch_labels = None
depends_on = None


def _tenant_columns(primary: bool = False) -> list[sa.Column]:
    return [
        sa.Column("organization_id", sa.String(63), nullable=False, primary_key=primary, server_default="default"),
        sa.Column("project_id", sa.String(63), nullable=False, primary_key=primary, server_default="default"),
    ]


def _project_fk() -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["organization_id", "project_id"], ["projects.organization_id", "projects.id"], ondelete="CASCADE")


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(63), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("identity_issuer", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "projects",
        sa.Column("organization_id", sa.String(63), sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("id", sa.String(63), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(255), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("INSERT INTO organizations (id, name) VALUES ('default', 'Default organization')")
    op.execute("INSERT INTO projects (organization_id, id, name, description, created_by) VALUES ('default', 'default', 'default', '', 'system')")

    with op.batch_alter_table("parameter_change_requests") as batch:
        for column in _tenant_columns():
            batch.add_column(column)
        batch.create_index("ix_parameter_change_requests_organization_id", ["organization_id"])
        batch.create_foreign_key("fk_change_requests_project", "projects", ["organization_id", "project_id"],
                                 ["organization_id", "id"], ondelete="CASCADE")

    # Composite primary keys: rebuild the two keyed tables and copy rows into the default tenant
    op.create_table(
        "parameter_baselines_v3",
        *_tenant_columns(primary=True),
        sa.Column("tag", sa.String(255), primary_key=True),
        sa.Column("fabric_hash", sa.String(64), nullable=False),
        sa.Column("fabric_json", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        _project_fk(),
    )
    op.execute("INSERT INTO parameter_baselines_v3 (organization_id, project_id, tag, fabric_hash, fabric_json, created_by, created_at) "
               "SELECT 'default', 'default', tag, fabric_hash, fabric_json, created_by, created_at FROM parameter_baselines")
    op.drop_table("parameter_baselines")
    op.rename_table("parameter_baselines_v3", "parameter_baselines")

    op.create_table(
        "parameter_fabric_states_v3",
        *_tenant_columns(primary=True),
        sa.Column("branch", sa.String(255), primary_key=True),
        sa.Column("fabric_json", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.String(255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        _project_fk(),
    )
    op.execute("INSERT INTO parameter_fabric_states_v3 (organization_id, project_id, branch, fabric_json, updated_by, updated_at) "
               "SELECT 'default', 'default', branch, fabric_json, updated_by, updated_at FROM parameter_fabric_states")
    op.drop_table("parameter_fabric_states")
    op.rename_table("parameter_fabric_states_v3", "parameter_fabric_states")

    op.create_table(
        "simulation_tasks",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("organization_id", sa.String(63), nullable=False, index=True),
        sa.Column("project_id", sa.String(63), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("owner", sa.String(255), nullable=False),
        sa.Column("analysis", sa.String(16), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("engine", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        _project_fk(),
    )


def downgrade() -> None:
    op.drop_table("simulation_tasks")
    for table in ("parameter_fabric_states", "parameter_baselines"):
        op.execute(f"DELETE FROM {table} WHERE organization_id <> 'default' OR project_id <> 'default'")
        with op.batch_alter_table(table, recreate="always") as batch:
            batch.drop_column("organization_id")
            batch.drop_column("project_id")
    with op.batch_alter_table("parameter_change_requests") as batch:
        batch.drop_constraint("fk_change_requests_project", type_="foreignkey")
        batch.drop_index("ix_parameter_change_requests_organization_id")
        batch.drop_column("organization_id")
        batch.drop_column("project_id")
    op.drop_table("projects")
    op.drop_table("organizations")
