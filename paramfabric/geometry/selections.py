"""Named selections: user-named groups of faces, edges, or bodies.

A selection is stored in the workbench settings::

    {"id": "ab12cd34", "name": "Clamp end", "kind": "face",
     "items": [{"body": "<model>/<body>", "selector": {"point": [...], "normal": [...], "topology": {...}}}]}

Exact-kernel faces and edges carry the same persistent names as features, so a
selection keeps meaning the same entity when parameters change. A boundary
condition with `named_selection` set takes its faces from the selection each
time a task runs, so it follows the geometry too.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from paramfabric.geometry import exact

KINDS = {"face", "edge", "body"}
_NAME = re.compile(r"^[^\W_][\w .\-]*$")
MAX_NAME = 60


class SelectionError(ValueError):
    """The named selection is invalid (bad name, duplicate, empty, unknown)"""


def clean_name(name: Any) -> str:
    """Validate a display name for an entity"""
    text = str(name or "").strip()
    if not text:
        raise SelectionError("enter a name")
    if len(text) > MAX_NAME:
        raise SelectionError(f"names are limited to {MAX_NAME} characters")
    if not _NAME.match(text):
        raise SelectionError("names start with a letter or digit and use letters, digits, spaces, '_', '-', or '.'")
    return text


def _check_unique(selections: list[dict[str, Any]], name: str, ignore: str | None = None) -> None:
    for selection in selections:
        if selection["id"] != ignore and selection["name"].casefold() == name.casefold():
            raise SelectionError(f"a named selection called {selection['name']!r} already exists")


def _check_items(kind: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not items:
        raise SelectionError("a named selection needs at least one entity")
    checked = []
    for item in items:
        body = item.get("body")
        if not isinstance(body, str) or "/" not in body:
            raise SelectionError("each entity needs its body")
        clean: dict[str, Any] = {"body": body}
        if kind != "body":
            selector = item.get("selector") or {}
            point = selector.get("point")
            if not (isinstance(point, list) and len(point) == 3 and all(isinstance(v, (int, float)) for v in point)):
                raise SelectionError(f"each {kind} needs a picked point")
            clean["selector"] = {"point": [float(v) for v in point]}
            if selector.get("normal"):
                clean["selector"]["normal"] = [float(v) for v in selector["normal"]]
        checked.append(clean)
    return checked


def create(settings: dict[str, Any], name: Any, kind: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Add a selection to `settings` (not yet annotated; see :func:`annotate`)"""
    if kind not in KINDS:
        raise SelectionError(f"kind must be one of {sorted(KINDS)}")
    selections = settings.setdefault("named_selections", [])
    title=clean_name(name)
    _check_unique(selections, title)
    selection = {"id": uuid.uuid4().hex[:8], "name": title, "kind": kind, "items": _check_items(kind, items)}
    selections.append(selection)
    return selection


def find(settings: dict[str, Any], selection_id: str) -> dict[str, Any]:
    for selection in settings.get("named_selections", []):
        if selection["id"] == selection_id:
            return selection
    raise KeyError(selection_id)


def rename(settings: dict[str, Any], selection_id: str, name: Any) -> dict[str, Any]:
    selection = find(settings, selection_id)
    title=clean_name(name)
    _check_unique(settings.get("named_selections", []), title, ignore=selection_id)
    selection["name"] = title
    return selection


def add_items(settings: dict[str, Any], selection_id: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Append entities to a selection; returns the new ones (entities already present are skipped)"""
    selection = find(settings, selection_id)
    fresh = []
    for item in _check_items(selection["kind"], items):
        if selection["kind"] == "body":
            duplicate = any(existing["body"] == item["body"] for existing in selection["items"])
        else:
            duplicate = any(existing["body"] == item["body"] and existing["selector"]["point"] == item["selector"]["point"]
                            for existing in selection["items"])
        if not duplicate:
            selection["items"].append(item)
            fresh.append(item)
    return fresh


def remove(settings: dict[str, Any], selection_id: str) -> None:
    find(settings, selection_id)
    settings["named_selections"] = [s for s in settings["named_selections"] if s["id"] != selection_id]
    for condition in settings.get("boundary_conditions", []):
        if condition.get("named_selection") == selection_id:
            condition.pop("named_selection")


def annotate(store: Any, models: list[Any], settings: dict[str, Any], kind: str, items: list[dict[str, Any]], quantities: dict[str, Any]) -> None:
    if kind == "body" or not items:
        return
    try:
        exact.name_entities(store, models, settings.get("features", []), kind, items, quantities)
    except (exact.RegenerationError, exact.ExactKernelUnavailable):
        pass  # the point-and-normal selector still works; persistent naming is an upgrade


def resolve(store: Any, models: list[Any], settings: dict[str, Any], selection: dict[str, Any], quantities: dict[str, Any]) -> list[dict[str, Any]]:
    if selection["kind"] == "body":
        return [{"body": item["body"]} for item in selection["items"]]
    try:
        return exact.resolve_entities(store, models, settings.get("features", []), selection["kind"], selection["items"], quantities)
    except (exact.RegenerationError, exact.ExactKernelUnavailable):
        return [{"body": item["body"], **{k: v for k, v in item["selector"].items() if k in {"point", "normal"}}} for item in selection["items"]]


def apply_to_conditions(store: Any, models: list[Any], settings: dict[str, Any], quantities: dict[str, Any]) -> dict[str, Any]:
    """Settings with each linked boundary condition's faces taken from its named selection"""
    conditions = settings.get("boundary_conditions", [])
    if not any(condition.get("named_selection") for condition in conditions):
        return settings
    resolved = []
    cache: dict[str, list[dict[str, Any]]] = {}
    for condition in conditions:
        selection_id = condition.get("named_selection")
        if selection_id:
            try:
                selection = find(settings, selection_id)
            except KeyError:
                selection=None
            if selection is not None and selection["kind"] == "face":
                if selection_id not in cache:
                    cache[selection_id] = resolve(store, models, settings, selection, quantities)
                condition = {**condition, "selectors": [{"point": item["point"], "normal": item.get("normal")} for item in cache[selection_id]]}
        resolved.append(condition)
    return {**settings, "boundary_conditions": resolved}
