"""Sealed compliance reports: content, HTML/PDF rendering, tamper detection, CLI, and the Studio endpoint"""

import json
import re
import zlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paramfabric.cli import app
from paramfabric.core.schema import Fabric
from paramfabric.reporting.pdf_exporter import ReportError, collect_report_data, export_report, verify_report
from paramfabric.reporting.pdfwriter import PDFDocument, pdf_text
from paramfabric.security.audit import AuditLedger
from paramfabric.security.signing import Signer

KEY = b"report sealing key with at least 32 bytes!"  # audit-ledger HMAC key
SIGNER = Signer.generate()
PUBLIC = [SIGNER.public_key]
FABRIC = Fabric.model_validate({"parameters": [
    {"name": "root_chord", "value": 2.0, "unit": "m", "description": "Wing root chord", "rationale": "Sized for Re = 1e6",
     "tags": ["aero"], "bindings": {"cpp_header": "generated/params.hpp"}},
    {"name": "wing_span", "value": "5 * root_chord", "unit": "m", "description": "Span", "rationale": "Aspect ratio 5"},
    {"name": "inlet_velocity", "value": 10, "unit": "m/s", "description": "Free stream", "rationale": "Cruise"},
    {"name": "youngs_modulus", "value": 200, "unit": "GPa"},
]})


@pytest.fixture()
def workspace(tmp_path):
    def task(task_id, viewport, progress=None):
        folder=tmp_path / ".paramfabric" / "tasks" / task_id
        folder.mkdir(parents=True)
        (folder / "viewport.json").write_text(json.dumps(viewport))
        if progress:
            (folder / "progress.json").write_text(json.dumps(progress))

    task("cfd1", {"analysis": "cfd", "engine": "openfoam", "method": "OpenFOAM simpleFoam · kOmegaSST", "converged": True, "iterations": 120,
                  "summary": {"drag_coefficient": 1.128, "lift_coefficient": 0.002, "y_plus": {"min": 0.3, "mean": 4.1, "max": 11.9},
                              "max_velocity": 12.8, "convergence": "force coefficients steady", "reliable": True},
                  "mesh": {"quality": {"cells": 51509, "nodes": 11960, "cell_types": {"tet4": 45857, "prism6": 5652}, "grade": "fair"}}},
         [{"iteration": i, "fields": {"Ux": 0.95 ** i, "p": 0.5 * 0.97 ** i, "k": 0.1 * 0.96 ** i}} for i in range(1, 121)])
    task("fea1", {"analysis": "fea", "engine": "native3d", "method": "3D linear elasticity · tet10", "converged": True, "iterations": 1,
                  "summary": {"max_von_mises": 73.2e6, "max_displacement": 4.99e-4, "reaction_force": [0, 0, 1000.0], "dof": 22176},
                  "mesh": {"quality": {"cells": 4084, "grade": "fair"}}})
    return tmp_path


def test_report_data_covers_provenance_units_impact_and_simulations(workspace):
    ledger = AuditLedger(workspace / "audit.jsonl", KEY)
    ledger.append({"actor": "lead", "action": "change_request.merged"})
    data = collect_report_data(workspace, FABRIC, prepared_by="qa", ledger=ledger,
                               governance={"merged": [{"id": "abcd1234", "author": "eng", "approvers": ["lead"], "created_at": "2026-10-01T00:00:00", "rationale": "AR"}]})
    by_name = {p["name"]: p for p in data["parameters"]}
    assert by_name["wing_span"]["formula"] == "5 * root_chord" and by_name["wing_span"]["resolved"] == 10.0
    assert by_name["youngs_modulus"]["si"] == pytest.approx(2e11) and by_name["youngs_modulus"]["si_unit"] == "kg / m / s ** 2"
    assert by_name["root_chord"]["downstream"] == ["wing_span"] and by_name["root_chord"]["unit_status"] == "OK"
    assert "0/U" in by_name["inlet_velocity"]["simulation"]
    assert data["summary"]["traceability"] == "3/4" and data["summary"]["units_ok"]
    assert data["simulations"]["cfd"]["metrics"]["drag_coefficient"] == 1.128
    assert set(data["simulations"]["cfd"]["residuals"]) == {"Ux", "p", "k"} and data["simulations"]["fea"]["metrics"]["dof"] == 22176
    assert data["audit"]["valid"] and data["audit"]["records"] == 1
    restricted = collect_report_data(workspace, FABRIC, task_ids=["fea1"])
    assert set(restricted["simulations"]) == {"fea"}  # tenant-scoped servers pass their own task ids


