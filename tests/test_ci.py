"""The pull-request quality gate (`paramfabric ci-check`) on real Git repositories"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paramfabric.ci import COMMENT_MARKER, run_ci_check
from paramfabric.cli import app
from paramfabric.core.schema import load_fabric
from paramfabric.governance.approvals import LOCK_FILE, load_lock, sign_approval, verify_approval, write_lock
from paramfabric.governance.pr_engine import fabric_hash
from paramfabric.security.signing import Signer

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
SIGNER = Signer.generate()  # the organization's key: private on the server, public in CI
BASE_FABRIC = """parameters:
  - {name: root_chord, value: 2.0, unit: m, tags: [aero], bindings: {cpp_header: generated/params.hpp, openfoam: system/paramfabricDict}}
  - {name: wing_span, value: "5 * root_chord", unit: m}
  - {name: inlet_velocity, value: 10, unit: m/s}
  - {name: mesh_fineness_ratio, value: 24, unit: dimensionless}
"""


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("PARAMFABRIC_APPROVAL_KEY", raising=False)
    monkeypatch.setenv("PARAMFABRIC_APPROVAL_PUBLIC_KEY", SIGNER.public_pem)  # CI holds only the public key
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "ci@example.com")
    git(tmp_path, "config", "user.name", "CI")
    (tmp_path / "params.yaml").write_text(BASE_FABRIC, encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "params.hpp").write_text("constexpr double ROOT_CHORD = 2.0; // [m]\nconstexpr double WING_SPAN = 10.0; // [m]\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "checkout", "-q", "-b", "feature")
    return tmp_path


def edit(repo: Path, old: str, new: str) -> None:
    path = repo / "params.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def approve(repo: Path, approvers=("lead",)) -> dict:
    record = sign_approval({"change_request": "cr-42", "fabric_hash": fabric_hash(load_fabric(repo / "params.yaml")),
                            "approvers": list(approvers)}, SIGNER)
    write_lock(repo / LOCK_FILE, record)
    return record


def codes(report) -> set[str]:
    return {finding.code for finding in report.errors}


def test_unchanged_fabric_passes(repo):
    report = run_ci_check(repo, Path("params.yaml"), "main")
    assert report.passed and not report.changes and not report.findings


def test_unapproved_edit_and_source_drift_block_the_gate(repo):
    edit(repo, "value: 2.0", "value: 2.4")
    report = run_ci_check(repo, Path("params.yaml"), "main")
    assert codes(report) == {"unapproved-edit", "source-drift"}
    assert {c["name"]: c["change"] for c in report.changes} == {"root_chord": "modified", "wing_span": "calculated"}
    assert [i["parameter"] for i in report.impacts] == ["root_chord", "wing_span"]
    assert {(t["kind"], t["path"]) for t in report.targets} == {("cpp_header", "generated/params.hpp"), ("openfoam", "system/paramfabricDict")}
    assert {(c["file"], c["name"]) for c in report.code} == {("src/params.hpp", "ROOT_CHORD"), ("src/params.hpp", "WING_SPAN")}


def test_signed_approval_and_synced_code_pass_with_a_complete_summary(repo):
    edit(repo, "value: 2.0", "value: 2.4")
    (repo / "src" / "params.hpp").write_text("constexpr double ROOT_CHORD = 2.4; // [m]\nconstexpr double WING_SPAN = 12.0; // [m]\n")
    approve(repo, ["lead", "stress"])
    report = run_ci_check(repo, Path("params.yaml"), "main")
    assert report.passed, report.findings
    assert report.approval["change_request"] == "cr-42"
    markdown = report.markdown()
    assert markdown.startswith(COMMENT_MARKER) and "Paramfabric gate passed" in markdown
    assert "| `root_chord` | modified | 2.0 m | 2.4 m | +0.4 (+20.0%) |" in markdown
    assert "OpenFOAM dictionary `system/paramfabricDict`" in markdown and "`src/params.hpp:2` cpp `WING_SPAN`" in markdown
    assert "approved by lead, stress" in markdown


def test_tampered_or_mismatched_approvals_are_rejected(repo):
    edit(repo, "value: 2.0", "value: 2.4")
    (repo / "src" / "params.hpp").write_text("constexpr double ROOT_CHORD = 2.4; // [m]\nconstexpr double WING_SPAN = 12.0; // [m]\n")
    record = approve(repo)
    lock = json.loads((repo / LOCK_FILE).read_text())
    lock[0]["approvers"] = ["mallory"]
    (repo / LOCK_FILE).write_text(json.dumps(lock))
    assert codes(run_ci_check(repo, Path("params.yaml"), "main")) == {"unapproved-edit"}
    write_lock(repo / LOCK_FILE, record)
    edit(repo, "value: 2.4", "value: 2.5")  # edited again after approval
    (repo / "src" / "params.hpp").write_text("constexpr double ROOT_CHORD = 2.5; // [m]\nconstexpr double WING_SPAN = 12.5; // [m]\n")
    report = run_ci_check(repo, Path("params.yaml"), "main")
    assert codes(report) == {"unapproved-edit"} and "edited without review: root_chord" in report.errors[0].message
    assert run_ci_check(repo, Path("params.yaml"), "main", approval_key="", public_keys="").errors[0].code == "approval-unverifiable"
    forged = sign_approval({"change_request": "cr-x", "fabric_hash": fabric_hash(load_fabric(repo / "params.yaml"))}, Signer.generate())
    write_lock(repo / LOCK_FILE, forged)  # correctly formed but signed by someone else's key
    assert codes(run_ci_check(repo, Path("params.yaml"), "main")) == {"unapproved-edit"}
    assert run_ci_check(repo, Path("params.yaml"), "main", approvals="warn").passed


def test_unit_mismatch_cycles_and_dimension_changes(repo):
    edit(repo, "unit: m/s", "unit: s")
    assert "dimension-change" in codes(run_ci_check(repo, Path("params.yaml"), "main", approvals="off", drift="off"))
    edit(repo, '"5 * root_chord"', '"5 * root_chord + inlet_velocity"')
    assert "unit-mismatch" in codes(run_ci_check(repo, Path("params.yaml"), "main", approvals="off", drift="off"))
    (repo / "params.yaml").write_text("""parameters:
  - {name: a, value: "b + 1", unit: m}
  - {name: b, value: "a + 1", unit: m}
