import json

import pytest

from paramfabric.security.audit import AuditLedger
from paramfabric.security.auth import AuthenticationError, decode_access_token, issue_access_token
from paramfabric.security.rbac import RBACPolicy


def test_jwt_verification_and_tag_scoped_rbac():
    secret = "test signing secret with sufficient bytes for HS256"
    token = issue_access_token("thermal-author", ["thermal-engineer"], secret)
    principal = decode_access_token(token, secret)
    policy = RBACPolicy(
        {"thermal-engineer": {"edit": ("thermal*",), "read": ("*",)}}
    )
    assert policy.allows(principal, "edit", ["thermal-fluid"])
    assert not policy.allows(principal, "edit", ["structures"])
    with pytest.raises(AuthenticationError):
        decode_access_token(token, "a different sufficiently long secret")


def test_audit_ledger_chain_and_tamper_detection(tmp_path):
    path=tmp_path / "audit.jsonl"
    ledger = AuditLedger(path, b"x" * 32)
    ledger.append({"actor": "user", "action": "parameter.updated"})
    ledger.append({"actor": "reviewer", "action": "request.approved"})
    assert ledger.verify()[0]
    assert len(ledger.records()) == 2

    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    record["event"]["actor"] = "attacker"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[0] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert not ledger.verify()[0]


def test_audit_anchor_detects_valid_prefix_truncation(tmp_path):
    path = tmp_path / "anchored.jsonl"
    ledger = AuditLedger(path, b"z" * 32)
    ledger.append({"action": "one"})
    ledger.append({"action": "two"})
    path.write_text(path.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
    assert not ledger.verify()[0]