def test_html_report_is_sealed_complete_and_tamper_evident(workspace):
    data = collect_report_data(workspace, FABRIC, prepared_by="qa")
    path = workspace / "audit_report.html"
    seal = export_report(path, data, SIGNER, signer="qa")
    html = path.read_text(encoding="utf-8")
    for section in ("Executive summary", "Parameter provenance", "Dimensional unit verification", "Impact analysis matrix",
                    "Simulation evidence", "Tamper-evident seal", "AS9100D", "Pint verified", "Drag coefficient Cd", "Max von Mises stress"):
        assert section in html, section
    assert "73.2 MPa" in html and html.count("<polyline") == 3  # three residual series
    assert seal["signature"] in html and seal["algorithm"] == "Ed25519" and verify_report(path, PUBLIC)["valid"]
    tampered = workspace / "tampered.html"
    tampered.write_text(html.replace('"drag_coefficient": 1.128', '"drag_coefficient": 0.9'), encoding="utf-8")
    check = verify_report(tampered, PUBLIC)
    assert not check["valid"] and not check["payload_intact"]
    wrong = verify_report(path, [Signer.generate().public_key])
    assert not wrong["valid"] and wrong["payload_intact"] and not wrong["signature_valid"]
    assert not verify_report(path, [])["valid"]  # the embedded public key is never trusted on its own
    # re-signing the edited data with an attacker's key does not help without the organization's public key
    forged=Signer.generate()
    forged_path = workspace / "forged.html"
    edited = json.loads(json.dumps(data))
    edited["summary"]["traceability"] = "4/4"
    export_report(forged_path, edited, forged, signer="qa")
    assert not verify_report(forged_path, PUBLIC)["valid"] and verify_report(forged_path, [forged.public_key])["valid"]
    with pytest.raises(ReportError):
        verify_report(_no_seal(workspace), PUBLIC)


def _no_seal(workspace):
    path = workspace / "plain.html"
    path.write_text("<html></html>", encoding="utf-8")
    return path


def _pdf_objects(raw: bytes) -> dict[int, bytes]:
    start = int(re.search(rb"startxref\s+(\d+)", raw).group(1))
    table = raw[start:].split(b"trailer")[0].split(b"\n")[2:]
    offsets = [int(line[:10]) for line in table if line.strip().endswith(b"n")]
    objects={}
    for number, offset in enumerate(offsets, start=1):
        assert raw[offset:].startswith(f"{number} 0 obj".encode()), number  # xref offsets point at their objects
        objects[number] = raw[offset:raw.index(b"endobj", offset)]
    return objects


def test_pdf_report_structure_text_and_seal(workspace):
    data = collect_report_data(workspace, FABRIC, prepared_by="qa")
    path = workspace / "audit_report.pdf"
    export_report(path, data, SIGNER)
    raw=path.read_bytes()
    assert raw.startswith(b"%PDF-1.7") and raw.rstrip().endswith(b"%%EOF")
    objects = _pdf_objects(raw)
    pages = [body for body in objects.values() if b"/Type /Page " in body]
    assert len(pages) >= 2 and all(b"/MediaBox [0 0 595.28 841.89]" in page for page in pages)
    text = b""
    for body in objects.values():
        if b"/FlateDecode" in body:
            stream = body.split(b"stream\n", 1)[1].rsplit(b"\nendstream", 1)[0]
            text += zlib.decompress(stream)
    for needle in (b"Executive summary", b"Pint verified", b"Impact analysis matrix", b"Tamper-evident seal", b"1.128", b"73.2 MPa",
                   b"Page 1 of", b"CFD residual convergence"):
        assert needle in text, needle
    assert verify_report(path, PUBLIC)["valid"]
    embedded=re.search(rb"/ParamfabricReport <([0-9a-f]+)>", raw).group(1)
    payload = json.loads(bytes.fromhex(embedded.decode()).decode("utf-8"))
    payload["simulations"]["cfd"]["metrics"]["drag_coefficient"] = 0.9  # edit a number, keep the seal
    forged = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8").hex().encode()
    (workspace / "tampered.pdf").write_bytes(raw.replace(embedded, forged))
    check = verify_report(workspace / "tampered.pdf", PUBLIC)
    assert not check["valid"] and not check["payload_intact"]


def test_pdf_text_encoding_and_pagination():
    assert pdf_text("y⁺ ≈ 1 (ω)") == b"y+ ~ 1 \\(omega\\)"
    assert pdf_text("±5 µm · 20 °C") == "±5 µm · 20 °C".encode("cp1252")
    document=PDFDocument("Long", header="h")
    document.table(["A", "B"], [[str(i), "x" * 300] for i in range(80)], [6, 80])
    assert len(document.pages) > 3
    assert document.render().count(b"/Type /Page ") == len(document.pages)


