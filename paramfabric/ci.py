"""Pull-request quality gate: `paramfabric ci-check`.

Compares the parameter fabric at a base and a head Git revision and reports:

Blocking errors (exit code 1)
    * `invalid-fabric`        the head fabric does not parse or validate
    * `unit-mismatch`         a value or formula is dimensionally inconsistent (Pint)
    * `circular-dependency`   formulas form a cycle
    * `invalid-expression`    unknown references, syntax errors, blocked inputs
    * `dimension-change`      a parameter changed physical dimension (m -> s)
    * `unapproved-edit`       parameter definitions changed without an Ed25519-signed
                                approval record for exactly this fabric in `paramfabric.lock`
    * `approval-unverifiable` an approval is required but no public key is configured
    * `source-drift`          Python/C++/Rust/OpenSCAD constants disagree with the fabric

Warnings never fail the gate (`--approvals warn` / `--drift warn` turn
the corresponding errors into warnings).

The Markdown summary lists every changed parameter and its blast radius:
downstream parameters, bound generator targets (C++ headers, OpenFOAM
dictionaries, CAD macros…), CAD features and dimension bindings in the
workbench, simulation inputs, and source-code constants. It starts with a
stable HTML marker so a CI job can update one pull-request comment in place.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from paramfabric.core.diagnostics import diagnose
from paramfabric.core.graph import ParameterGraph
from paramfabric.core.schema import Fabric, load_fabric
from paramfabric.governance.approvals import LOCK_FILE, load_lock, verify_approval
from paramfabric.governance.impact import blast_radius
from paramfabric.governance.pr_engine import fabric_hash
from paramfabric.security.signing import SigningError, load_public_keys
from paramfabric.utils.git_tools import _run_git, fabric_at_revision, parameter_diff

COMMENT_MARKER = "<!-- paramfabric-ci-check -->"
WORKTREE = "WORKTREE"
BINDING_LABELS = {
    "cpp_header": "C++ header", "py_module": "Python module", "rust": "Rust module", "openscad": "OpenSCAD",
    "latex": "LaTeX table", "markdown": "Markdown table", "solidworks": "SolidWorks macro", "nx": "Siemens NX journal",
    "ansys": "ANSYS APDL", "openfoam": "OpenFOAM dictionary", "comsol": "COMSOL model",
}
SIMULATION_INPUTS = {
    "inlet_velocity": "OpenFOAM `0/U` inlet and `forceCoeffs` magUInf; native CFD inlet",
    "fluid_density": "OpenFOAM `forceCoeffs` rhoInf and reported pressures; native CFD density",
    "kinematic_viscosity": "OpenFOAM `constant/transportProperties` ν; boundary-layer first height",
    "mesh_fineness_ratio": "gmsh target cell size (all meshes)",
    "youngs_modulus": "FEA material stiffness (solid, beam, shell)",
    "poissons_ratio": "FEA material Poisson ratio",
    "applied_load": "FEA default load case",
    "thermal_expansion": "FEA thermal load",
    "temperature_change": "FEA thermal load",
}
DRIFT_STATES = {"drift", "structural_drift", "unit_mismatch", "conflict"}


@dataclass
class Finding:
    severity: str  # "error" | "warning"
    code: str
    message: str
    parameter: str | None = None


@dataclass
class CIReport:
    base: str
    head: str
    findings: list[Finding] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    impacts: list[dict[str, Any]] = field(default_factory=list)
    targets: list[dict[str, Any]] = field(default_factory=list)
    cad: list[dict[str, Any]] = field(default_factory=list)
    simulation: list[dict[str, Any]] = field(default_factory=list)
    code: list[dict[str, Any]] = field(default_factory=list)
    approval: dict[str, Any] | None = None
    fabric_hash: str | None = None

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def passed(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["passed"] = self.passed
        return data

    def error(self, code: str, message: str, parameter: str | None = None, *, demote: bool = False) -> None:
        self.findings.append(Finding("warning" if demote else "error", code, message, parameter))

    # ------------------------------------------------------------------ markdown
    def markdown(self) -> str:
        status = "✅ **Paramfabric gate passed**" if self.passed else "❌ **Paramfabric gate failed**"
        definition_changes=[c for c in self.changes if c["change"] != "calculated"]
        lines = [
            COMMENT_MARKER,
            f"## {status}",
            "",
            f"`{self.base}` → `{self.head}` · **{len(definition_changes)}** parameter change(s) · "
            f"**{max(0, len(self.impacts) - len(definition_changes))}** downstream parameter(s) · "
            f"**{len(self.errors)}** blocking error(s) · {len(self.warnings)} warning(s)",
            "",
        ]
        if self.findings:
            lines += ["### Checks", "", "| | Check | Parameter | Detail |", "|---|---|---|---|"]
            for finding in sorted(self.findings, key=lambda f: (f.severity != "error", f.code)):
                icon = "❌" if finding.severity == "error" else "⚠️"
                lines.append(f"| {icon} | `{finding.code}` | {_cell(finding.parameter or '—')} | {_cell(finding.message)} |")
            lines.append("")
        else:
            lines += ["All checks passed: units, dependency graph, approvals, and source sync.", ""]
        if self.changes:
            lines += ["### Parameter changes", "", "| Parameter | Change | Before | After | Δ |", "|---|---|---|---|---|"]
            for change in self.changes:
                lines.append(f"| `{change['name']}` | {change['change']} | {_cell(_describe(change.get('before'), change.get('before_resolved')))} | "
                             f"{_cell(_describe(change.get('after'), change.get('after_resolved')))} | {_delta(change)} |")
            lines.append("")
        if self.impacts:
            lines += ["### Blast radius", "", "| Parameter | Distance | Depends on | Tags |", "|---|---|---|---|"]
            for impact in self.impacts:
                lines.append(f"| `{impact['parameter']}` | {impact['distance']} | {_cell(', '.join(impact['dependencies']) or '—')} | {_cell(', '.join(impact['tags']) or '—')} |")
            lines.append("")
        sections = [
            ("Generated targets (bindings)", [f"{t['label']} `{t['path']}` ← `{t['parameter']}`" for t in self.targets]),
            ("CAD features and dimensions", [f"{c['description']} ← `{c['parameter']}`" for c in self.cad]),
            ("Simulation inputs", [f"`{s['parameter']}`: {s['effect']}" for s in self.simulation]),
            ("Source-code constants", [f"`{c['file']}:{c['line']}` {c['language']} `{c['name']}` ← `{c['parameter']}`" for c in self.code]),
        ]
        if any(items for _, items in sections):
            lines += ["### Downstream artifacts affected", ""]
            for title, items in sections:
                if items:
                    lines += [f"<details open><summary><b>{title}</b> ({len(items)})</summary>", ""]
                    lines += [f"- {item}" for item in items]
                    lines += ["", "</details>", ""]
        if self.approval:
            approvers = ", ".join(self.approval.get("approvers") or []) or "—"
            lines += ["### Approval", "",
                      f"Change request `{self.approval.get('change_request')}` · approved by {approvers} · "
                      f"fabric `{(self.fabric_hash or '')[:12]}` · signature verified.", ""]
        lines.append("<sub>Generated by `paramfabric ci-check`.</sub>")
        return "\n".join(lines) + "\n"


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _describe(parameter: dict[str, Any] | None, resolved: Any) -> str:
    if not parameter:
        return "—"
    value = parameter.get("value")
    text = f"{value} {parameter.get('unit', '')}".strip()
    if isinstance(value, str) and parameter.get("type") != "str" and resolved is not None:
        text += f" (= {resolved:.6g})" if isinstance(resolved, (int, float)) else ""
    return text


def _delta(change: dict[str, Any]) -> str:
    delta = change.get("resolved_delta")
    before = change.get("before_resolved")
    if not isinstance(delta, (int, float)) or not isinstance(before, (int, float)):
        return "—"
    relative = f" ({delta / before:+.1%})" if before else ""
    return f"{delta:+.6g}{relative}"


def load_revision(revision: str, fabric_path: Path, root: Path) -> Fabric:
    if revision == WORKTREE:
        return load_fabric(fabric_path)
    return fabric_at_revision(revision, fabric_path, root)


def _lock_at(revision: str, lock_path: Path, root: Path) -> list[dict[str, Any]] | None:
    if revision == WORKTREE:
        return load_lock(lock_path) if lock_path.is_file() else None
    relative = lock_path.resolve().relative_to(root.resolve()).as_posix()
    try:
        data = json.loads(_run_git(["show", f"{revision}:{relative}"], root))
    except RuntimeError:
        return None  # no lock file at that revision
    except ValueError:
        return []
    return data if isinstance(data, list) else [data]


def run_ci_check(
    root: Path,
    fabric_path: Path,
    base: str,
    head: str = WORKTREE,
    *,
    approvals: str = "required",
    drift: str = "error",
    lock_path: Path | None = None,
    approval_key: str | None = None,
    public_keys: str | Path | None = None,
) -> CIReport:
    root = Path(root).resolve()
    fabric_path = (root / fabric_path) if not Path(fabric_path).is_absolute() else Path(fabric_path)
    report = CIReport(base=base, head=head)
    try:
        head_fabric = load_revision(head, fabric_path, root)
    except (ValidationError, ValueError, RuntimeError, FileNotFoundError) as exc:
        report.error("invalid-fabric", f"head fabric cannot be loaded: {exc}")
        return report
    try:
        base_fabric = load_revision(base, fabric_path, root)
    except RuntimeError as exc:
        if "does not exist" in str(exc) or "exists on disk, but not in" in str(exc):
            base_fabric = Fabric()  # the fabric is new in this pull request
        else:
            report.error("invalid-fabric", f"base revision {base!r} cannot be read: {exc}")
            return report
    except (ValidationError, ValueError) as exc:
        report.findings.append(Finding("warning", "invalid-base", f"base fabric is invalid ({exc}); comparing against an empty fabric"))
        base_fabric = Fabric()

    # 1. Units, cycles, and expressions on the head fabric.
    head_report = diagnose(head_fabric)
    codes = {"unit": "unit-mismatch", "cycle": "circular-dependency"}
    for issue in head_report["issues"]:
        report.error(codes.get(issue["kind"], "invalid-expression"), issue["message"], issue.get("parameter"))
    report.fabric_hash = fabric_hash(head_fabric)

    # 2. Changes and dimension changes.
    base_report = diagnose(base_fabric)
    try:
        report.changes = parameter_diff(base_fabric, head_fabric) if head_report["ok"] and base_report["ok"] else _definition_diff(base_fabric, head_fabric)
    except ValueError:
        report.changes = _definition_diff(base_fabric, head_fabric)
    for change in report.changes:
        name = change["name"]
        before, after = base_report["resolved"].get(name), head_report["resolved"].get(name)
        if change["change"] == "modified" and before and after and before.get("dimensionality") != after.get("dimensionality"):
            report.error("dimension-change", f"{name} changed dimension from {before.get('dimensionality')} to {after.get('dimensionality')}", name)
    edited = sorted(c["name"] for c in report.changes if c["change"] != "calculated")

    # 3. Approval of the edited fabric.
    if edited and approvals != "off":
        demote = approvals == "warn"
        lock = Path(lock_path) if lock_path else root / LOCK_FILE
        legacy=approval_key if approval_key is not None else os.getenv("PARAMFABRIC_APPROVAL_KEY")
        source = public_keys if public_keys is not None else os.getenv("PARAMFABRIC_APPROVAL_PUBLIC_KEY")
        keys: list[Any] = []
        if source:
            try:
                keys = load_public_keys(source)
            except SigningError as exc:
                report.error("approval-unverifiable", f"cannot load the approval public key: {exc}", demote=demote)
        records = _lock_at(head, lock if lock.is_absolute() else root / lock, root)
        if not keys and not legacy:
            report.error("approval-unverifiable", "set PARAMFABRIC_APPROVAL_PUBLIC_KEY (the organization's public key) to verify approval records", demote=demote)
        elif not records:
            report.error("unapproved-edit", f"{len(edited)} parameter(s) changed without an approval record ({LOCK_FILE} missing): {', '.join(edited)}", demote=demote)
        else:
            valid=[record for record in records if verify_approval(record, keys, legacy_secret=legacy)]
            match = next((record for record in valid if record.get("fabric_hash") == report.fabric_hash), None)
            if match:
                report.approval = match
            elif len(valid) < len(records) and not valid:
                report.error("unapproved-edit", f"{LOCK_FILE} signature is invalid (tampered or signed with another key)", demote=demote)
            else:
                report.error("unapproved-edit", f"no approved change request matches this fabric; edited without review: {', '.join(edited)}", demote=demote)

    # 4. Blast radius and downstream artifacts.
    head_names = set(head_fabric.by_name())
    changed={name for name in edited if name in head_names}
    if changed and head_report["dag_ok"]:
        report.impacts = [dict(impact) for impact in blast_radius(ParameterGraph(head_fabric), changed)]
    impacted = {impact["parameter"] for impact in report.impacts} | {name for name in edited if name not in head_names}
    parameters = {**base_fabric.by_name(), **head_fabric.by_name()}
    for name in sorted(impacted):
        for kind, path in sorted(parameters[name].bindings.items()):
            report.targets.append({"parameter": name, "kind": kind, "label": BINDING_LABELS.get(kind, kind), "path": path})
        if name in SIMULATION_INPUTS:
            report.simulation.append({"parameter": name, "effect": SIMULATION_INPUTS[name]})
    report.cad = _cad_references(root, impacted)

    # 5. Source-code drift (on the checked-out working tree).
    if drift != "off":
        report.code, drifted = _source_constants(root, head_fabric, head_report, impacted)
        for item in drifted:
            message = (f"{item['path']}:{item['line']} {item['name']} = {item.get('value')} "
                       f"but the fabric has {item.get('fabric_value')} ({item['state'].replace('_', ' ')})")
            report.error("source-drift", message, item["parameter"], demote=drift == "warn")
    return report


def _definition_diff(before: Fabric, after: Fabric) -> list[dict[str, Any]]:
    left, right = before.by_name(), after.by_name()
    changes = []
    for name in sorted(left.keys() | right.keys()):
        if name not in left:
            changes.append({"name": name, "change": "added", "after": right[name].model_dump()})
        elif name not in right:
            changes.append({"name": name, "change": "removed", "before": left[name].model_dump()})
        elif left[name] != right[name]:
            changes.append({"name": name, "change": "modified", "before": left[name].model_dump(), "after": right[name].model_dump()})
    return changes


def _cad_references(root: Path, names: set[str]) -> list[dict[str, Any]]:
    if not names:
        return []
    try:
        from paramfabric.geometry.scene import GeometryStore

        store = GeometryStore(root)
        settings, models = store.settings(), store.models()
    except Exception:  # noqa: BLE001 - no workbench in this repository
        return []
    found = []
    for model in models:
        for body in model.bodies:
            for axis, parameter in body.dimension_parameters.items():
                if parameter in names:
                    found.append({"parameter": parameter, "description": f"{model.filename} · {body.name} {axis.upper()} extent (exact push-pull)"})
    for feature in settings.get("features", []):
        for role, parameter in feature.get("params", {}).items():
            if parameter in names:
                found.append({"parameter": parameter, "description": f"Feature {feature.get('name', feature.get('type'))} ({feature.get('type')}, {role})"})
    return found


def _source_constants(root: Path, fabric: Fabric, report: dict[str, Any], impacted: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from paramfabric.auditors.sync import SyncEngine

    import paramfabric

    if not report["ok"]:
        return [], []
    engine=SyncEngine(root, exclude=[Path(paramfabric.__file__).resolve().parent])
    items = engine.status(fabric, report["quantities"])
    affected = [
        {"parameter": item["parameter"], "file": item["path"], "line": item["line"], "name": item["name"], "language": item["language"]}
        for item in items if item["parameter"] in impacted
    ]
    return affected, [item for item in items if item.get("state") in DRIFT_STATES]
