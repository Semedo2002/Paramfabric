"""Multi-tenant relational model: organizations, projects, and the simulation task registry.

Organization boundaries are enforced by the database itself: every governed
table (change requests, baselines, branch states, simulation tasks) carries
`(organization_id, project_id)` with a **composite foreign key** to
`projects(organization_id, id)`. A row can therefore only reference a
project that belongs to the same organization; a mismatched pair is rejected
by the database, not just by application code. SQLite connections enable
`PRAGMA foreign_keys` (see :func:`paramfabric.governance.store.create_database`).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from paramfabric.governance.store import Base
from paramfabric.governance.tenancy import DEFAULT_ORGANIZATION, DEFAULT_PROJECT, Tenant


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(63), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    identity_issuer: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Project(Base):
    __tablename__ = "projects"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[str] = mapped_column(String(63), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by: Mapped[str] = mapped_column(String(255), nullable=False, default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


def project_foreign_key() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["organization_id", "project_id"], ["projects.organization_id", "projects.id"], ondelete="CASCADE"
    )


class SimulationTaskRecord(Base):
    """Durable, tenant-scoped index of mesh/solve tasks (live state stays in the queue)"""

    __tablename__ = "simulation_tasks"
    __table_args__ = (project_foreign_key(),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(63), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(63), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    analysis: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    engine: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def ensure_tenant(session: Session, tenant: Tenant, *, organization_name: str | None = None,
                  issuer: str | None = None, create: bool = True) -> bool:
    """Make sure the organization and project rows exist; returns False if missing and not created"""
    organization = session.get(Organization, tenant.organization)
    if organization is None:
        if not create:
            return False
        session.add(Organization(id=tenant.organization, name=organization_name or tenant.organization, identity_issuer=issuer))
        session.flush()
    if session.get(Project, (tenant.organization, tenant.project)) is None:
        if not create:
            return False
        session.add(Project(organization_id=tenant.organization, id=tenant.project, name=tenant.project))
    session.commit()
    return True


def ensure_default_tenant(session: Session) -> None:
    ensure_tenant(session, Tenant(DEFAULT_ORGANIZATION, DEFAULT_PROJECT), organization_name="Default organization")


def list_projects(session: Session, organization: str) -> list[Project]:
    return list(session.scalars(select(Project).where(Project.organization_id == organization).order_by(Project.id)))


class SigningKeyRecord(Base):
    """Public half of an organization's Ed25519 signing key (reports and approval locks).

    Private keys never enter the database; verifiers fetch these public keys
    (`GET /api/public/organizations/<org>/signing-keys`). Rotated keys stay
    listed with `revoked_at` so older reports remain verifiable.
    """

    __tablename__ = "signing_keys"

    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True)
    key_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    algorithm: Mapped[str] = mapped_column(String(16), nullable=False, default="Ed25519")
    public_key_pem: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False, default="reports,approvals")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def register_public_key(session: Session, organization: str, signer: object) -> SigningKeyRecord:
    record = session.get(SigningKeyRecord, (organization, signer.key_id))  # type: ignore[attr-defined]
    if record is None:
        record = SigningKeyRecord(organization_id=organization, key_id=signer.key_id,  # type: ignore[attr-defined]
                                  public_key_pem=signer.public_pem)  # type: ignore[attr-defined]
        session.add(record)
        session.commit()
    return record


def organization_public_keys(session: Session, organization: str, include_revoked: bool = True) -> list[SigningKeyRecord]:
    query = select(SigningKeyRecord).where(SigningKeyRecord.organization_id == organization)
    if not include_revoked:
        query = query.where(SigningKeyRecord.revoked_at.is_(None))
    return list(session.scalars(query.order_by(SigningKeyRecord.created_at)))
