"""Serialized access to the process-global gmsh / OpenCASCADE session"""

from __future__ import annotations

import threading
from typing import Any

_LOCK = threading.RLock()


def available() -> bool:
    try:
        import gmsh  # noqa: F401
    except ImportError:
        return False
    return True


class GmshSession:
    """`with GmshSession() as gmsh:` - exclusive, cleared gmsh model state.

    `fresh=True` re-initializes gmsh completely. Writers need this: after a
    labelled STEP import, gmsh's exporters keep attribute state that makes
    them emit untransformed shapes, and `gmsh.clear()` does not reset it.
    """

    def __init__(self, fresh: bool = False) -> None:
        self.fresh = fresh

    def __enter__(self) -> Any:
        _LOCK.acquire()
        try:
            import gmsh

            if self.fresh and gmsh.isInitialized():
                gmsh.finalize()
            if not gmsh.isInitialized():
                gmsh.initialize(interruptible=False)
            gmsh.option.setNumber("General.Terminal", 0)
            gmsh.option.setNumber("General.Verbosity", 1)
            gmsh.clear()
            gmsh.option.setString("Geometry.OCCTargetUnit", "M")
            # Tight bounds (no tolerance gap) so dimension-driven push-pull lands exactly
            gmsh.option.setNumber("Geometry.OCCBoundsUseStl", 1)
            self.gmsh = gmsh
        except BaseException:
            _LOCK.release()
            raise
        return gmsh

    def __exit__(self, *_: Any) -> None:
        try:
            self.gmsh.clear()
        finally:
            _LOCK.release()


def import_shapes(gmsh: Any, path: Any) -> list[tuple[int, int]]:
    """Import STEP/IGES/BREP into the current model in metres; returns volume dim-tags.

    STEP and BREP honour `Geometry.OCCTargetUnit`; OpenCASCADE reads IGES in
    millimetres, so IGES is scaled exactly to metres. Surface-only models
    (common in IGES) are sewn into closed solids.
    """
    from pathlib import Path

    suffix = Path(str(path)).suffix.lower()
    occ=gmsh.model.occ
    if suffix in {".igs", ".iges"}:
        gmsh.option.setString("Geometry.OCCTargetUnit", "")
        try:
            entities = occ.importShapes(str(path))
        finally:
            gmsh.option.setString("Geometry.OCCTargetUnit", "M")
        occ.synchronize()
        if entities:
            occ.dilate(entities, 0, 0, 0, 1e-3, 1e-3, 1e-3)
            occ.synchronize()
    else:
        entities = occ.importShapes(str(path))
        occ.synchronize()
    volumes = [entity for entity in entities if entity[0] == 3]
    surfaces = [tag for dim, tag in entities if dim == 2]
    if not volumes and surfaces:
        loop = occ.addSurfaceLoop(surfaces, sewing=True)
        volumes=[(3, occ.addVolume([loop]))]
        occ.synchronize()
    return volumes


def write_model(gmsh: Any, path: Any) -> None:
    """Write the current model; IGES is written in millimetres (its native unit).

    OpenCASCADE's IGES writer stores raw coordinates under a millimetre header
    and ignores transforms applied through shape locations, so IGES goes via a
    STEP file re-imported in millimetres. This clears the current model.
    """
    import tempfile
    from pathlib import Path

    if Path(str(path)).suffix.lower() not in {".igs", ".iges"}:
        gmsh.write(str(path))
        return
    with tempfile.TemporaryDirectory() as scratch:
        step = Path(scratch) / "model.step"
        gmsh.write(str(step))
        gmsh.clear()
        gmsh.option.setString("Geometry.OCCTargetUnit", "MM")
        try:
            gmsh.model.occ.importShapes(str(step))
            gmsh.model.occ.synchronize()
            gmsh.option.setString("Geometry.OCCTargetUnit", "")
            gmsh.write(str(path))
        finally:
            gmsh.option.setString("Geometry.OCCTargetUnit", "M")
