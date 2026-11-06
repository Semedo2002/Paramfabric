"""Validated change request workflow with enforced review before main merge"""

from __future__ import annotations

import json
import hashlib
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from paramfabric.core.graph import ParameterGraph
from paramfabric.core.schema import Fabric
from paramfabric.governance.impact import Impact, blast_radius
from paramfabric.governance.tenancy import DEFAULT_TENANT, Tenant
from paramfabric.governance.store import (
    ChangeRequestRecord,
    FabricStateRecord,
    ReviewRecord,
)


class ChangeRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposed_fabric: Fabric
    rationale: str = Field(min_length=1)
    target_branch: str = "main"
    base_revision: str | None = None


class ReviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    comment: str = ""


def _changed_names(before: Fabric, after: Fabric) -> set[str]:
    old, new = before.by_name(), after.by_name()
    return {
        name
        for name in old.keys() | new.keys()
        if name not in old or name not in new or old[name] != new[name]
    }


def fabric_hash(fabric: Fabric) -> str:
    return _fabric_hash(fabric)


def _state_key(tenant: Tenant, branch: str) -> tuple[str, str, str]:
    return (tenant.organization, tenant.project, branch)


def _fabric_hash(fabric: Fabric) -> str:
    canonical = json.dumps(fabric.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_request(
    session: Session,
    payload: ChangeRequestCreate,
    author: str,
    current: Fabric,
    *,
    tenant: Tenant = DEFAULT_TENANT,
) -> tuple[ChangeRequestRecord, list[Impact]]:
    """Validate proposal, calculate impacts, and persist as pending"""
    proposed_graph = ParameterGraph(payload.proposed_fabric)
    proposed_graph.evaluate()
    changes = _changed_names(current, payload.proposed_fabric)
    if not changes:
        raise ValueError("proposal does not change any parameters")
    state = session.get(FabricStateRecord, _state_key(tenant, payload.target_branch))
    if state is None:
        state=FabricStateRecord(
            organization_id=tenant.organization,
            project_id=tenant.project,
            branch=payload.target_branch,
            fabric_json=current.model_dump_json(),
            updated_by="system",
        )
        session.add(state)
    elif _fabric_hash(Fabric.model_validate_json(state.fabric_json)) != _fabric_hash(current):
        raise ValueError("branch changed while the proposal was being prepared; refresh and retry")
    impacts = blast_radius(proposed_graph, changes)
    record = ChangeRequestRecord(
        id=str(uuid4()),
        organization_id=tenant.organization,
        project_id=tenant.project,
        author=author,
        rationale=payload.rationale,
        target_branch=payload.target_branch,
        base_revision=payload.base_revision,
        base_hash=_fabric_hash(current),
        base_fabric=current.model_dump_json(),
        status="pending",
        proposed_fabric=payload.proposed_fabric.model_dump_json(),
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record, impacts


def get_request(session: Session, request_id: str, tenant: Tenant = DEFAULT_TENANT) -> ChangeRequestRecord | None:
    request = session.get(ChangeRequestRecord, request_id)
    if request is None or (request.organization_id, request.project_id) != (tenant.organization, tenant.project):
        return None
    return request


def add_review(
    session: Session,
    request_id: str,
    reviewer: str,
    payload: ReviewCreate,
    *,
    tenant: Tenant = DEFAULT_TENANT,
) -> ChangeRequestRecord:
    """Record an independent approval/rejection and update request status"""
    request = get_request(session, request_id, tenant)
    if request is None:
        raise LookupError("change request not found")
    if request.status != "pending":
        raise ValueError(f"request is already {request.status}")
    if request.author == reviewer:
        raise PermissionError("authors cannot review their own change request")
    review = ReviewRecord(
        request_id=request_id,
        reviewer=reviewer,
        decision=payload.decision,
        comment=payload.comment,
    )
    session.add(review)
    try:
        request.status = "approved" if payload.decision == "approve" else "rejected"
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ValueError("reviewer has already reviewed this request") from exc
    session.refresh(request)
    return request


def merge_request(session: Session, request_id: str, actor: str, *, tenant: Tenant = DEFAULT_TENANT) -> Fabric:
    """Promote only an approved request to the main branch state"""
    request = get_request(session, request_id, tenant)
    if request is None:
        raise LookupError("change request not found")
    if request.status != "approved":
        raise ValueError("only approved change requests can be merged")
    fabric = Fabric.model_validate_json(request.proposed_fabric)
    state = session.get(FabricStateRecord, _state_key(tenant, request.target_branch))
    if state is None:
        raise ValueError("branch state disappeared; cannot safely merge")
    if _fabric_hash(Fabric.model_validate_json(state.fabric_json)) != request.base_hash:
        raise ValueError("branch changed after this request was opened; rebase and submit a new request")
    state.fabric_json = fabric.model_dump_json()
    state.updated_by = actor
    request.status = "merged"
    session.commit()
    return fabric


def current_fabric(session: Session, initial: Fabric, branch: str = "main", *, tenant: Tenant = DEFAULT_TENANT) -> Fabric:
    """Return persisted branch state or seed it from the configured fabric"""
    state = session.get(FabricStateRecord, _state_key(tenant, branch))
    if state is not None:
        return Fabric.model_validate_json(state.fabric_json)
    state = FabricStateRecord(organization_id=tenant.organization, project_id=tenant.project, branch=branch,
                              fabric_json=initial.model_dump_json(), updated_by="system")
    session.add(state)
    session.commit()
    return initial


def request_summary(record: ChangeRequestRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "organization": record.organization_id,
        "project": record.project_id,
        "author": record.author,
        "rationale": record.rationale,
        "target_branch": record.target_branch,
        "base_revision": record.base_revision,
        "status": record.status,
        "proposed_fabric": json.loads(record.proposed_fabric),
        "created_at": record.created_at.isoformat(),
        "reviews": [
            {
                "reviewer": review.reviewer,
                "decision": review.decision,
                "comment": review.comment,
                "created_at": review.created_at.isoformat(),
            }
            for review in record.reviews
        ],
    }


def list_requests(session: Session, tenant: Tenant = DEFAULT_TENANT) -> list[ChangeRequestRecord]:
    return list(
        session.scalars(
            select(ChangeRequestRecord)
            .where(ChangeRequestRecord.organization_id == tenant.organization, ChangeRequestRecord.project_id == tenant.project)
            .order_by(ChangeRequestRecord.created_at.desc())
        ).unique()
    )
