"""Validated parameter-fabric schema and file loading"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ParameterType = Literal["float", "int", "str", "bool"]


class Parameter(BaseModel):
    """A single engineering parameter and its downstream bindings"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    value: float | int | str | bool
    unit: str = "dimensionless"
    description: str = ""
    rationale: str = ""
    type: ParameterType = "float"
    tags: list[str] = Field(default_factory=list)
    bindings: dict[str, str] = Field(default_factory=dict)

    @field_validator("bindings")
    @classmethod
    def validate_bindings(cls, bindings: dict[str, str]) -> dict[str, str]:
        supported = {
            "cpp_header",
            "py_module",
            "rust",
            "latex",
            "markdown",
            "solidworks",
            "nx",
            "ansys",
            "openfoam",
            "comsol",
            "openscad",
        }
        unknown = set(bindings) - supported
        if unknown:
            raise ValueError(f"unsupported binding type(s): {', '.join(sorted(unknown))}")
        if any(not path.strip() for path in bindings.values()):
            raise ValueError("binding paths cannot be empty")
        return bindings

    @model_validator(mode="after")
    def validate_value_type(self) -> Parameter:
        is_expression = isinstance(self.value, str) and self.type in {"float", "int"}
        if self.type == "str" and not isinstance(self.value, str):
            raise ValueError("string parameters require a string value")
        if self.type == "bool" and not isinstance(self.value, bool):
            raise ValueError("boolean parameters require a boolean value")
        if self.type == "int" and not is_expression and (
            isinstance(self.value, bool) or not isinstance(self.value, int)
        ):
            raise ValueError("integer parameters require an integer value or expression")
        if self.type == "float" and not is_expression and (
            isinstance(self.value, bool) or not isinstance(self.value, (int, float))
        ):
            raise ValueError("float parameters require a numeric value or expression")
        return self


class Fabric(BaseModel):
    """The complete source-of-truth set of parameters"""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    parameters: list[Parameter] = Field(default_factory=list)

    @field_validator("parameters")
    @classmethod
    def unique_parameter_names(cls, parameters: list[Parameter]) -> list[Parameter]:
        names = [parameter.name for parameter in parameters]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate parameter name(s): {', '.join(duplicates)}")
        return parameters

    def by_name(self) -> dict[str, Parameter]:
        return {parameter.name: parameter for parameter in self.parameters}


def load_fabric(path: str | Path = "params.yaml") -> Fabric:
    """Load a JSON or YAML fabric file with explicit parse errors"""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"parameter fabric not found: {source}")
    text=source.read_text(encoding="utf-8-sig")
    if source.suffix.lower() == ".json":
        data: Any = json.loads(text)
    else:
        data = yaml.safe_load(text)
    if data is None:
        data = {}
    if isinstance(data, list):
        data = {"parameters": data}
    if not isinstance(data, dict):
        raise ValueError("fabric file must contain an object or a parameters list")
    return Fabric.model_validate(data)
