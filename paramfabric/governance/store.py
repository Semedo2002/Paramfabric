"""SQLAlchemy persistence for change requests, reviews, and baselines"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

TENANT_DEFAULT = "default"


def _project_fk() -> ForeignKeyConstraint:
    # composite reference: a row's project must belong to the row's organization
    return ForeignKeyConstraint(
        ["organization_id", "project_id"], ["projects.organization_id", "projects.id"], ondelete="CASCADE"
    )


class Base(DeclarativeBase):
    """Shared SQLAlchemy declarative base"""


class ChangeRequestRecord(Base):
    __tablename__="parameter_change_requests"
    __table_args__=(_project_fk(),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    organization_id: Mapped[str] = mapped_column(String(63), nullable=False, default=TENANT_DEFAULT, index=True)
    project_id: Mapped[str] = mapped_column(String(63), nullable=False, default=TENANT_DEFAULT)
    author: Mapped[str] = mapped_column(String(255), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    target_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="main")
    base_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    base_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    base_fabric: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    proposed_fabric: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    reviews: Mapped[list["ReviewRecord"]] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )


class ReviewRecord(Base):
    __tablename__ = "parameter_change_reviews"
    __table_args__=(UniqueConstraint("request_id", "reviewer", name="uq_review_request_reviewer"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(
        ForeignKey("parameter_change_requests.id", ondelete="CASCADE"), nullable=False
    )
    reviewer: Mapped[str] = mapped_column(String(255), nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    request: Mapped[ChangeRequestRecord] = relationship(back_populates="reviews")


class BaselineRecord(Base):
    __tablename__="parameter_baselines"
    __table_args__=(_project_fk(),)

    organization_id: Mapped[str] = mapped_column(String(63), primary_key=True, default=TENANT_DEFAULT)
    project_id: Mapped[str] = mapped_column(String(63), primary_key=True, default=TENANT_DEFAULT)
    tag: Mapped[str] = mapped_column(String(255), primary_key=True)
    fabric_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fabric_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class FabricStateRecord(Base):
    __tablename__ = "parameter_fabric_states"
    __table_args__=(_project_fk(),)

    organization_id: Mapped[str] = mapped_column(String(63), primary_key=True, default=TENANT_DEFAULT)
    project_id: Mapped[str] = mapped_column(String(63), primary_key=True, default=TENANT_DEFAULT)
    branch: Mapped[str] = mapped_column(String(255), primary_key=True)
    fabric_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


def create_database(
    database_url: str = "sqlite:///paramfabric-enterprise.db",
    *,
    create_schema: bool = True,
) -> tuple[Any, sessionmaker[Session]]:
    """Create engine and tables; SQLite connections are safe for FastAPI workers.

    SQLite enforces foreign keys only when asked, so every connection enables
    them: tenant boundaries (composite organization/project keys) hold in the
    database on every backend. The default organization and project are
    created on first use.
    """
    import paramfabric.server.models as models  # registers organizations/projects/simulation_tasks

    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(database_url, **kwargs)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _foreign_keys(connection: Any, _: Any) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    if create_schema:
        Base.metadata.create_all(engine)
        with factory() as session:
            models.ensure_default_tenant(session)
    return engine, factory


def sqlite_url(path: str | Path) -> str:
    return f"sqlite:///{Path(path).resolve().as_posix()}"
