"""Physics and boundary-condition settings stored as fabric parameters.

Keeping physics inputs in the fabric means they are unit-checked by Pint,
participate in formulas, appear in the dependency graph, and sync to code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from paramfabric.core.schema import Fabric, Parameter
from paramfabric.core.units import ureg

ANALYSES = ("cfd", "fea")


@dataclass(frozen=True)
class PhysicsInput:
    name: str
    default: float
    unit: str
    description: str
    tag: str
    optional: bool = False  # absent -> default value, so older workspaces keep working


PHYSICS: dict[str, tuple[PhysicsInput, ...]] = {
    "cfd": (
        PhysicsInput("inlet_velocity", 10.0, "m/s", "Uniform inlet velocity along +X (velocity-inlet boundary).", "physics.cfd"),
        PhysicsInput("fluid_density", 1.225, "kg/m^3", "Fluid density used for Bernoulli pressure.", "physics.cfd"),
        PhysicsInput("kinematic_viscosity", 1.5e-5, "m^2/s", "Kinematic viscosity (OpenFOAM laminar solve).", "physics.cfd"),
    ),
    "fea": (
        PhysicsInput("youngs_modulus", 200.0, "GPa", "Young's modulus of the linear-elastic material.", "physics.fea"),
        PhysicsInput("poissons_ratio", 0.3, "dimensionless", "Poisson's ratio of the material.", "physics.fea"),
        PhysicsInput("applied_load", 1000.0, "N", "Default load: total force on the max-X face in −Z (min-X face clamped) unless face loads are assigned.", "physics.fea"),
        PhysicsInput("thermal_expansion", 1.2e-5, "1/K", "Linear thermal expansion coefficient (native 3D solver).", "physics.fea", True),
        PhysicsInput("temperature_change", 0.0, "K", "Uniform temperature change from the stress-free state (native 3D solver).", "physics.fea", True),
        PhysicsInput("material_density", 7850.0, "kg/m^3", "Material density (modal analysis: mass matrix).", "physics.fea", True),
    ),
}
MESH_FINENESS = PhysicsInput(
    "mesh_fineness_ratio", 24.0, "dimensionless",
    "Characteristic body length divided by the target cell size.", "mesh",
)

BOUNDARY_CONDITIONS: dict[str, list[dict[str, str]]] = {
    "cfd": [
        {"patch": "inlet (min X)", "type": "Velocity inlet", "value": "inlet_velocity"},
        {"patch": "outlet (max X)", "type": "Pressure outlet", "value": "p = 0 (gauge)"},
        {"patch": "sides", "type": "Slip wall", "value": "—"},
        {"patch": "body", "type": "Wall (no-slip in OpenFOAM, streamline in potential flow)", "value": "—"},
    ],
    "fea": [
        {"patch": "min-X face", "type": "Fixed support", "value": "u = 0"},
        {"patch": "max-X face", "type": "Force (−Z)", "value": "applied_load"},
    ],
}


class PhysicsError(ValueError):
    """Raised when physics parameters are missing or dimensionally wrong"""


def ensure_parameters(fabric: Fabric, analysis: str) -> list[Parameter]:
    if analysis not in PHYSICS:
        raise PhysicsError(f"unknown analysis type {analysis!r}")
    existing = fabric.by_name()
    return [
        Parameter(
            name=spec.name, value=spec.default, unit=spec.unit, type="float",
            description=spec.description, rationale="Default created by the Physics setup step.",
            tags=["physics" if spec.tag != "mesh" else "mesh", spec.tag],
        )
        for spec in (*PHYSICS[analysis], MESH_FINENESS)
        if spec.name not in existing
    ]


def resolve_physics(values: dict[str, Any], analysis: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for spec in (*PHYSICS[analysis], MESH_FINENESS):
        if spec.name not in values and spec.optional:
            result[spec.name] = float(ureg.Quantity(spec.default, spec.unit).to_base_units().magnitude)
            continue
        if spec.name not in values:
            raise PhysicsError(f"missing physics parameter {spec.name!r}; run the Physics step to create defaults")
        quantity = values[spec.name]
        reference = ureg.Quantity(1, "dimensionless" if spec.unit == "dimensionless" else spec.unit)
        if quantity.dimensionality != reference.dimensionality:
            raise PhysicsError(
                f"{spec.name} must have dimensions of {reference.dimensionality or 'dimensionless'} "
                f"(e.g. {spec.unit}), got {quantity.units}"
            )
        result[spec.name] = float(quantity.to_base_units().magnitude)
    return result
