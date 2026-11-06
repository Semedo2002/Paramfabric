"""Pint-backed unit parsing, dimensional validation, and safe expressions"""

from __future__ import annotations

import ast
import operator
from typing import Any

import pint

ureg = pint.UnitRegistry(autoconvert_offset_to_baseunit=True)
Q_: Any = ureg.Quantity


class UnitMismatchError(ValueError):
    """Raised when a parameter expression has incompatible dimensions"""


def unit_quantity(value: int | float, unit: str) -> pint.Quantity:
    try:
        return Q_(value, ureg.dimensionless if unit in {"", "1"} else unit)
    except (pint.errors.UndefinedUnitError, TypeError, ValueError) as exc:
        raise UnitMismatchError(f"invalid unit {unit!r}: {exc}") from exc


def validate_unit(quantity: pint.Quantity, expected_unit: str, parameter_name: str) -> pint.Quantity:
    expected = unit_quantity(1, expected_unit)
    try:
        return quantity.to(expected.units)
    except pint.errors.DimensionalityError as exc:
        raise UnitMismatchError(
            f"{parameter_name}: expression has units {quantity.units}, "
            f"but declared unit is {expected_unit}"
        ) from exc


_BINARY_OPERATORS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
}
_UNARY_OPERATORS: dict[type[ast.unaryop], Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def expression_names(expression: str) -> set[str]:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid expression {expression!r}: {exc.msg}") from exc
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def evaluate_expression(expression: str, values: dict[str, pint.Quantity]) -> pint.Quantity:
    """Evaluate arithmetic over quantities without executing arbitrary Python"""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid expression {expression!r}: {exc.msg}") from exc

    def visit(node: ast.AST) -> Any:
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in values:
                raise ValueError(f"unknown parameter reference: {node.id}")
            return values[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
            left, right = visit(node.left), visit(node.right)
            try:
                return _BINARY_OPERATORS[type(node.op)](left, right)
            except pint.errors.DimensionalityError as exc:
                raise UnitMismatchError(
                    f"cannot apply {type(node.op).__name__} to {left.units} and {right.units}"
                ) from exc
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
            return _UNARY_OPERATORS[type(node.op)](visit(node.operand))
        raise ValueError(f"unsupported expression syntax: {type(node).__name__}")

    result = visit(tree.body)
    if not isinstance(result, pint.Quantity):
        return Q_(result, ureg.dimensionless)
    return result