""", encoding="utf-8")
    assert "circular-dependency" in codes(run_ci_check(repo, Path("params.yaml"), "main", approvals="off", drift="off"))
    (repo / "params.yaml").write_text("parameters: [{name: 1bad}]\n", encoding="utf-8")
    assert codes(run_ci_check(repo, Path("params.yaml"), "main")) == {"invalid-fabric"}


def test_committed_head_revision_and_new_fabric(repo):
    edit(repo, "value: 10", "value: 12")
    approve(repo)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "faster inlet")
    report = run_ci_check(repo, Path("params.yaml"), "main", head="HEAD", drift="off")
    assert report.passed and report.approval and [c["name"] for c in report.changes] == ["inlet_velocity"]
    assert report.simulation[0]["parameter"] == "inlet_velocity" and "0/U" in report.simulation[0]["effect"]
    git(repo, "checkout", "-q", "--orphan", "empty")
    git(repo, "rm", "-q", "-rf", ".")
    git(repo, "commit", "-q", "--allow-empty", "-m", "empty")
    report=run_ci_check(repo, Path("params.yaml"), "empty", head="feature", drift="off")
    assert {c["change"] for c in report.changes} == {"added"}  # fabric introduced by the pull request


def test_cli_exit_codes_and_outputs(repo, monkeypatch):
    monkeypatch.chdir(repo)
    runner=CliRunner()
    assert runner.invoke(app, ["ci-check", "--base", "main"]).exit_code == 0
    edit(repo, "value: 2.0", "value: 2.4")
    summary_file=repo / "summary.md"
    step_summary = repo / "step.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step_summary))
    result = runner.invoke(app, ["ci-check", "--base", "main", "--summary", str(summary_file), "--json", str(repo / "result.json")])
    assert result.exit_code == 1
    assert summary_file.read_text(encoding="utf-8").startswith(COMMENT_MARKER)
    assert step_summary.read_text(encoding="utf-8") == summary_file.read_text(encoding="utf-8")
    data = json.loads((repo / "result.json").read_text())
    assert data["passed"] is False and {f["code"] for f in data["findings"]} == {"unapproved-edit", "source-drift"}
    assert runner.invoke(app, ["ci-check", "--base", "main", "--approvals", "off", "--drift", "warn"]).exit_code == 0
    assert runner.invoke(app, ["ci-check", "--base", "main", "--approvals", "sometimes"]).exit_code == 2


def test_approval_records_sign_verify_and_keep_history(tmp_path):
    first = sign_approval({"change_request": "a", "fabric_hash": "1" * 64}, SIGNER)
    second = sign_approval({"change_request": "b", "fabric_hash": "2" * 64}, SIGNER)
    assert first["algorithm"] == "Ed25519" and first["key_id"] == SIGNER.key_id
    assert verify_approval(first, [SIGNER.public_key]) and not verify_approval(first, [Signer.generate().public_key])
    assert not verify_approval({**first, "fabric_hash": "3" * 64}, [SIGNER.public_key])
    assert not verify_approval(first, [])  # no trusted key, no approval
    write_lock(tmp_path / LOCK_FILE, first)
    write_lock(tmp_path / LOCK_FILE, second)
    write_lock(tmp_path / LOCK_FILE, second)  # re-issuing the same state does not duplicate it
    assert [r["change_request"] for r in load_lock(tmp_path / LOCK_FILE)] == ["b", "a"]
    import hashlib
    import hmac

    legacy = {"kind": "paramfabric.approval", "version": 1, "fabric_hash": "4" * 64}  # v0.5 HMAC record
    legacy["signature"] = "hmac-sha256:" + hmac.new(b"old shared secret", json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
    assert verify_approval(legacy, [SIGNER.public_key], legacy_secret="old shared secret")
    assert not verify_approval(legacy, [SIGNER.public_key])


def test_server_issues_approval_records_for_merged_requests(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from paramfabric.core.schema import Fabric
    from paramfabric.security.auth import issue_access_token
    from paramfabric.server.app import create_app

    monkeypatch.setenv("PARAMFABRIC_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "params.yaml").write_text("parameters: [{name: chord, value: 2, unit: m}]\n", encoding="utf-8")
    secret = "test signing secret with at least thirty-two bytes"
    client = TestClient(create_app(tmp_path / "params.yaml", database_url=f"sqlite:///{(tmp_path / 's.db').as_posix()}",
                                   jwt_secret=secret, audit_key="audit key with at least thirty-two bytes!!",
                                   audit_path=tmp_path / "audit.jsonl"))
    headers = {name: {"Authorization": f"Bearer {issue_access_token(name, [role], secret)}"}
               for name, role in (("engineer", "engineer"), ("approver", "approver"), ("admin", "admin"))}
    proposed = Fabric.model_validate({"parameters": [{"name": "chord", "value": 3, "unit": "m"}]})
    request = client.post("/api/change-requests", json={"proposed_fabric": proposed.model_dump(mode="json"), "rationale": "stiffer"},
                          headers=headers["engineer"]).json()
    assert client.get(f"/api/change-requests/{request['id']}/approval", headers=headers["admin"]).status_code == 409  # not merged
    client.post(f"/api/change-requests/{request['id']}/review", json={"decision": "approve"}, headers=headers["approver"])
    assert client.post(f"/api/change-requests/{request['id']}/merge", headers=headers["admin"]).status_code == 200
    record = client.get(f"/api/change-requests/{request['id']}/approval", headers=headers["engineer"]).json()
    from paramfabric.security.signing import load_public_keys

    published=client.get("/api/public/organizations/default/signing-keys?format=pem")  # no token: public
    assert published.status_code == 200 and record["key_id"] in client.get("/api/public/organizations/default/signing-keys").text
    assert verify_approval(record, load_public_keys(published.content)) and record["approvers"] == ["approver"]
    assert record["fabric_hash"] == fabric_hash(proposed)
