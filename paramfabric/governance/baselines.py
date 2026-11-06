"""Immutable fabric baselines and content-addressed comparisons"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy.orm import Session

from paramfabric.core.graph import ParameterGraph
from paramfabric.core.schema import Fabric
from paramfabric.governance.store import BaselineRecord
from paramfabric.governance.tenancy import DEFAULT_TENANT, Tenant
from paramfabric.utils.git_tools import parameter_diff


def _canonical(fabric: Fabric) -> str:
    return json.dumps(fabric.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def create_baseline(
    session: Session,
    tag: str,
    fabric: Fabric,
    created_by: str,
    *,
    tenant: Tenant = DEFAULT_TENANT,
) -> BaselineRecord:
    """Create a permanent named baseline; duplicate tags are rejected"""
    if not tag.strip():
        raise ValueError("baseline tag cannot be empty")
    ParameterGraph(fabric).evaluate()
    if get_baseline(session, tag, tenant) is not None:
        raise ValueError(f"baseline {tag!r} already exists and is immutable")
    canonical=_canonical(fabric)
    record = BaselineRecord(
        organization_id=tenant.organization,
        project_id=tenant.project,
        tag=tag,
        fabric_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        fabric_json=canonical,
        created_by=created_by,
    )
    session.add(record)
    session.commit()
    return record


def get_baseline(session: Session, tag: str, tenant: Tenant = DEFAULT_TENANT) -> BaselineRecord | None:
    return session.get(BaselineRecord, (tenant.organization, tenant.project, tag))


def baseline_diff(session: Session, first: str, second: str, tenant: Tenant = DEFAULT_TENANT) -> list[dict[str, object]]:
    left, right = get_baseline(session, first, tenant), get_baseline(session, second, tenant)
    if left is None or right is None:
        raise LookupError("one or both baseline tags were not found")
    return parameter_diff(
        Fabric.model_validate_json(left.fabric_json),
        Fabric.model_validate_json(right.fabric_json),
    )


def list_baselines(session: Session, tenant: Tenant = DEFAULT_TENANT) -> list[BaselineRecord]:
    from sqlalchemy import select

    return list(
        session.scalars(
            select(BaselineRecord)
            .where(BaselineRecord.organization_id == tenant.organization, BaselineRecord.project_id == tenant.project)
            .order_by(BaselineRecord.created_at.desc())
        )
    )
