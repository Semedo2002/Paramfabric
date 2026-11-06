"""Python AST extraction for numeric parameter assignments"""

from __future__ import annotations

import ast
import re
from pathlib import Path


def scan_python(path: Path) -> list[tuple[str, float, str | None]]:
    """Extract simple numeric assignments and optional `# [unit]` comments"""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    comments: dict[int, str] = {}
    for line_number, line in enumerate(source.splitlines(), start=1):
        match = re.search(r"#\s*\[([^\]]+)\]", line)
        if match:
            comments[line_number] = match.group(1).strip()
    records: list[tuple[str, float, str | None]] = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        value_node: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value_node = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value_node = [node.target], node.value
        if value_node is None or not isinstance(value_node, ast.Constant):
            continue
        if type(value_node.value) not in {int, float}:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                records.append(
                    (target.id, float(value_node.value), comments.get(node.lineno))
                )
    return records
