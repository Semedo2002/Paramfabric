"""Compliance and executive audit reports (HTML and PDF) with an Ed25519 signature seal.

A report is built in two steps:

1. :func:`collect_report_data` gathers a JSON-serializable snapshot: fabric
   provenance (description, rationale, tags, bindings), Pint unit
   verification per parameter, the dependency/impact matrix (downstream
   parameters, generated targets, CAD features, simulation inputs, source
   constants), governance history (merged change requests and approvers,
   baselines), audit-ledger verification, and the latest CFD/FEA results
   (mesh statistics, Cd/Cl, y+, max von Mises, convergence history).
2. :func:`seal` signs the canonical JSON of that snapshot with the
   organization's **Ed25519 private key**. Both renderers draw *only* from the
   sealed snapshot and embed it (HTML: a JSON script block; PDF: metadata
   keys), so :func:`verify_report` can prove, with nothing but the
   organization's **public key**, that the numbers were produced by that
   organization's Paramfabric and not edited afterwards.

The public key is published by the server (`GET
/api/public/organizations/<org>/signing-keys`, no account needed) and with
`paramfabric keys public`; auditors run `paramfabric report-verify report.pdf
--public-key org.pub.pem`. Only the private-key holder can create seals.

The seal supports ISO 9001 (7.5 documented information, 8.3 design and
development controls) and AS9100 (8.3.4 verification, 8.1.3 configuration
management) evidence packages. v0.5 reports sealed with HMAC-SHA256 still
verify with `--legacy-key`.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import networkx as nx

import paramfabric
from paramfabric.core.diagnostics import diagnose
from paramfabric.core.graph import ParameterGraph
from paramfabric.core.schema import Fabric
from paramfabric.governance.pr_engine import fabric_hash
from paramfabric.reporting.pdfwriter import PDFDocument

SERIES = ["#2f6fbf", "#c4511f", "#178a63", "#a87000", "#b8457a", "#4b6b00"]
SEAL_ALGORITHM = "Ed25519"
LEGACY_ALGORITHM = "HMAC-SHA256"
_PAYLOAD_ID = "paramfabric-report-data"
_SEAL_ID = "paramfabric-report-seal"


class ReportError(ValueError):
    """Raised when a report cannot be produced or verified"""


# ---------------------------------------------------------------------------- collection
def _canonical(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def _simulations(root: Path, task_ids: list[str] | None) -> dict[str, Any]:
    tasks_dir = root / ".paramfabric" / "tasks"
    if not tasks_dir.is_dir():
        return {}
    candidates = []
    for viewport in tasks_dir.glob("*/viewport.json"):
        if task_ids is not None and viewport.parent.name not in task_ids:
            continue
        candidates.append((viewport.stat().st_mtime, viewport))
    latest: dict[str, Any] = {}
    for _, path in sorted(candidates, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        analysis = data.get("analysis")
        if analysis not in {"cfd", "fea"} or analysis in latest or "summary" not in data:
            continue
        summary=data.get("summary") or {}
        mesh=(data.get("mesh") or {}).get("quality") or {}
        entry: dict[str, Any] = {
            "task_id": path.parent.name,
            "finished_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            "engine": data.get("engine"), "method": data.get("method"),
            "converged": data.get("converged"), "iterations": data.get("iterations"),
            "reliable": summary.get("reliable", True), "warnings": summary.get("warnings", []),
            "mesh": {key: mesh.get(key) for key in ("cells", "nodes", "cell_types", "grade", "min_tet_quality", "mean_tet_quality", "boundary_layers", "first_layer_height") if key in mesh},
        }
        if analysis == "cfd":
            entry["metrics"] = {key: summary.get(key) for key in (
                "drag_coefficient", "lift_coefficient", "side_coefficient", "drag", "pressure_drop", "max_velocity",
                "pressure_range", "y_plus", "turbulence", "convergence", "mass_imbalance") if summary.get(key) is not None}
        else:
            entry["metrics"] = {key: summary.get(key) for key in (
                "max_von_mises", "max_displacement", "reaction_force", "elements", "dof", "relative_residual", "stress_measure")
                if summary.get(key) is not None}
        progress_path = path.parent / "progress.json"
        if progress_path.is_file():
            try:
                progress = json.loads(progress_path.read_text(encoding="utf-8"))
            except ValueError:
                progress=[]
            stride = max(1, len(progress) // 400)
            history: dict[str, list[list[float]]] = {}
            for item in progress[::stride] + ([progress[-1]] if progress and (len(progress) - 1) % stride else []):
                for name, value in item.get("fields", {}).items():
                    if isinstance(value, (int, float)) and value > 0 and math.isfinite(value):
                        history.setdefault(name, []).append([item["iteration"], float(value)])
            entry["residuals"] = history
        latest[analysis] = entry
    return latest


def collect_report_data(
    root: Path,
    fabric: Fabric,
    *,
    title: str = "Engineering Parameter Compliance Report",
    organization: str = "default",
    project: str = "default",
    governance: dict[str, Any] | None = None,
    ledger: Any = None,
    task_ids: list[str] | None = None,
    prepared_by: str = "paramfabric",
) -> dict[str, Any]:
    """Snapshot everything the report shows (JSON-serializable; this is what gets sealed)"""
    from paramfabric.ci import SIMULATION_INPUTS, _cad_references

    root = Path(root).resolve()
    report = diagnose(fabric)
    issues_by_parameter: dict[str, list[str]] = {}
    for issue in report["issues"]:
        issues_by_parameter.setdefault(issue.get("parameter") or "", []).append(issue["message"])
    graph = nx.DiGraph()
    graph.add_nodes_from(fabric.by_name())
    graph.add_edges_from((link["source"], link["target"]) for link in report["links"])
    cad = _cad_references(root, set(fabric.by_name()))
    code: dict[str, list[str]] = {}
    if report["ok"]:
        try:
            from paramfabric.auditors.sync import SyncEngine

            for item in SyncEngine(root, exclude=[Path(paramfabric.__file__).resolve().parent]).status(fabric, report["quantities"]):
                code.setdefault(item["parameter"], []).append(f"{item['path']}:{item['line']} {item['name']} ({item['state'].replace('_', ' ')})")
        except Exception:  # noqa: BLE001 - source scanning is best effort
            code = {}

    parameters = []
    for parameter in fabric.parameters:
        resolved = report["resolved"].get(parameter.name, {})
        problems = issues_by_parameter.get(parameter.name, [])
        parameters.append({
            "name": parameter.name, "value": parameter.value, "unit": parameter.unit, "type": parameter.type,
            "formula": parameter.value if isinstance(parameter.value, str) and parameter.type != "str" else None,
            "resolved": resolved.get("value"), "si": resolved.get("si"), "si_unit": resolved.get("si_unit"),
            "dimensionality": resolved.get("dimensionality"), "description": parameter.description,
            "rationale": parameter.rationale, "tags": parameter.tags, "bindings": parameter.bindings,
            "unit_status": "FAIL" if problems else "OK", "issues": problems,
            "depends_on": sorted(graph.predecessors(parameter.name)) if parameter.name in graph else [],
            "downstream": sorted(nx.descendants(graph, parameter.name)) if parameter.name in graph else [],
            "cad": [ref["description"] for ref in cad if ref["parameter"] == parameter.name],
            "simulation": SIMULATION_INPUTS.get(parameter.name),
            "code": code.get(parameter.name, []),
        })
    audit: dict[str, Any] | None = None
    if ledger is not None:
        valid, head = ledger.verify()
        records = ledger.records() if valid else []
        audit = {"valid": bool(valid), "head": head, "records": len(records),
                 "recent": [{k: r.get(k) for k in ("timestamp", "actor", "action")} for r in records[-12:]]}
    traced = [p for p in parameters if p["description"] and p["rationale"]]
    return {
        "schema": "paramfabric.compliance-report/1",
        "title": title,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": f"Paramfabric {paramfabric.__version__}",
        "prepared_by": prepared_by,
        "organization": organization,
        "project": project,
        "workspace": root.name,
        "fabric_hash": fabric_hash(fabric),
        "summary": {
            "parameters": len(parameters),
            "formulas": sum(1 for p in parameters if p["formula"]),
            "units_ok": report["units_ok"], "dag_ok": report["dag_ok"], "ok": report["ok"],
            "issues": [issue["message"] for issue in report["issues"]],
            "dependencies": len(report["links"]),
            "bound_targets": sum(len(p["bindings"]) for p in parameters),
            "cad_references": len(cad),
            "traceability": f"{len(traced)}/{len(parameters)}",
        },
        "parameters": parameters,
        "governance": governance or {},
        "audit": audit,
        "simulations": _simulations(root, task_ids),
    }


# ---------------------------------------------------------------------------- sealing
def seal(data: dict[str, Any], signer: Any, signed_by: str = "paramfabric") -> dict[str, Any]:
    canonical = _canonical(data)
    return {
        "algorithm": SEAL_ALGORITHM,
        "payload_sha256": hashlib.sha256(canonical).hexdigest(),
        "signature": signer.sign(canonical),
        "key_id": signer.key_id,
        "public_key": signer.public_pem,  # informational: verify against a key you trust, not this one
        "signed_at": datetime.now(timezone.utc).isoformat(),
        "signer": signed_by,
    }


def _check(data: dict[str, Any], seal_record: dict[str, Any], public_keys: list[Any] | None, legacy_key: bytes | None) -> dict[str, Any]:
    from paramfabric.security.signing import verify

    canonical = _canonical(data)
    digest_ok = hashlib.sha256(canonical).hexdigest() == seal_record.get("payload_sha256")
    trusted_key = None
    if seal_record.get("algorithm") == LEGACY_ALGORITHM:
        signature_ok = bool(legacy_key) and hmac.compare_digest(
            hmac.new(legacy_key or b"", canonical, hashlib.sha256).hexdigest(), str(seal_record.get("mac", "")))
    else:
        trusted_key = verify(public_keys or [], canonical, str(seal_record.get("signature", "")))
        signature_ok = trusted_key is not None
    return {"valid": digest_ok and signature_ok, "payload_intact": digest_ok, "signature_valid": signature_ok,
            "verified_key_id": trusted_key, "key_matches": trusted_key is not None and trusted_key == seal_record.get("key_id"),
            "algorithm": seal_record.get("algorithm"), "seal": seal_record,
            "fabric_hash": data.get("fabric_hash"), "generated_at": data.get("generated_at")}


def read_report(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """The embedded data and seal of an exported HTML or PDF report"""
    raw = Path(path).read_bytes()
    if raw.startswith(b"%PDF"):
        found = dict(re.findall(rb"/(ParamfabricReport|ParamfabricSeal) <([0-9a-f]+)>", raw))
        if b"ParamfabricReport" not in found or b"ParamfabricSeal" not in found:
            raise ReportError("the PDF carries no Paramfabric seal")
        return (json.loads(bytes.fromhex(found[b"ParamfabricReport"].decode()).decode("utf-8")),
                json.loads(bytes.fromhex(found[b"ParamfabricSeal"].decode()).decode("utf-8")))
    text = raw.decode("utf-8", errors="replace")
    blocks = {name: re.search(rf'<script type="application/json" id="{name}">(.*?)</script>', text, re.DOTALL) for name in (_PAYLOAD_ID, _SEAL_ID)}
    if not all(blocks.values()):
        raise ReportError("the HTML carries no Paramfabric seal")
    return json.loads(html.unescape(blocks[_PAYLOAD_ID].group(1))), json.loads(html.unescape(blocks[_SEAL_ID].group(1)))


def verify_report(path: Path, public_keys: list[Any] | None = None, *, legacy_key: bytes | None = None) -> dict[str, Any]:
    """Verify an exported HTML or PDF report against trusted public keys (or a v0.5 HMAC secret).

    The signature covers the embedded data. The visible document is checked too: rendering is
    deterministic, so the file must equal the document re-rendered from the signed data. Any edit
    to the pages, text, or metadata fails, even when the embedded data is left untouched.
    """
    raw = Path(path).read_bytes()
    data, seal_record = read_report(path)
    result = _check(data, seal_record, public_keys, legacy_key)
    result["document_intact"] = _document_intact(raw, data, seal_record)
    if result["document_intact"] is False:
        result["valid"] = False
    return result


def _document_intact(raw: bytes, data: dict[str, Any], seal_record: dict[str, Any]) -> bool | None:
    if seal_record.get("algorithm") != SEAL_ALGORITHM or data.get("generator") != f"Paramfabric {paramfabric.__version__}":
        return None  # rendered by another release: only the signed data can be checked
    try:
        expected = render_pdf(data, seal_record) if raw.startswith(b"%PDF") else render_html(data, seal_record).encode("utf-8")
    except (KeyError, TypeError, ValueError):
        return False
    return raw.replace(b"\r\n", b"\n") == expected.replace(b"\r\n", b"\n")  # tolerate CRLF from text-mode saves


# ---------------------------------------------------------------------------- formatting
def _fmt(value: Any, digits: int = 5) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        if value == 0:
            return "0"
        return f"{value:.{digits}g}"
    if isinstance(value, (list, tuple)):
        return "(" + ", ".join(_fmt(v, 4) for v in value) + ")"
    if isinstance(value, dict):
        return ", ".join(f"{k} {_fmt(v, 4)}" for k, v in value.items())
    return str(value)


METRIC_LABELS = {
    "drag_coefficient": ("Drag coefficient Cd", ""), "lift_coefficient": ("Lift coefficient Cl", ""),
    "side_coefficient": ("Side coefficient Cs", ""), "drag": ("Drag force", "N"), "pressure_drop": ("Pressure drop", "Pa"),
    "max_velocity": ("Max velocity", "m/s"), "pressure_range": ("Pressure range", "Pa"), "y_plus": ("Wall y⁺", ""),
    "turbulence": ("Turbulence model", ""), "convergence": ("Convergence criterion", ""), "mass_imbalance": ("Mass imbalance", ""),
    "max_von_mises": ("Max von Mises stress", "MPa"), "max_displacement": ("Max displacement", "mm"),
    "reaction_force": ("Reaction force", "N"), "elements": ("Elements", ""), "dof": ("Degrees of freedom", ""),
    "relative_residual": ("Relative residual", ""), "stress_measure": ("Stress measure", ""),
}


def _metric(name: str, value: Any) -> tuple[str, str]:
    label, unit = METRIC_LABELS.get(name, (name, ""))
    if name == "max_von_mises" and isinstance(value, (int, float)):
        value = value / 1e6
    if name == "max_displacement" and isinstance(value, (int, float)):
        value=value * 1e3
    return label, f"{_fmt(value)} {unit}".strip()


def _findings(data: dict[str, Any]) -> list[tuple[str, str]]:
    s = data["summary"]
    findings = [
        ("PASS" if s["units_ok"] else "FAIL", f"Dimensional consistency (Pint) across {s['parameters']} parameters and {s['formulas']} formulas"),
        ("PASS" if s["dag_ok"] else "FAIL", f"Dependency graph acyclic ({s['dependencies']} dependencies)"),
        ("PASS" if s["ok"] else "FAIL", "All parameters resolve" if s["ok"] else "; ".join(s["issues"][:3])),
        ("INFO", f"Traceability: {s['traceability']} parameters carry both a description and a design rationale"),
    ]
    audit = data.get("audit")
    if audit is not None:
        findings.append(("PASS" if audit["valid"] else "FAIL", f"Audit ledger hash chain {'verified' if audit['valid'] else 'BROKEN'} ({audit['records']} records)"))
    merged = data.get("governance", {}).get("merged", [])
    if merged:
        findings.append(("INFO", f"{len(merged)} reviewed change request(s) merged; latest by {merged[0].get('merged_by') or merged[0].get('author')}"))
    for analysis, label in (("cfd", "CFD"), ("fea", "FEA")):
        sim = data["simulations"].get(analysis)
        if sim:
            status="PASS" if sim.get("converged") and sim.get("reliable", True) else "WARN"
            findings.append((status, f"{label}: {sim.get('method')}, {'converged' if sim.get('converged') else 'not converged'} after {sim.get('iterations')} iteration(s)"))
    return findings


# ---------------------------------------------------------------------------- HTML
def _svg_plot(series: dict[str, list[list[float]]], width: int = 720, height: int = 260) -> str:
    points = [(x, y) for values in series.values() for x, y in values if y > 0]
    if not points:
        return ""
    left, right, top, bottom = 56, width - 12, 26, height - 34
    xs = [p[0] for p in points]
    ys=[math.log10(p[1]) for p in points]
    x0, x1 = min(xs), max(xs) or 1
    y0, y1 = math.floor(min(ys)), math.ceil(max(ys))
    y1 = y1 if y1 > y0 else y0 + 1
    sx = lambda x: left + (x - x0) / ((x1 - x0) or 1) * (right - left)  # noqa: E731
    sy = lambda y: bottom - (y - y0) / (y1 - y0) * (bottom - top)  # noqa: E731
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Residual convergence (log scale)" class="plot">']
    for decade in range(int(y0), int(y1) + 1):
        parts.append(f'<line x1="{left}" x2="{right}" y1="{sy(decade):.1f}" y2="{sy(decade):.1f}" class="grid"/>'
                     f'<text x="{left - 6}" y="{sy(decade) + 3:.1f}" text-anchor="end" class="tick">1e{decade}</text>')
    for k in range(6):
        xv = x0 + k * (x1 - x0) / 5
        parts.append(f'<text x="{sx(xv):.1f}" y="{bottom + 14}" text-anchor="middle" class="tick">{xv:.0f}</text>')
    parts.append(f'<line x1="{left}" x2="{right}" y1="{bottom}" y2="{bottom}" class="axis"/>')
    legend_x = left
    for (name, values), color in zip(series.items(), SERIES):
        path = " ".join(f"{sx(x):.1f},{sy(math.log10(y)):.1f}" for x, y in values if y > 0)
        parts.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round"><title>{html.escape(name)}</title></polyline>')
        parts.append(f'<rect x="{legend_x}" y="8" width="10" height="3" rx="1.5" fill="{color}"/><text x="{legend_x + 14}" y="12" class="legend">{html.escape(name)}</text>')
        legend_x += 24 + 7 * len(name)
    parts.append(f'<text x="{(left + right) / 2}" y="{height - 4}" text-anchor="middle" class="tick">Iteration · initial residual (log scale)</text></svg>')
    return "".join(parts)


def render_html(data: dict[str, Any], seal_record: dict[str, Any]) -> str:
    e = html.escape
    status_class = {"PASS": "ok", "OK": "ok", "FAIL": "bad", "WARN": "warn", "INFO": "info"}

    def table(headers: list[str], rows: list[list[str]], classes: list[str] | None = None) -> str:
        head="".join(f"<th>{e(h)}</th>" for h in headers)
        body = "".join("<tr>" + "".join(f'<td class="{(classes or [""] * len(r))[i]}">{c}</td>' for i, c in enumerate(r)) + "</tr>" for r in rows)
        return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'

    s = data["summary"]
    findings = "".join(f'<li><span class="badge {status_class.get(k, "info")}">{k}</span>{e(text)}</li>' for k, text in _findings(data))
    provenance = table(
        ["Parameter", "Value", "Unit", "Description", "Design rationale", "Tags"],
        [[f"<code>{e(p['name'])}</code>", e(_fmt(p["value"])), e(p["unit"]), e(p["description"] or "—"), e(p["rationale"] or "—"), e(", ".join(p["tags"]) or "—")] for p in data["parameters"]],
    )
    units = table(
        ["Parameter", "Declared", "Resolved", "SI base", "Dimensionality", "Status"],
        [[f"<code>{e(p['name'])}</code>", e(f"{_fmt(p['value'])} {p['unit']}"), e(_fmt(p["resolved"])),
          e(f"{_fmt(p['si'])} {p['si_unit'] or ''}".strip()), e(p["dimensionality"] or "—"),
          f'<span class="badge {"ok" if p["unit_status"] == "OK" else "bad"}">{"Pint verified" if p["unit_status"] == "OK" else "FAIL"}</span>'
          + (f'<div class="issue">{e("; ".join(p["issues"]))}</div>' if p["issues"] else "")]
         for p in data["parameters"]],
    )
    impact=table(
        ["Parameter", "Depends on", "Downstream parameters", "Generated targets", "CAD features", "Simulation input", "Source constants"],
        [[f"<code>{e(p['name'])}</code>", e(", ".join(p["depends_on"]) or "—"), e(", ".join(p["downstream"]) or "—"),
          e("; ".join(f"{k} → {v}" for k, v in p["bindings"].items()) or "—"), e("; ".join(p["cad"]) or "—"),
          e(p["simulation"] or "—"), e("; ".join(p["code"]) or "—")] for p in data["parameters"]],
    )
    simulations = []
    for analysis, label in (("cfd", "CFD (OpenFOAM / native)"), ("fea", "3D FEA")):
        sim = data["simulations"].get(analysis)
        if not sim:
            simulations.append(f"<h3>{label}</h3><p class=\"muted\">No completed {analysis.upper()} run in this workspace.</p>")
            continue
        rows = [["Engine", e(_fmt(sim["engine"]))], ["Method", e(_fmt(sim["method"]))], ["Converged", e(_fmt(sim["converged"]))],
                ["Iterations", e(_fmt(sim["iterations"]))], ["Task", f"<code>{e(sim['task_id'])}</code> · {e(sim['finished_at'][:19])}Z"]]
        rows += [[e(k.replace("_", " ").capitalize()), e(_fmt(v))] for k, v in sim["mesh"].items()]
        rows += [list(map(e, _metric(k, v))) for k, v in sim["metrics"].items()]
        warn = "".join(f'<p class="issue">{e(w)}</p>' for w in sim.get("warnings") or [])
        plot = _svg_plot(sim.get("residuals") or {})
        simulations.append(f"<h3>{label}</h3>{table(['Quantity', 'Value'], rows)}{warn}{plot}")
    governance = data.get("governance") or {}
    merged = governance.get("merged", [])
    governance_html=table(
        ["Change request", "Author", "Approvers", "Merged", "Rationale"],
        [[f"<code>{e(r['id'][:8])}</code>", e(r.get("author") or "—"), e(", ".join(r.get("approvers") or []) or "—"), e((r.get("created_at") or "")[:19]), e(r.get("rationale") or "")] for r in merged],
    ) if merged else '<p class="muted">No server-side change requests (local workspace or none merged).</p>'
    baselines = governance.get("baselines", [])
    if baselines:
        governance_html += table(["Baseline", "Fabric hash", "Created by", "Created"], [[e(b["tag"]), f"<code>{e(b['hash'][:16])}</code>", e(b["created_by"]), e(b["created_at"][:19])] for b in baselines])
    audit = data.get("audit")
    audit_html=(f'<p><span class="badge {"ok" if audit["valid"] else "bad"}">{"VERIFIED" if audit["valid"] else "BROKEN"}</span> '
                  f'Hash-chained ledger with {audit["records"]} records; head <code>{e(str(audit["head"])[:24])}</code>.</p>'
                  + table(["Time", "Actor", "Action"], [[e(str(r.get("timestamp"))[:19]), e(str(r.get("actor"))), e(str(r.get("action")))] for r in audit["recent"]])
                  ) if audit else '<p class="muted">Audit ledger not included.</p>'
    payload = e(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str), quote=False)
    seal_json = e(json.dumps(seal_record, sort_keys=True), quote=False)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(data['title'])}</title>
<style>
:root {{ --ink:#1d2733; --muted:#5b6876; --line:#d9e2ec; --head:#0d2b52; --accent:#2f6fbf; --soft:#eef4fb; --paper:#ffffff;
  --ok:#0b7a0b; --ok-bg:#e7f5e7; --bad:#b42318; --bad-bg:#fdecea; --warn:#8a5a00; --warn-bg:#fff4dc; --info:#2f6fbf; --info-bg:#eaf1fb; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--paper); color:var(--ink); font:13px/1.5 "Segoe UI", system-ui, -apple-system, sans-serif; }}
main {{ max-width:1080px; margin:0 auto; padding:32px 24px 48px; }}
header.cover {{ border-bottom:3px solid var(--head); padding-bottom:16px; margin-bottom:20px; }}
.kicker {{ font:600 11px/1 ui-monospace, Consolas, monospace; letter-spacing:.14em; color:var(--muted); text-transform:uppercase; }}
h1 {{ margin:6px 0 8px; font-size:26px; color:var(--head); }} h2 {{ margin:28px 0 10px; font-size:17px; color:var(--head); border-bottom:1px solid var(--line); padding-bottom:4px; }}
h3 {{ margin:18px 0 8px; font-size:14px; }}
.meta {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(190px, 1fr)); gap:6px 18px; color:var(--muted); font-size:12px; }}
.meta b {{ color:var(--ink); font-weight:600; }}
.kpis {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(140px, 1fr)); gap:10px; margin:14px 0; }}
.kpi {{ border:1px solid var(--line); border-radius:8px; padding:10px 12px; }} .kpi strong {{ display:block; font-size:22px; color:var(--head); font-variant-numeric:tabular-nums; }}
.kpi span {{ color:var(--muted); font-size:12px; }}
ul.findings {{ list-style:none; padding:0; margin:0; display:grid; gap:6px; }}
.badge {{ display:inline-block; min-width:62px; text-align:center; font:600 10.5px/1.7 ui-monospace, Consolas, monospace; border-radius:4px; padding:0 6px; margin-right:8px; }}
.badge.ok {{ color:var(--ok); background:var(--ok-bg); }} .badge.bad {{ color:var(--bad); background:var(--bad-bg); }}
.badge.warn {{ color:var(--warn); background:var(--warn-bg); }} .badge.info {{ color:var(--info); background:var(--info-bg); }}
.table-wrap {{ overflow-x:auto; margin:8px 0 14px; }}
table {{ border-collapse:collapse; width:100%; font-size:12px; }} th, td {{ text-align:left; vertical-align:top; padding:6px 8px; border-bottom:1px solid var(--line); }}
th {{ background:var(--soft); color:var(--head); font-weight:600; position:sticky; top:0; }} tbody tr:nth-child(even) td {{ background:#fafcfe; }}
code {{ font:12px ui-monospace, Consolas, monospace; }} .muted {{ color:var(--muted); }} .issue {{ color:var(--bad); font-size:11.5px; margin-top:2px; }}
svg.plot {{ width:100%; max-width:760px; height:auto; display:block; margin:6px 0 16px; }}
svg .grid {{ stroke:#e3e9f0; stroke-width:1; }} svg .axis {{ stroke:#8796a5; }} svg .tick, svg .legend {{ fill:#5b6876; font:11px ui-monospace, Consolas, monospace; }}
.seal {{ border:2px solid var(--head); border-radius:10px; padding:14px 16px; margin-top:28px; background:var(--soft); }}
.seal dl {{ display:grid; grid-template-columns:max-content 1fr; gap:4px 14px; margin:8px 0 0; }} .seal dt {{ color:var(--muted); }} .seal dd {{ margin:0; font:12px ui-monospace, Consolas, monospace; word-break:break-all; }}
@media (max-width:640px) {{ main {{ padding:20px 16px; }} h1 {{ font-size:21px; }} }}
@media print {{ @page {{ size:A4; margin:16mm 14mm; }} main {{ max-width:none; padding:0; }} h2 {{ break-after:avoid; }} tr, .seal, svg {{ break-inside:avoid; }} th {{ position:static; }} }}
</style></head>
<body><main>
<header class="cover">
  <div class="kicker">Compliance &amp; executive audit report</div>
  <h1>{e(data['title'])}</h1>
  <div class="meta">
    <span><b>Organization</b> {e(data['organization'])}</span><span><b>Project</b> {e(data['project'])}</span>
    <span><b>Workspace</b> {e(data['workspace'])}</span><span><b>Generated</b> {e(data['generated_at'][:19])}Z</span>
    <span><b>Prepared by</b> {e(data['prepared_by'])}</span><span><b>Generator</b> {e(data['generator'])}</span>
    <span><b>Fabric hash</b> <code>{e(data['fabric_hash'][:16])}…</code></span>
  </div>
</header>
<section aria-labelledby="exec"><h2 id="exec">1. Executive summary</h2>
  <div class="kpis">
    <div class="kpi"><strong>{s['parameters']}</strong><span>Governed parameters</span></div>
    <div class="kpi"><strong>{s['formulas']}</strong><span>Derived formulas</span></div>
    <div class="kpi"><strong>{s['dependencies']}</strong><span>Dependencies</span></div>
    <div class="kpi"><strong>{s['bound_targets']}</strong><span>Generated targets</span></div>
    <div class="kpi"><strong>{s['cad_references']}</strong><span>CAD references</span></div>
  </div>
  <ul class="findings">{findings}</ul>
</section>
<section aria-labelledby="prov"><h2 id="prov">2. Parameter provenance</h2>{provenance}</section>
<section aria-labelledby="units"><h2 id="units">3. Dimensional unit verification</h2>
  <p class="muted">Every value and formula is evaluated with Pint; mixed-dimension arithmetic is rejected. SI base values are shown for independent checking.</p>{units}</section>
<section aria-labelledby="impact"><h2 id="impact">4. Impact analysis matrix</h2>
  <p class="muted">For each parameter: its inputs, the parameters that change with it, and the CAD, simulation, generated, and source-code artifacts bound to it.</p>{impact}</section>
<section aria-labelledby="sim"><h2 id="sim">5. Simulation evidence</h2>{''.join(simulations)}</section>
<section aria-labelledby="gov"><h2 id="gov">6. Configuration management &amp; review</h2>{governance_html}<h3>Audit ledger</h3>{audit_html}</section>
<section class="seal" aria-labelledby="sealh"><h2 id="sealh" style="border:0;margin:0">7. Tamper-evident seal</h2>
  <p>This report's data (embedded below in machine-readable form) is signed with the organization's {e(seal_record['algorithm'])} private key. Any change to a value invalidates the signature.
  Anyone can verify it with the organization's <b>public</b> key: <code>paramfabric report-verify &lt;file&gt; --public-key org.pub.pem</code>.</p>
  <dl><dt>Algorithm</dt><dd>{e(seal_record['algorithm'])} (RFC 8032)</dd><dt>Payload SHA-256</dt><dd>{e(seal_record['payload_sha256'])}</dd>
  <dt>Signature</dt><dd>{e(seal_record['signature'])}</dd><dt>Signing key id</dt><dd>{e(seal_record['key_id'])}</dd>
  <dt>Signed</dt><dd>{e(seal_record['signed_at'])} by {e(seal_record['signer'])}</dd>
  <dt>Standards</dt><dd>ISO 9001:2015 §7.5, §8.3 · AS9100D §8.1.3, §8.3.4 (documented information, design verification, configuration management)</dd></dl>
</section>
</main>
<script type="application/json" id="{_PAYLOAD_ID}">{payload}</script>
<script type="application/json" id="{_SEAL_ID}">{seal_json}</script>
</body></html>
"""


