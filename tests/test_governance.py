import pytest

from paramfabric.core.schema import Fabric
from paramfabric.governance.baselines import baseline_diff, create_baseline
from paramfabric.governance.pr_engine import (
    ChangeRequestCreate,
    ReviewCreate,
    add_review,
    create_request,
    merge_request,
)
from paramfabric.governance.store import create_database


def fabrics():
    original = Fabric.model_validate(
        {
            "parameters": [
                {"name": "chord", "value": 2, "unit": "m", "tags": ["geometry"]},
                {"name": "span", "value": "5 * chord", "unit": "m", "tags": ["geometry"]},
            ]
        }
    )
    updated = Fabric.model_validate(
        {
            "parameters": [
                {"name": "chord", "value": 3, "unit": "m", "tags": ["geometry"]},
                {"name": "span", "value": "5 * chord", "unit": "m", "tags": ["geometry"]},
            ]
        }
    )
    return original, updated


def test_change_requires_independent_approval_before_merge(tmp_path):
    _, session_factory = create_database(f"sqlite:///{(tmp_path / 'governance.db').as_posix()}")
    original, updated = fabrics()
    with session_factory() as session:
        request, impacts = create_request(
            session,
            ChangeRequestCreate(proposed_fabric=updated, rationale="Increase span"),
            "engineer",
            original,
        )
        assert [item["parameter"] for item in impacts] == ["chord", "span"]
        with pytest.raises(PermissionError, match="own"):
            add_review(session, request.id, "engineer", ReviewCreate(decision="approve"))
        with pytest.raises(ValueError, match="only approved"):
            merge_request(session, request.id, "admin")
        add_review(session, request.id, "approver", ReviewCreate(decision="approve"))
        merged = merge_request(session, request.id, "admin")
        assert merged.by_name()["chord"].value == 3


def test_named_baselines_are_immutable_and_diffable(tmp_path):
    _, session_factory = create_database(f"sqlite:///{(tmp_path / 'baseline.db').as_posix()}")
    original, updated = fabrics()
    with session_factory() as session:
        first = create_baseline(session, "v1-flight", original, "lead")
        create_baseline(session, "v2-flight", updated, "lead")
        assert len(first.fabric_hash) == 64
        assert any(item["name"] == "span" for item in baseline_diff(session, "v1-flight", "v2-flight"))
        with pytest.raises(ValueError, match="immutable"):
            create_baseline(session, "v1-flight", updated, "lead")


def test_stale_approved_request_cannot_overwrite_a_newer_merge(tmp_path):
    _, session_factory = create_database(f"sqlite:///{(tmp_path / 'stale.db').as_posix()}")
    original, updated = fabrics()
    _, second_update = fabrics()
    second_update.parameters[0].value = 4
    with session_factory() as session:
        first, _ = create_request(
            session,
            ChangeRequestCreate(proposed_fabric=updated, rationale="First update"),
            "engineer-a",
            original,
        )
        second, _ = create_request(
            session,
            ChangeRequestCreate(proposed_fabric=second_update, rationale="Concurrent update"),
            "engineer-b",
            original,
        )
        add_review(session, first.id, "reviewer-a", ReviewCreate(decision="approve"))
        add_review(session, second.id, "reviewer-b", ReviewCreate(decision="approve"))
        merge_request(session, first.id, "admin")
        with pytest.raises(ValueError, match="branch changed"):
            merge_request(session, second.id, "admin")