def test_cli_report_and_verify(workspace, monkeypatch):
    monkeypatch.chdir(workspace)
    for name in ("PARAMFABRIC_SIGNING_KEY", "PARAMFABRIC_SIGNING_KEYS_DIR", "PARAMFABRIC_REPORT_KEY"):
        monkeypatch.delenv(name, raising=False)
    (workspace / "params.yaml").write_text(json.dumps(FABRIC.model_dump(mode="json")), encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(app, ["keys", "generate", "--output", "keys/quality.pem"])
    assert result.exit_code == 0 and (workspace / "keys" / "quality.pub.pem").is_file()
    monkeypatch.setenv("PARAMFABRIC_SIGNING_KEY", str(workspace / "keys" / "quality.pem"))
    assert runner.invoke(app, ["keys", "public"]).output == (workspace / "keys" / "quality.pub.pem").read_text()
    for name in ("audit_report.html", "audit_report.pdf"):
        result = runner.invoke(app, ["report", "--output", name, "--fabric", "params.yaml", "--prepared-by", "Quality"])
        assert result.exit_code == 0, result.output
    # An auditor elsewhere: only the report and the public key, no secrets and no server
    monkeypatch.delenv("PARAMFABRIC_SIGNING_KEY")
    auditor=workspace / "auditor"
    auditor.mkdir()
    monkeypatch.chdir(auditor)
    for name in ("audit_report.html", "audit_report.pdf"):
        result = runner.invoke(app, ["report-verify", str(workspace / name), "--public-key", str(workspace / "keys" / "quality.pub.pem")])
        assert result.exit_code == 0 and "Signature valid" in result.output, result.output
    other = auditor / "other.pem"
    Signer.generate().save(other)
    result = runner.invoke(app, ["report-verify", str(workspace / "audit_report.html"), "--public-key", str(other.with_suffix(".pub.pem"))])
    assert result.exit_code == 1 and "not signed by any of the trusted keys" in result.output
    assert runner.invoke(app, ["report-verify", str(workspace / "audit_report.html")]).exit_code == 2  # no key given, none local


def test_studio_report_endpoint(tmp_path, monkeypatch):
    from tests.test_native3d import _client

    client=_client(tmp_path, monkeypatch)
    fabric = client.get("/api/local/fabric").json()
    fabric["parameters"] = FABRIC.model_dump(mode="json")["parameters"]
    assert client.post("/api/local/fabric", json=fabric).status_code == 200
    for fmt, media in (("html", "text/html"), ("pdf", "application/pdf")):
        response = client.get(f"/api/workbench/report?format={fmt}")
        assert response.status_code == 200 and response.headers["content-type"].startswith(media)
        assert "paramfabric-compliance-default-" in response.headers["content-disposition"] and response.headers["x-paramfabric-key-id"]
        path = tmp_path / f"report.{fmt}"
        path.write_bytes(response.content)
        from paramfabric.security.signing import load_public_keys

        public = client.get("/api/public/organizations/default/signing-keys?format=pem").content
        result=verify_report(path, load_public_keys(public))
        assert result["valid"] and result["seal"]["signer"] == "local-admin"
        checked = client.post("/api/public/organizations/default/verify-report", content=response.content).json()
        assert checked["valid"] and checked["verified_key_id"] == response.headers["x-paramfabric-key-id"]
    envelope = client.get("/api/workbench/report?format=pdf&download=1").json()  # the Studio's download path
    import base64

    assert envelope["filename"].endswith(".pdf") and base64.b64decode(envelope["content_base64"]).startswith(b"%PDF-1.7")
    assert client.get("/api/workbench/report?format=docx").status_code == 422


@pytest.mark.parametrize("fmt", ["pdf", "html"])
def test_edits_to_the_visible_document_fail_verification(workspace, fmt):
    data = collect_report_data(workspace, FABRIC, prepared_by="qa")
    path = workspace / f"report.{fmt}"
    export_report(path, data, SIGNER, signer="qa")
    genuine = verify_report(path, PUBLIC)
    assert genuine["valid"] and genuine["document_intact"] is True
    raw=path.read_bytes()
    if fmt == "pdf":
        edited=raw.replace(b"/Author (Paramfabric)", b"/Author (Paramfabrik)", 1)  # same length, data untouched
    else:
        head, _, tail = raw.partition(b'<script type="application/json"')
        edited = head.replace(b"Executive summary", b"Executive  summary", 1) + _ + tail  # visible text only
    assert edited != raw
    path.write_bytes(edited)
    result = verify_report(path, PUBLIC)
    assert result["payload_intact"] and result["signature_valid"]  # the embedded data still verifies...
    assert result["document_intact"] is False and not result["valid"]  # ...but the document does not