# ---------------------------------------------------------------------------- PDF
def render_pdf(data: dict[str, Any], seal_record: dict[str, Any]) -> bytes:
    doc=PDFDocument(data["title"], header=f"{data['organization']} / {data['project']} · {data['generated_at'][:10]}",
                      created=datetime.fromisoformat(seal_record["signed_at"]))
    doc.y -= 6
    doc.text(48, doc.y - 10, "COMPLIANCE & EXECUTIVE AUDIT REPORT", 8.5, "bold", "#5b6876")
    doc.y -= 16
    doc.text(48, doc.y - 18, data["title"], 18, "bold", "#0d2b52")
    doc.y -= 30
    doc.key_values([
        ("Organization / project", f"{data['organization']} / {data['project']}"), ("Workspace", data["workspace"]),
        ("Generated", data["generated_at"]), ("Prepared by", data["prepared_by"]), ("Generator", data["generator"]),
        ("Fabric hash (SHA-256)", data["fabric_hash"]),
    ])
    s = data["summary"]
    doc.heading("1. Executive summary")
    doc.paragraph(f"{s['parameters']} governed parameters · {s['formulas']} formulas · {s['dependencies']} dependencies · "
                  f"{s['bound_targets']} generated targets · {s['cad_references']} CAD references", 8.5)
    doc.table(["Status", "Finding"], [[k, text] for k, text in _findings(data)], [8, 84], 8, status_column=0)
    doc.heading("2. Parameter provenance")
    doc.table(["Parameter", "Value", "Unit", "Description", "Rationale", "Tags"],
              [[p["name"], _fmt(p["value"]), p["unit"], p["description"] or "-", p["rationale"] or "-", ", ".join(p["tags"]) or "-"] for p in data["parameters"]],
              [18, 14, 9, 24, 24, 12])
    doc.heading("3. Dimensional unit verification (Pint)")
    doc.table(["Parameter", "Declared", "Resolved", "SI base", "Dimensionality", "Status"],
              [[p["name"], f"{_fmt(p['value'])} {p['unit']}", _fmt(p["resolved"]), f"{_fmt(p['si'])} {p['si_unit'] or ''}", p["dimensionality"] or "-",
                "OK (Pint verified)" if p["unit_status"] == "OK" else "FAIL: " + "; ".join(p["issues"])] for p in data["parameters"]],
              [18, 16, 11, 16, 17, 22], status_column=5)
    doc.heading("4. Impact analysis matrix")
    doc.table(["Parameter", "Depends on", "Downstream", "Targets / CAD / simulation / code"],
              [[p["name"], ", ".join(p["depends_on"]) or "-", ", ".join(p["downstream"]) or "-",
                "; ".join([*(f"{k} -> {v}" for k, v in p["bindings"].items()), *p["cad"], *([p["simulation"]] if p["simulation"] else []), *p["code"]]) or "-"]
               for p in data["parameters"]],
              [18, 18, 20, 44])
    doc.heading("5. Simulation evidence")
    for analysis, label in (("cfd", "CFD"), ("fea", "3D FEA")):
        sim = data["simulations"].get(analysis)
        if not sim:
            doc.paragraph(f"{label}: no completed run in this workspace.")
            continue
        rows = [("Engine", _fmt(sim["engine"])), ("Method", _fmt(sim["method"])), ("Converged", _fmt(sim["converged"])),
                ("Iterations", _fmt(sim["iterations"])), ("Task", f"{sim['task_id']} ({sim['finished_at'][:19]}Z)")]
        rows += [(k.replace("_", " ").capitalize(), _fmt(v)) for k, v in sim["mesh"].items()]
        rows += [_metric(k, v) for k, v in sim["metrics"].items()]
        doc.paragraph(label, 10, "bold", "#0d2b52")
        doc.key_values(rows)
        for warning in sim.get("warnings") or []:
            doc.paragraph(f"Warning: {warning}", 8, "mono", "#b42318")
        if sim.get("residuals"):
            doc.plot(sim["residuals"], title=f"{label} residual convergence", colors=SERIES)
    doc.heading("6. Configuration management & review")
    governance = data.get("governance") or {}
    if governance.get("merged"):
        doc.table(["Request", "Author", "Approvers", "Date", "Rationale"],
                  [[r["id"][:8], r.get("author") or "-", ", ".join(r.get("approvers") or []) or "-", (r.get("created_at") or "")[:19], r.get("rationale") or ""] for r in governance["merged"]],
                  [9, 14, 18, 20, 31])
    else:
        doc.paragraph("No server-side change requests (local workspace or none merged).")
    if governance.get("baselines"):
        doc.table(["Baseline", "Fabric hash", "Created by", "Created"], [[b["tag"], b["hash"][:16], b["created_by"], b["created_at"][:19]] for b in governance["baselines"]], [20, 20, 20, 22])
    audit = data.get("audit")
    if audit:
        doc.paragraph(f"Audit ledger: {'VERIFIED' if audit['valid'] else 'BROKEN'} hash chain, {audit['records']} records, head {str(audit['head'])[:32]}", 8.5, "mono", "#0b7a0b" if audit["valid"] else "#b42318")
    doc.heading("7. Tamper-evident seal")
    doc.key_values([("Algorithm", f"{seal_record['algorithm']} (RFC 8032)"), ("Payload SHA-256", seal_record["payload_sha256"]),
                    ("Signature", seal_record["signature"]), ("Signing key id", seal_record["key_id"]),
                    ("Signed", f"{seal_record['signed_at']} by {seal_record['signer']}"),
                    ("Standards", "ISO 9001:2015 7.5, 8.3 / AS9100D 8.1.3, 8.3.4"),
                    ("Verification", "paramfabric report-verify <file> --public-key <organization public key>")])
    doc.metadata["ParamfabricReport"] = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    doc.metadata["ParamfabricSeal"] = json.dumps(seal_record, sort_keys=True)
    return doc.render()


