"""Regex-based extraction of numeric C/C++ constants"""

from __future__ import annotations

import re
from pathlib import Path

_DECLARATION = re.compile(
    r"^\s*(?:(?:inline|static|constexpr|const|constinit)\s+)*"
    r"(?:[\w:<>]+\s+)+(?P<name>[A-Za-z_]\w*)\s*"
    r"(?:=\s*|)\s*(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*;",
    re.MULTILINE,
)
_DEFINE = re.compile(
    r"^\s*#\s*define\s+(?P<name>[A-Za-z_]\w*)\s+"
    r"(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)",
    re.MULTILINE,
)


def scan_cpp(path: Path) -> list[tuple[str, float, str | None]]:
    """Extract numeric constant definitions and optional `// [unit]` comments"""
    source = path.read_text(encoding="utf-8")
    lines=source.splitlines()
    records: list[tuple[str, float, str | None]] = []
    for pattern in (_DECLARATION, _DEFINE):
        for match in pattern.finditer(source):
            line_number = source.count("\n", 0, match.start()) + 1
            line = lines[line_number - 1]
            unit_match = re.search(r"//\s*\[([^\]]+)\]", line)
            records.append(
                (
                    match.group("name").lower(),
                    float(match.group("value")),
                    unit_match.group(1).strip() if unit_match else None,
                )
            )
    return records
