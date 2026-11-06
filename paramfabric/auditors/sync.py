"""Bi-directional, AST-based synchronization between the fabric and source code.

Scanners return each constant's exact initializer span so it can be rewritten
in place without disturbing formatting, comments, or literal suffixes:

* Python - module-level `NAME = expr` / `NAME: float = expr` (`ast` positions)
* C/C++  - `const`/`constexpr`/`constinit` declarations and object-like `#define`
* Rust   - `const`/`static` items (`1_000.0`, `2.5_f64`, `x.powf(2.0)`)
* OpenSCAD - top-level `name = expr;` assignments

Initializers may be literals or formulas over other constants
(`WING_SPAN = 5 * ROOT_CHORD`). Formulas are parsed into a language-neutral
expression tree (:mod:`paramfabric.auditors.expressions`), identifiers are
matched to fabric parameters case-insensitively, and each side is reduced to a
canonical form - `v:<number>` or `f:<formula>` - so comparison is
structural, not textual.

Sync preserves the *form* of each source constant: literal constants receive
values (converted to a `// [unit]` comment's unit when present), expression
constants receive formulas rendered in that language. A three-way baseline
distinguishes source edits (pull), fabric edits (push), and edits on both
sides (conflict, never auto-resolved). :meth:`SyncEngine.diff` reports
structural drift across files and :meth:`SyncEngine.resolve` applies an
explicit strategy.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pint

from paramfabric.auditors import expressions as ex
from paramfabric.core.schema import Fabric
from paramfabric.core.units import Q_, evaluate_expression

SOURCE_SUFFIXES = {
    ".py": "python", ".h": "cpp", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp", ".cpp": "cpp",
    ".cc": "cpp", ".cxx": "cpp", ".rs": "rust", ".scad": "openscad",
}
EXCLUDED_DIRECTORIES = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__", ".paramfabric",
    "dist", "build", ".pytest_cache", ".mypy_cache", "site-packages", "target",
}
MAX_FILE_BYTES = 1_000_000
MAX_FILES = 5_000
STRATEGIES = ("fabric", "newest", "accept")  # plus "source:<path>"

_CPP_DECLARATION = re.compile(
    r"^[ \t]*(?:(?:inline|static|extern)\s+)*(?:constexpr|const|constinit)\s+"
    r"(?:(?:inline|static|const|constexpr|unsigned|signed|long|short|std::)\s*)*[\w:<>]+\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*(?:=\s*(?P<value>[^;{}\n]+?)|\{\s*(?P<braced>[^;{}\n]+?)\s*\})\s*;",
    re.MULTILINE,
)
_CPP_DEFINE = re.compile(
    r"^[ \t]*#[ \t]*define[ \t]+(?P<name>[A-Za-z_]\w*)[ \t]+(?P<value>[^\n]*?)[ \t]*(?://.*|/\*.*)?$",
    re.MULTILINE,
)
_RUST_ITEM = re.compile(
    r"^[ \t]*(?:pub(?:\([^)]*\))?\s+)?(?:const|static)\s+(?P<name>[A-Za-z_]\w*)\s*:\s*(?P<type>[\w:]+)\s*=\s*"
    r"(?P<value>[^;]+?)\s*;",
    re.MULTILINE,
)
_SCAD_ASSIGNMENT = re.compile(r"^[ \t]*(?P<name>[A-Za-z_]\w*)[ \t]*=[ \t]*(?P<value>[^;\n]+?)[ \t]*;", re.MULTILINE)
_UNIT_COMMENT = re.compile(r"(?://|#)\s*\[([^\]]+)\]")
_LITERAL_PARTS = re.compile(r"^(?P<sign>[+-]?)(?P<number>(?:\d(?:[\d_]*\d)?)?\.?\d(?:[\d_]*\d)?(?:[eE][+-]?\d+)?)(?P<suffix>_?(?:f32|f64|i8|i16|i32|i64|i128|u8|u16|u32|u64|u128|usize|isize)|[fFlLuU]*)$")
_FLOAT_TYPES = {"f32", "f64", "float", "double"}


@dataclass(frozen=True)
class SourceConstant:
    """One constant: its initializer span, parsed form, and value if literal"""

    name: str
    value: float | None
    unit: str | None
    language: str
    path: str
    line: int
    start: int
    end: int
    literal: str
    integer_literal: bool
    float_type: bool
    formula: str | None = None  # canonical formula over *source* identifiers

    @property
    def key(self) -> str:
        return f"{self.path}::{self.name}"


def _line_unit(text: str, offset: int) -> str | None:
    line_end = text.find("\n", offset)
    match = _UNIT_COMMENT.search(text[offset : line_end if line_end >= 0 else len(text)])
    return match.group(1).strip() if match else None


def _integer(literal: str) -> bool:
    return not any(char in literal for char in ".eE")


def _make(name: str, initializer: str, start: int, text: str, language: str, path: str, float_type: bool) -> SourceConstant | None:
    raw = initializer.strip()
    if not raw:
        return None
    leading = len(initializer) - len(initializer.lstrip())
    start += leading
    parts = _LITERAL_PARTS.match(raw)
    if parts and language != "python":
        number=parts.group("sign") + parts.group("number")
        suffix = parts.group("suffix") or ""
        literal_float = float_type or (language == "cpp" and "f" in suffix.lower()) or suffix.lstrip("_") in {"f32", "f64"}
        return SourceConstant(
            name=name, value=float(number.replace("_", "")), unit=_line_unit(text, start), language=language, path=path,
            line=text.count("\n", 0, start) + 1, start=start, end=start + len(number), literal=number,
            integer_literal=_integer(number), float_type=bool(literal_float or not _integer(number)),
        )
    try:
        tree = ex.parse(raw, language)
    except ex.ExpressionError:
        return None
    end = start + len(raw)
    if ex.is_literal(tree):
        return SourceConstant(
            name=name, value=ex.literal_value(tree), unit=_line_unit(text, start), language=language, path=path,
            line=text.count("\n", 0, start) + 1, start=start, end=end, literal=raw,
            integer_literal=_integer(raw), float_type=float_type or not _integer(raw),
        )
    return SourceConstant(
        name=name, value=None, unit=_line_unit(text, start), language=language, path=path,
        line=text.count("\n", 0, start) + 1, start=start, end=end, literal=raw,
        integer_literal=False, float_type=float_type, formula=ex.canonical(tree),
    )


def scan_text(text: str, language: str, path: str = "") -> list[SourceConstant]:
    """Extract literal and formula constants with their initializer spans"""
    constants: list[SourceConstant] = []
    if language == "python":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        line_starts=[0]
        for line in text.splitlines(keepends=True):
            line_starts.append(line_starts[-1] + len(line))
        lines = text.splitlines(keepends=True)

        def offset(line_number: int, byte_column: int) -> int:
            raw = lines[line_number - 1].encode("utf-8")[:byte_column]
            return line_starts[line_number - 1] + len(raw.decode("utf-8", "replace"))

        for statement in tree.body:
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                target, value_node, annotation = statement.targets[0], statement.value, None
            elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
                target, value_node, annotation = statement.target, statement.value, statement.annotation
            else:
                continue
            if not isinstance(target, ast.Name) or value_node.end_lineno is None or value_node.end_col_offset is None:
                continue
            try:
                ex.validate(value_node)
            except ex.ExpressionError:
                continue
            start = offset(value_node.lineno, value_node.col_offset)
            end = offset(value_node.end_lineno, value_node.end_col_offset)
            float_annotation = isinstance(annotation, ast.Name) and annotation.id == "float"
            unit = _line_unit(text, start)
            if ex.is_literal(value_node):
                inner = value_node if isinstance(value_node, ast.Constant) else value_node.operand  # type: ignore[attr-defined]
                constants.append(SourceConstant(
                    name=target.id, value=ex.literal_value(value_node), unit=unit, language=language, path=path,
                    line=value_node.lineno, start=start, end=end, literal=text[start:end],
                    integer_literal=isinstance(inner.value, int), float_type=isinstance(inner.value, float) or float_annotation,
                ))
            else:
                constants.append(SourceConstant(
                    name=target.id, value=None, unit=unit, language=language, path=path, line=value_node.lineno,
                    start=start, end=end, literal=text[start:end], integer_literal=False, float_type=float_annotation,
                    formula=ex.canonical(value_node),
                ))
        return constants
    if language == "cpp":
        for match in _CPP_DECLARATION.finditer(text):
            group = "value" if match.group("value") is not None else "braced"
            declaration = match.group(0)
            float_type = bool(re.search(r"\b(double|float)\b", declaration[: match.start("name") - match.start()]))
            item = _make(match.group("name"), match.group(group), match.start(group), text, language, path, float_type)
            if item:
                constants.append(item)
        for match in _CPP_DEFINE.finditer(text):
            if "(" in text[match.end("name") : match.end("name") + 1]:
                continue  # function-like macro
            item = _make(match.group("name"), match.group("value"), match.start("value"), text, language, path, False)
            if item:
                constants.append(item)
    elif language == "rust":
        for match in _RUST_ITEM.finditer(text):
            float_type = match.group("type") in _FLOAT_TYPES
            item = _make(match.group("name"), match.group("value"), match.start("value"), text, language, path, float_type)
            if item:
                constants.append(item)
    elif language == "openscad":
        for match in _SCAD_ASSIGNMENT.finditer(text):
            item = _make(match.group("name"), match.group("value"), match.start("value"), text, language, path, False)
            if item:
                constants.append(item)
    return sorted(constants, key=lambda item: item.start)


def format_literal(value: float, constant: SourceConstant, integer_parameter: bool) -> str:
    """Render a value in the style of the literal it replaces"""
    if integer_parameter and float(value).is_integer() and not constant.float_type:
        return str(int(value))
    text = f"{value:.15g}"
    if "inf" in text or "nan" in text:
        raise ValueError(f"cannot write non-finite value {text} to {constant.language} source")
    if constant.language == "openscad":
        return text
    if constant.integer_literal and not constant.float_type and float(value).is_integer():
        return str(int(value))
    if not any(char in text for char in ".eE"):
        text += ".0"
    return text


def inject(text: str, replacements: list[tuple[SourceConstant, str]]) -> str:
    for constant, literal in sorted(replacements, key=lambda item: item[0].start, reverse=True):
        text = text[: constant.start] + literal + text[constant.end :]
    return text


def _read(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _equal(a: float, b: float) -> bool:
    return abs(a - b) <= max(1e-12, 1e-9 * max(abs(a), abs(b)))


def _same_form(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return a == b
    if a.startswith("v:") and b.startswith("v:"):
        return _equal(float(a[2:]), float(b[2:]))
    return a == b


class SyncEngine:
    """Scan a workspace, diff source against the fabric, and reconcile both ways"""

    def __init__(self, root: Path, *, state_path: Path | None = None, exclude: list[Path] | None = None) -> None:
        self.root = Path(root).resolve()
        self.state_path = state_path or self.root / ".paramfabric" / "sync-state.json"
        self.exclude = [Path(path).resolve() for path in (exclude or [])]
        self._cache: dict[Path, tuple[int, int, list[SourceConstant]]] = {}

    # state
    def _state(self) -> dict[str, str]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}
        state = {}
        for key, value in data.items():
            if isinstance(value, (int, float)):
                state[str(key)] = f"v:{float(value)!r}"  # baselines written by older releases
            elif isinstance(value, str):
                state[str(key)] = value
        return state

    def _save_state(self, state: dict[str, str]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")

    # scanning
    def files(self) -> list[Path]:
        found: list[Path] = []
        stack=[self.root]
        while stack and len(found) < MAX_FILES:
            directory=stack.pop()
            try:
                entries = sorted(directory.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_dir():
                    if entry.name in EXCLUDED_DIRECTORIES or entry.name.startswith("."):
                        continue
                    if any(entry.resolve() == excluded for excluded in self.exclude):
                        continue
                    stack.append(entry)
                elif entry.suffix.lower() in SOURCE_SUFFIXES:
                    try:
                        if entry.stat().st_size <= MAX_FILE_BYTES:
                            found.append(entry)
                    except OSError:
                        continue
        return sorted(found)

    def scan(self, names: set[str]) -> list[SourceConstant]:
        lowered = {name.lower() for name in names}
        result: list[SourceConstant] = []
        for path in self.files():
            try:
                stat = path.stat()
            except OSError:
                continue
            cached = self._cache.get(path)
            if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
                constants = cached[2]
            else:
                try:
                    text=_read(path)
                except (OSError, UnicodeDecodeError):
                    continue
                constants = scan_text(text, SOURCE_SUFFIXES[path.suffix.lower()], path.relative_to(self.root).as_posix())
                self._cache[path] = (stat.st_mtime_ns, stat.st_size, constants)
            result.extend(constant for constant in constants if constant.name.lower() in lowered)
        return result

    # canonical forms
    @staticmethod
    def _fabric_formula(parameter) -> str | None:
        if isinstance(parameter.value, str) and parameter.type in {"float", "int"}:
            try:
                return ex.canonical(ex.parse(parameter.value, "python"))
            except ex.ExpressionError:
                return None
        return None

    def status(self, fabric: Fabric, values: dict[str, Any]) -> list[dict[str, Any]]:
        parameters = {parameter.name.lower(): parameter for parameter in fabric.parameters}
        state = self._state()
        items = []
        for constant in self.scan(set(parameters)):
            parameter = parameters[constant.name.lower()]
            fabric_formula = self._fabric_formula(parameter)
            item: dict[str, Any] = {
                **asdict(constant), "key": constant.key, "parameter": parameter.name, "parameter_unit": parameter.unit,
                "baseline": state.get(constant.key), "expression": fabric_formula is not None,
                "fabric_formula": fabric_formula, "source_formula": None, "source_form": None, "fabric_form": None,
            }
            if parameter.type in {"str", "bool"} or parameter.name not in values:
                item.update(state="unsupported", fabric_value=None)
                items.append(item)
                continue
            quantity = values[parameter.name]
            if constant.formula is not None:
                mapped, unknown = self._to_fabric(constant.formula, parameters)
                item["source_formula"] = mapped
                item["fabric_value"] = float(quantity.magnitude)
                if unknown:
                    item.update(state="unresolved", detail=f"unknown identifier(s): {', '.join(sorted(unknown))}")
                    items.append(item)
                    continue
                if constant.unit and constant.unit != parameter.unit:
                    item.update(state="unit_mismatch", detail="formula constants must use the parameter's unit")
                    items.append(item)
                    continue
                try:
                    item["value"] = float(evaluate_expression(mapped, values).to(quantity.units).magnitude)
                except (ValueError, pint.errors.PintError, AttributeError):
                    item["value"] = None
                item["source_form"] = f"f:{mapped}"
                item["fabric_form"] = f"f:{fabric_formula}" if fabric_formula else f"v:{float(quantity.magnitude)!r}"
                item["source_unit"] = parameter.unit
                item["state"] = "in_sync" if item["source_form"] == item["fabric_form"] else "structural_drift"
                items.append(item)
                continue
            unit = constant.unit or parameter.unit
            try:
                fabric_value=float(quantity.to(Q_(1, unit if unit not in {"", "1"} else "dimensionless").units).magnitude)
            except (pint.errors.DimensionalityError, pint.errors.UndefinedUnitError, ValueError, TypeError, AttributeError):
                item.update(state="unit_mismatch", fabric_value=float(quantity.magnitude))
                items.append(item)
                continue
            item.update(fabric_value=fabric_value, source_unit=unit)
            item["source_form"] = f"v:{constant.value!r}"
            item["fabric_form"] = f"v:{fabric_value!r}"
            item["state"] = "in_sync" if _equal(fabric_value, constant.value) else "drift"  # type: ignore[arg-type]
            items.append(item)
        return items

    @staticmethod
    def _to_fabric(expression: str, parameters: dict[str, Any]) -> tuple[str, set[str]]:
        tree=ex.parse(expression, "python")
        unknown = {name for name in ex.names(tree) if name.lower() not in parameters}
        mapped = ex.rename(tree, lambda name: parameters[name.lower()].name if name.lower() in parameters else name)
        return ex.canonical(mapped), unknown

    def diff(self, fabric: Fabric, values: dict[str, Any]) -> list[dict[str, Any]]:
        """Group definitions per parameter and flag cross-file structural drift"""
        report: dict[str, dict[str, Any]] = {}
        for item in self.status(fabric, values):
            entry = report.setdefault(item["parameter"], {
                "parameter": item["parameter"], "fabric": item["fabric_form"], "definitions": [], "status": "in_sync",
            })
            entry["definitions"].append({
                "key": item["key"], "path": item["path"], "line": item["line"], "language": item["language"],
                "form": item["source_form"], "text": item["literal"], "state": item["state"],
            })
        for entry in report.values():
            forms = {d["form"] for d in entry["definitions"] if d["form"]}
            kinds = {form.split(":", 1)[0] for form in forms}
            states = {d["state"] for d in entry["definitions"]}
            if len(kinds) > 1:
                entry["status"] = "mixed_forms"
            elif len(forms) > 1 and not all(_same_form(a, b) for a in forms for b in forms):
                entry["status"] = "cross_file_drift"
            elif states - {"in_sync"}:
                entry["status"] = sorted(states - {"in_sync"})[0]
        return sorted(report.values(), key=lambda entry: entry["parameter"])

    # writing
    def _render_for(self, item: dict[str, Any], fabric: Fabric, parameters: dict[str, Any], identifiers: dict[str, str]) -> tuple[str, str]:
        constant = SourceConstant(**{name: item[name] for name in SourceConstant.__dataclass_fields__})
        parameter=fabric.by_name()[item["parameter"]]
        fabric_formula = item["fabric_formula"]
        if constant.formula is not None and fabric_formula is not None:
            tree = ex.parse(fabric_formula, "python")
            def source_name(name: str) -> str:
                return identifiers.get(name.lower(), name.upper() if constant.language != "openscad" else name)
            rendered = ex.render(ex.rename(tree, source_name), constant.language, constant.float_type)
            return rendered, f"f:{fabric_formula}"
        literal = format_literal(item["fabric_value"], constant, parameter.type == "int")
        return literal, f"v:{float(literal.replace('_', ''))!r}"

    def push(self, fabric: Fabric, values: dict[str, Any], keys: set[str] | None = None) -> list[dict[str, Any]]:
        """Write the fabric into source constants (all drifting ones, or `keys`)"""
        items=[
            item for item in self.status(fabric, values)
            if item["state"] in {"drift", "structural_drift", "conflict"} and (keys is None or item["key"] in keys)
            and (keys is not None or item["state"] != "conflict")
        ]
        parameters = {parameter.name.lower(): parameter for parameter in fabric.parameters}
        by_path: dict[str, list[tuple[dict[str, Any], SourceConstant, str, str]]] = {}
        for item in items:
            constant = SourceConstant(**{name: item[name] for name in SourceConstant.__dataclass_fields__})
            by_path.setdefault(constant.path, []).append((item, constant, "", ""))
        state = self._state()
        written = []
        for relative, entries in by_path.items():
            path = self.root / relative
            original = _read(path)
            identifiers = {c.name.lower(): c.name for c in scan_text(original, entries[0][1].language, relative)}
            replacements = []
            for item, constant, _, _ in entries:
                if original[constant.start : constant.end] != constant.literal:
                    continue
                text, form = self._render_for(item, fabric, parameters, identifiers)
                replacements.append((constant, text))
                state[constant.key] = form
                written.append({"path": relative, "name": constant.name, "line": constant.line, "value": text})
            if replacements:
                _write(path, inject(original, replacements))
            self._cache.pop(path, None)
        self._save_state(state)
        return written

    def pull(self, fabric: Fabric, values: dict[str, Any], keys: set[str], *, allow_structural: bool = False) -> tuple[Fabric, list[dict[str, Any]]]:
        """Copy source definitions (values or formulas) into the fabric"""
        items=[
            item for item in self.status(fabric, values)
            if item["key"] in keys and item["state"] in {"drift", "structural_drift", "conflict"}
        ]
        document = fabric.model_dump(mode="json")
        by_name = {parameter["name"]: parameter for parameter in document["parameters"]}
        state = self._state()
        applied, chosen = [], {}
        for item in items:
            parameter = by_name[item["parameter"]]
            if item["source_formula"] is not None:
                new_value: Any = item["source_formula"]
                form = f"f:{item['source_formula']}"
            else:
                if item["expression"] and not allow_structural:
                    raise ValueError(
                        f"{item['parameter']} is a formula; edit its inputs instead of pulling a literal from source"
                    )
                value = item["value"]
                if item["source_unit"] != parameter["unit"]:
                    value = float(Q_(value, item["source_unit"]).to(parameter["unit"]).magnitude)
                new_value = int(round(value)) if parameter["type"] == "int" else value
                form = f"v:{float(item['value'])!r}"
            previous = chosen.get(item["parameter"])
            if previous is not None and not _same_form(previous, form):
                raise ValueError(f"{item['parameter']} has conflicting definitions in the selected sources")
            chosen[item["parameter"]] = form
            parameter["value"] = new_value
            state[item["key"]] = form
            applied.append({"parameter": item["parameter"], "value": new_value, "path": item["path"], "line": item["line"]})
        self._save_state(state)
        updated = Fabric.model_validate(document)
        return updated, applied

    def accept_in_sync(self, items: list[dict[str, Any]]) -> None:
        state = self._state()
        changed = False
        for item in items:
            if item["state"] == "in_sync" and state.get(item["key"]) != item["source_form"]:
                state[item["key"]] = item["source_form"]
                changed = True
        if changed:
            self._save_state(state)

    def reconcile(self, fabric: Fabric, values: dict[str, Any]) -> dict[str, Any]:
        """Apply one round of automatic three-way sync; conflicts are held, never resolved"""
        items = self.status(fabric, values)
        self.accept_in_sync(items)
        pull_keys: set[str] = set()
        push_keys: set[str] = set()
        conflicts: list[dict[str, Any]] = []
        for item in items:
            if item["state"] not in {"drift", "structural_drift"} or item["baseline"] is None:
                continue
            baseline = item["baseline"]
            if baseline.startswith("v:") and item["source_form"] and item["source_form"].startswith("v:") and item["state"] == "drift":
                # literal constants compare in the source's unit
                source_moved = not _same_form(item["source_form"], baseline)
                fabric_moved = not _same_form(f"v:{item['fabric_value']!r}", baseline)
            else:
                source_moved = not _same_form(item["source_form"], baseline)
                fabric_now = item["fabric_form"] if item["source_formula"] is not None else f"v:{item['fabric_value']!r}"
                fabric_moved = not _same_form(fabric_now, baseline)
            source_pullable = item["source_formula"] is not None or not item["expression"]
            if source_moved and not fabric_moved and source_pullable:
                pull_keys.add(item["key"])
            elif fabric_moved and not source_moved:
                push_keys.add(item["key"])
            else:
                item["state"] = "conflict"
                conflicts.append(item)
        forms: dict[str, set[str]] = {}
        for item in items:
            if item["key"] in pull_keys:
                forms.setdefault(item["parameter"], set()).add(item["source_form"])
        for item in items:
            if item["key"] in pull_keys and len(forms[item["parameter"]]) > 1:
                pull_keys.discard(item["key"])
                item["state"] = "conflict"
                conflicts.append(item)
        updated: Fabric | None = None
        pulled: list[dict[str, Any]] = []
        if pull_keys:
            updated, pulled = self.pull(fabric, values, pull_keys)
        pushed=self.push(fabric, values, push_keys) if push_keys else []
        return {"pulled": pulled, "pushed": pushed, "conflicts": conflicts, "fabric": updated}

    def resolve(self, fabric: Fabric, values: dict[str, Any], parameter: str, strategy: str) -> dict[str, Any]:
        """Resolve all definitions of one parameter with an explicit strategy.

        `fabric`          write the fabric's form into every source definition
        `source:<path>`   adopt that file's definition, then propagate it
        `newest`          adopt the most recently modified source definition
        `accept`          record the current state as the new baseline (no writes)
        """
        items = [item for item in self.status(fabric, values) if item["parameter"] == parameter]
        if not items:
            raise ValueError(f"no source definitions found for {parameter!r}")
        if strategy == "accept":
            state = self._state()
            for item in items:
                if item["source_form"]:
                    state[item["key"]] = item["source_form"]
            self._save_state(state)
            return {"fabric": None, "pulled": [], "pushed": []}
        if strategy == "fabric":
            return {"fabric": None, "pulled": [], "pushed": self.push(fabric, values, {item["key"] for item in items})}
        if strategy == "newest":
            chosen = max(items, key=lambda item: (self.root / item["path"]).stat().st_mtime_ns)
        elif strategy.startswith("source:"):
            path = strategy.split(":", 1)[1]
            chosen = next((item for item in items if item["path"] == path), None)
            if chosen is None:
                raise ValueError(f"{parameter!r} is not defined in {path!r}")
        else:
            raise ValueError(f"unknown strategy {strategy!r}; use fabric, newest, accept, or source:<path>")
        updated, pulled = (fabric, [])
        if chosen["state"] != "in_sync":
            updated, pulled = self.pull(fabric, values, {chosen["key"]}, allow_structural=True)
        from paramfabric.core.graph import ParameterGraph

        new_values = ParameterGraph(updated).evaluate()
        others = {item["key"] for item in items if item["key"] != chosen["key"]}
        pushed = self.push(updated, new_values, others) if others else []
        return {"fabric": updated if pulled else None, "pulled": pulled, "pushed": pushed}