def export_report(path: Path, data: dict[str, Any], signing_key: Any, *, signer: str = "paramfabric", fmt: str | None = None) -> dict[str, Any]:
    """Seal `data` and write it as HTML or PDF (chosen by `fmt` or the file suffix)"""
    path = Path(path)
    kind = (fmt or path.suffix.lstrip(".") or "html").lower()
    if kind not in {"html", "pdf"}:
        raise ReportError("report format must be html or pdf")
    # round-trip through JSON so the sealed bytes equal what verification will parse
    data = json.loads(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str))
    seal_record = seal(data, signing_key, signer)
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "html":
        path.write_text(render_html(data, seal_record), encoding="utf-8")
    else:
        path.write_bytes(render_pdf(data, seal_record))
    return seal_record


def governance_summary(session: Any, tenant: Any) -> dict[str, Any]:
    """Merged change requests (with approvers) and baselines of one tenant, newest first"""
    from paramfabric.governance.baselines import list_baselines
    from paramfabric.governance.pr_engine import list_requests

    merged=[]
    for record in list_requests(session, tenant):
        if record.status != "merged":
            continue
        merged.append({
            "id": record.id, "author": record.author, "rationale": record.rationale,
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "approvers": [review.reviewer for review in record.reviews if review.decision == "approve"],
        })
    baselines = [{"tag": b.tag, "hash": b.fabric_hash, "created_by": b.created_by, "created_at": b.created_at.isoformat()}
                 for b in list_baselines(session, tenant)]
    return {"merged": merged[:50], "baselines": baselines[:50]}


def report_signer(root: Path, organization: str = "default") -> Any:
    from paramfabric.security.signing import SigningError, resolve_signer

    try:
        return resolve_signer(root, organization)
    except SigningError as exc:
        raise ReportError(str(exc)) from exc
