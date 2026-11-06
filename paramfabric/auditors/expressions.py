"""Language-neutral expression trees for source ↔ fabric formula synchronization.

C++, Rust, and OpenSCAD constant initializers are normalized to Python syntax
and parsed with :mod:`ast`, giving one tree type for every language. Trees are
restricted to the fabric's expression grammar (numbers, identifiers, `+ - * /`,
`**`, unary signs) and can be rendered back into any supported language with
minimal parentheses and language-correct numeric literals.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable

_BINARY = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.Pow: "**"}
_PRECEDENCE = {ast.Add: 1, ast.Sub: 1, ast.Mult: 2, ast.Div: 2, ast.Pow: 4}
_UNARY_PRECEDENCE = 3
_NUMBER_SUFFIX = re.compile(r"(?<![\w.])((?:\d(?:[\d_]*\d)?)?\.?\d(?:[\d_]*\d)?(?:[eE][+-]?\d+)?)(?:_?(?:f32|f64|i8|i16|i32|i64|i128|u8|u16|u32|u64|u128|usize|isize)|[fFlLuU]+)\b")


class ExpressionError(ValueError):
    """Raised for expressions outside the synchronizable grammar"""


def _replace_calls(text: str, names: tuple[str, ...]) -> str:
    pattern=re.compile(r"(?:" + "|".join(re.escape(name) for name in names) + r")\s*\(")
    while True:
        match = pattern.search(text)
        if not match:
            return text
        depth, index, comma = 1, match.end(), None
        while index < len(text) and depth:
            char = text[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 1 and comma is None:
                comma = index
            index += 1
        if depth or comma is None:
            raise ExpressionError("unbalanced pow() call")
        base, exponent = text[match.end() : comma], text[comma + 1 : index - 1]
        text = f"{text[:match.start()]}(({base})**({exponent})){text[index:]}"


def _replace_method_pow(text: str) -> str:
    pattern = re.compile(r"\.pow[fi]\s*\(")
    while True:
        match = pattern.search(text)
        if not match:
            return text
        end = match.start()
        if text[end - 1] == ")":
            depth, start = 0, end - 1
            while start >= 0:
                depth += text[start] == ")"
                depth -= text[start] == "("
                if depth == 0:
                    break
                start -= 1
        else:
            start = end
            while start > 0 and (text[start - 1].isalnum() or text[start - 1] in "_."):
                start -= 1
        depth, index = 1, match.end()
        while index < len(text) and depth:
            depth += text[index] == "("
            depth -= text[index] == ")"
            index += 1
        base, exponent = text[start:end], text[match.end() : index - 1]
        text=f"{text[:start]}(({base})**({exponent})){text[index:]}"


def to_python_syntax(text: str, language: str) -> str:
    """Normalize a source-language initializer into Python expression syntax"""
    source = text.strip()
    if language == "python":
        return source
    if language in {"cpp", "rust"}:
        source = re.sub(r"\bstatic_cast\s*<[^>]*>\s*", "", source)
        source = re.sub(r"\(\s*(?:double|float|int|long)\s*\)", "", source)
        source = re.sub(r"\s+as\s+(?:f32|f64|i32|i64|u32|u64|usize|isize)\b", "", source)
        source = _NUMBER_SUFFIX.sub(r"\1", source)
        source = re.sub(r"(?<=\d)_(?=\d)", "", source)
    if language == "cpp":
        source = _replace_calls(source, ("std::pow", "pow"))
        source = source.replace("::", "_")
    elif language == "rust":
        source = _replace_method_pow(source)
        source = re.sub(r"\b(?:f64|f32)::consts::", "", source)
    elif language == "openscad":
        source=_replace_calls(source, ("pow",))
        source = source.replace("^", "**")
    return source


def parse(text: str, language: str) -> ast.expr:
    try:
        tree = ast.parse(to_python_syntax(text, language), mode="eval").body
    except SyntaxError as exc:
        raise ExpressionError(f"cannot parse expression {text!r}") from exc
    validate(tree)
    return tree


def validate(node: ast.AST) -> None:
    for child in ast.walk(node):
        if isinstance(child, ast.Constant):
            if type(child.value) not in {int, float}:
                raise ExpressionError("only numeric constants are supported")
        elif isinstance(child, ast.BinOp):
            if type(child.op) not in _BINARY:
                raise ExpressionError(f"unsupported operator {type(child.op).__name__}")
        elif isinstance(child, ast.UnaryOp):
            if not isinstance(child.op, (ast.USub, ast.UAdd)):
                raise ExpressionError("unsupported unary operator")
        elif not isinstance(child, (ast.Name, ast.Load, ast.operator, ast.unaryop)):
            raise ExpressionError(f"unsupported syntax {type(child).__name__}")


def names(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def is_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) or (
        isinstance(node, ast.UnaryOp) and isinstance(node.operand, ast.Constant)
    )


def literal_value(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):
        return float(node.value)
    sign=-1.0 if isinstance(node.op, ast.USub) else 1.0  # type: ignore[attr-defined]
    return sign * float(node.operand.value)  # type: ignore[attr-defined]


def rename(node: ast.expr, mapping: Callable[[str], str]) -> ast.expr:
    class Renamer(ast.NodeTransformer):
        def visit_Name(self, item: ast.Name) -> ast.Name:  # noqa: N802
            return ast.copy_location(ast.Name(id=mapping(item.id), ctx=ast.Load()), item)

        def visit_Constant(self, item: ast.Constant) -> ast.Constant:  # noqa: N802
            return ast.copy_location(ast.Constant(value=float(item.value)), item)

    return Renamer().visit(ast.parse(ast.unparse(node), mode="eval").body)


def canonical(node: ast.expr) -> str:
    return render(rename(node, lambda name: name), "fabric")


def _number(value: float, language: str, float_literals: bool) -> str:
    if float(value).is_integer() and not float_literals and language in {"fabric", "python", "openscad", "cpp"}:
        return str(int(value))
    text = f"{value:.15g}"
    if not any(char in text for char in ".eE") and language != "openscad":
        text += ".0"
    return text


def render(node: ast.expr, language: str, float_literals: bool = False) -> str:
    def emit(item: ast.AST, parent: int = 0, right: bool = False) -> str:
        if isinstance(item, ast.Constant):
            text=_number(float(item.value), language, float_literals or language == "rust")
            return text
        if isinstance(item, ast.Name):
            return item.id
        if isinstance(item, ast.UnaryOp):
            operand = emit(item.operand, _UNARY_PRECEDENCE)
            text = ("-" if isinstance(item.op, ast.USub) else "+") + operand
            return f"({text})" if parent > _UNARY_PRECEDENCE else text
        if isinstance(item, ast.BinOp):
            precedence = _PRECEDENCE[type(item.op)]
            if isinstance(item.op, ast.Pow) and language in {"cpp", "rust", "openscad"}:
                base, exponent = emit(item.left), emit(item.right)
                if language == "cpp":
                    return f"std::pow({base}, {exponent})"
                if language == "openscad":
                    return f"pow({base}, {exponent})"
                atomic = isinstance(item.left, (ast.Name, ast.Constant))
                return f"{base if atomic else f'({base})'}.powf({exponent})"
            if isinstance(item.op, ast.Pow):
                left = emit(item.left, precedence + 1)
                right_text = emit(item.right, precedence)
            else:
                left = emit(item.left, precedence)
                right_text = emit(item.right, precedence + 1)
            text = f"{left} {_BINARY[type(item.op)]} {right_text}"
            return f"({text})" if precedence < parent else text
        raise ExpressionError(f"cannot render {type(item).__name__}")

    return emit(node)
