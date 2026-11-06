"""Git-backed fabric history and parameter comparisons"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pint

from paramfabric.core.graph import ParameterGraph
from paramfabric.core.schema import Fabric


def _run_git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def fabric_at_revision(revision: str, path: Path, cwd: Path) -> Fabric:
    """Load the fabric file stored at a Git revision"""
    relative_path = path.resolve().relative_to(cwd.resolve()).as_posix()
    text = _run_git(["show", f"{revision}:{relative_path}"], cwd)
    text = text.lstrip("\ufeff")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        import yaml

        data=yaml.safe_load(text)
    if isinstance(data, list):
        data = {"parameters": data}
    return Fabric.model_validate(data)


def parameter_diff(before: Fabric, after: Fabric) -> list[dict[str, object]]:
    """Return changed, added, and removed parameter records"""
    left, right = before.by_name(), after.by_name()
    before_values = ParameterGraph(before).evaluate()
    after_values = ParameterGraph(after).evaluate()
    changes: list[dict[str, object]] = []
    for name in sorted(left.keys() | right.keys()):
        if name not in left:
            changes.append({"name": name, "change": "added", "after": right[name].model_dump()})
            continue
        if name not in right:
            changes.append({"name": name, "change": "removed", "before": left[name].model_dump()})
            continue
        if left[name].type in {"str", "bool"} or right[name].type in {"str", "bool"}:
            old_resolved: int | float | str | bool = left[name].value
            new_resolved: int | float | str | bool = right[name].value
            delta = None
            resolved_changed = old_resolved != new_resolved
        else:
            before_quantity = before_values[name]
            after_quantity=after_values[name]
            old_resolved = float(before_quantity.magnitude)
            new_resolved = float(after_quantity.magnitude)
            try:
                delta = float(after_quantity.to(before_quantity.units).magnitude) - old_resolved
                resolved_changed = abs(delta) > max(1e-12, abs(old_resolved) * 1e-9)
            except pint.errors.DimensionalityError:
                delta = None
                resolved_changed = True
        definition_changed = left[name] != right[name]
        if definition_changed or resolved_changed:
            changes.append(
                {
                    "name": name,
                    "change": "modified" if definition_changed else "calculated",
                    "before": left[name].model_dump(),
                    "after": right[name].model_dump(),
                    "before_resolved": old_resolved,
                    "after_resolved": new_resolved,
                    "resolved_delta": delta,
                }
            )
    return changes


def parameter_history(
    name: str,
    path: Path,
    cwd: Path,
    revision: str = "HEAD",
) -> list[dict[str, str]]:
    """Return commit hashes, authors, and timestamps that changed one parameter"""
    relative = path.resolve().relative_to(cwd.resolve()).as_posix()
    rows = _run_git(
        ["log", revision, "--format=%H%x09%an%x09%aI", "--", relative],
        cwd,
    )
    history: list[dict[str, str]] = []
    for row in rows.splitlines():
        commit, author, timestamp = row.split("\t", maxsplit=2)
        parents = _run_git(["rev-list", "--parents", "-n", "1", commit], cwd).split()
        current = fabric_at_revision(commit, path, cwd).by_name().get(name)
        previous = None
        if len(parents) > 1:
            try:
                previous = fabric_at_revision(parents[1], path, cwd).by_name().get(name)
            except RuntimeError as exc:
                if "does not exist" not in str(exc) and "not in" not in str(exc):
                    raise
        if current != previous:
            history.append({"commit": commit, "author": author, "timestamp": timestamp})
    return history
