# Paramfabric Studio

Paramfabric Studio is a parametric CAD/CAE workbench built on a governed
parameter fabric. One unit-checked, dependency-tracked set of parameters
drives exact B-Rep geometry, unstructured meshes, OpenFOAM and native
structural analyses, generated code, and CAD exports. Edits made in source
code, including formulas, flow back into the same fabric.

The Studio is a dark, three-pane workbench (scene and fabric | 3D viewport |
inspector and physics) organized around four steps: **Geometry & Parameters →
Physics & Boundary Conditions → Mesh & Solver → Results & Post-Processing**.
The enterprise governance layer (reviewed change requests, immutable
baselines, RBAC, tamper-evident audit ledger, offline licensing) is
documented further down.

## Install

```console
python -m pip install -e ".[dev]"            # core: numpy, scipy, gmsh (OpenCASCADE), pywebview
python -m pip install -e ".[distributed]"    # Redis-backed remote workers
python -m pip install -e ".[s3]"             # boto3 for S3 (optional: a built-in SigV4 client is the fallback)
python -m pip install -e ".[all]"            # dev + desktop packaging + distributed + s3
pytest
```

The CAE stack (numpy ≥ 1.24, scipy ≥ 1.10, gmsh ≥ 4.11 with its bundled
OpenCASCADE, pywebview ≥ 4.0) is part of the core install. Extras:
`dev` (pytest), `desktop` (PyInstaller), `distributed` (redis), `s3` (boto3), `all`.

**CFD needs OpenFOAM**, installed separately. Paramfabric finds it in this
order:

1. `PARAMFABRIC_OPENFOAM`: `none` (disable), `docker` (image from
   `PARAMFABRIC_OPENFOAM_DOCKER_IMAGE`, default `opencfd/openfoam-default:2506`),
   `wsl:<distro>:<path to etc/bashrc>`, or a path to an OpenFOAM `etc/bashrc`;
2. `simpleFoam` on `PATH` (Linux, or an already sourced environment);
3. on Windows, every WSL distribution with `/usr/lib/openfoam/openfoam*/etc/bashrc`
   or `/opt/openfoam*/etc/bashrc` (for example `apt install openfoam2506-default`
   in Ubuntu on WSL 2).

Developed and tested against OpenFOAM v2506 (ESI/OpenCFD) in WSL Ubuntu.

## Architecture

```
STEP/IGES ──► OpenCASCADE B-Rep ──► regeneration (push-pull, fillet, chamfer, split, boolean)
                 │   persistent face/edge names      │
                 ▼                                   ▼
           STEP / IGES / STL export         gmsh: tets + prism inflation layers
                                                    │
                     ┌──────────────────────────────┼──────────────────────────────┐
                     ▼                              ▼                              ▼
     OpenFOAM simpleFoam (k-ω SST)       native solid FEA (Tet10/Hex20)    beam / shell FEA (Studio + API)  
     polyMesh writer → checkMesh →       scipy.sparse direct / AMG
     solve → fields, forces, y⁺, VTK
                     │                              │
                     └──────────► viewport payload + VTK artifacts ◄─────────┘
                              (task queue: threads locally, Redis workers remotely)
```

## Capabilities and their boundaries

| Area | What it does | Boundaries |
|---|---|---|
| **Exact B-Rep kernel** (`geometry/exact.py`) | OpenCASCADE, through gmsh's bundled OCCT binding. Reads STEP/IGES with XCAF labels, so assembly placements are applied and part paths and colours are kept. Faces stay exact (plane, cylinder, B-spline, …). Every parameter change **rebuilds the B-Rep by replaying operations**, never by moving mesh vertices. | Layer metadata is not exposed. Parasolid needs a licensed kernel (export STEP instead). |
| **Parametric operations** | Dimension bindings act as push-pull of the extreme planar faces (exact affine dilation when no planar face exists). Push/pull a selected face, extrude, rolling-ball fillet, chamfer, split (two exact solids), uniform scale, boolean cut/fuse, flow-volume extraction. | Operations replay in creation order; there is no sketcher or constraint solver. |
| **Persistent naming** | When a feature is created, each referenced face/edge is fingerprinted on the geometry it was picked from: surface/curve type, outward normal, edge count, and the types and normals of its neighbours. It is then ranked among entities with the same fingerprint (offset along the normal). Regeneration resolves the name first, so lengthening a part never moves a push-pull or fillet onto a nearer parallel face (tested). | When the fingerprint count changes (a feature split or merged faces), resolution falls back to the stored point and normal and can then pick a neighbour. Names are not tracked through OCC's modification history. |
| **Neutral export** | STEP (AP214, millimetres), IGES, and STL of the regenerated B-Rep. STEP and IGES round-trip at exact size in tests. | IGES is written face-wise; the importer sews faces back into closed solids. |
| **Meshing** (`meshing/`) | gmsh, body-fitted: Tet4/Tet10, all-hex Hex8/Hex20 (by subdivision), curvature-adaptive sizing, distance-threshold refinement at selected faces, graded far field for external flow. **Prism inflation layers** with the first height from a target y⁺ (flat-plate skin friction), N layers, and growth ratio; wall nodes stay on the exact surface. | Hexahedra come from subdivision, not hex-dominant meshing. Inflation layers are extruded along one smoothed normal field; very sharp concave corners may fall back to a mesh without layers (logged). |
| **Guided assistant** (`studio/static/knowledge.json`, `guide.js`) | A registry of every physics choice: turbulence models (k-ω SST, k-ε, Spalart–Allmaras, laminar), mesh types, solver engines (FVM vs LBM), boundary conditions, inflation layers, relaxation and tolerances, beams and shells. Each entry has a one-line summary, a y⁺ target, equations, use cases, pitfalls, and literature references. `?` badges and glass hover cards appear on labelled settings, and double-clicking opens a slide-over reference. *Compare* buttons open side-by-side tables (tet vs hex, LBM vs FVM, the four turbulence models). The **skill level** (top bar) controls guidance: *Beginner* shows every badge and applies conservative defaults (10 inflation layers at y⁺ = 1, k-ω SST); *Intermediate* shows cards on hover only; *Expert* hides help and unlocks α_U, α_p, α_turb, residual target, matrix tolerances, iteration count, and MPI processes. | The registry is guidance, not a substitute for validation. LBM is described for comparison only; Paramfabric has no LBM solver. |
| **Interactive post-processing** (`viewport.js`) | A cutting plane along X, Y, or Z (slider, flip) clips the bodies, mesh, and results in real time. A marching-tetrahedra section of the volume mesh is computed in the browser, so it shows interior cells (mesh step), pressure/speed/temperature contours (CFD), or von Mises stress (FEA) for the selected field. Velocity vectors are drawn as instanced arrows, scaled and coloured by magnitude, on the plane and at inlet/outlet patches. **Iso-surfaces** of any volume field (e.g. T = 310 K, \|U\| = 0.5 m/s) come from the same marching-tetrahedra core, with a level slider, coloured by the legend field. **Streamlines** are particle-traced in the browser (midpoint RK2 through a tetrahedron locator, barycentric velocity), seeded from the inlet patches, coloured by speed, with animated particles that move at the local transit time. They are drawn when the solver supplies none. | Volume meshes above 600k tetrahedra are not sent to the browser (use the VTK file). FEA sections are drawn on the undeformed mesh. Iso-surfaces are piecewise linear (as smooth as the mesh); streamlines stop at walls, stagnation points, and the domain edge. |
| **CFD: OpenFOAM** (`solvers/openfoam.py`) | The production CFD engine. The gmsh mesh is written as a native `constant/polyMesh`: upper-triangular face order, owner < neighbour, outward normals, patches grouped, wall patches in the `wall` group. A steady `simpleFoam` case: laminar, **k-ω SST** (`kqRWallFunction`/`omegaWallFunction`/`nutkWallFunction`), **k-ε** (`epsilonWallFunction`), or **Spalart–Allmaras** (ν̃ = 3ν inflow, `nutUSpaldingWallFunction`); inlet k and ω from turbulence intensity and length scale; linear-upwind momentum; GAMG pressure; optional `decomposePar` + `mpirun`. Function objects: `forceCoeffs`, `yPlus`, `wallShearStress`, and legacy-VTK `surfaces` (walls plus a mid-plane cut). Runs `checkMesh` first, streams residuals live, and stops gracefully on cancel (`stopAt writeNow`). Reads back fields, force coefficients, y⁺, and VTK surfaces for the WebGL viewport, and writes `flow.vtk`. Convergence means residuals below the tolerance, or Cd steady within ±0.5% over the last 20% of iterations. *Export case* writes the same case with an `Allrun` script. | Steady incompressible only (no PISO/PIMPLE transients or multiphase; heat transfer is the CHT row below). The flow direction is +X with a box far field (external) or selected inlet/outlet faces (internal). Wall functions assume y⁺ is in their validity range; the summary reports min/mean/max y⁺ so you can check. |
| **CFD: conjugate heat transfer** (`solvers/openfoam.py`, `meshing/gmsh_mesher.py`) | `chtMultiRegionSimpleFoam` with a fluid and a solid region. gmsh fragments the flow box with the bodies, so the two region meshes share the interface node for node; each is written as its own `constant/<region>/polyMesh` with `mappedWall` interface patches. Coolant `heRhoThermo` + `rhoConst` (water, air, 50 % glycol) and solid `heSolidThermo` + `constIso` κ (aluminium, copper, steel). The interface uses `compressible::turbulentTemperatureCoupledBaffleMixed` (T and heat flux continuous). A heater face takes `externalWallHeatFluxTemperature` (`mode power`, total watts). A body fully immersed in the coolant has no unwetted face, so the power then goes in as uniform volumetric generation (`fvOptions` `scalarSemiImplicitSource` on h). Function objects report the mass-flow-weighted outlet temperature, the outlet mass flow, and the peak solid temperature. The summary checks the **energy balance** ṁ c<sub>p</sub> ΔT = Q and reports the thermal resistance. In the Studio: *Physics → Conjugate heat transfer* (coolant, solid, power, inlet T) and the *Heat source* face condition. Results show solid temperature, sections, iso-surfaces, and streamlines, and write `cht.vtk`. | Laminar, steady, constant properties. One solid material for all bodies. The peak solid temperature is mesh-sensitive on coarse meshes (321.8 K vs 313.3 K between convection schemes at 5 mm): refine until it stops changing. |
| **CFD: native (experimental)** (`solvers/fvm/`) | The earlier in-process SIMPLE solver (laminar / k-ε) remains selectable as `native3d`. It is used automatically only when no OpenFOAM is found, and then every result is marked *not reliable*. | Laminar internal flow agrees with analytic solutions; external k-ε disagrees with OpenFOAM (see validation). Do not use it for decisions. |
| **Structural: solid** (`solvers/fem/`) | Isoparametric Tet4/Tet10/Hex8/Hex20 (node order verified against gmsh). scipy.sparse assembly. Fixed supports, resultant forces, pressure (outward normals), gravity, thermal expansion. Direct sparse solve (minimum-degree ordering) up to 300k DOF, then CG. Nodal von Mises stress, reactions. Drives the Studio's FEA step. | Linear static, isotropic, small strain. No plasticity. |
| **Structural: modal & contact** (`solvers/fem/structural.py`, `solvers/structural.py`) | **Modal analysis**: consistent mass matrices (Tet4/Tet10/Hex8/Hex20 at exact quadrature orders; consistent 3D frame mass; lumped shell mass with rotary inertia), then `scipy.sparse.linalg.eigsh` in shift-invert mode with a small negative shift, so under-supported models still solve. Returns f<sub>n</sub>, unit-max mode shapes, and effective modal mass fractions along x, y, z. In the Studio, *Physics → Structural analysis → Modal* gives a frequency table, and each mode is a result field whose shape deforms the model (`modes.vtk`). **Contact** (`solve_contact`): small-sliding, frictionless node-to-surface penalty between face groups of separate bodies. Gap g = g₀ + n·(u<sub>s</sub> − u<sub>m</sub>), penalty k = factor·E·A<sub>trib</sub>/h, active-set iteration until no node changes state. | Modal: supports only (loads are ignored), linear, no pre-stress. Contact: Python API only (no Studio UI yet), no friction, no large sliding; the master surface uses its undeformed geometry. |
| **Structural: beam & shell** (`solvers/structural.py`, `solvers/members.py`) | 3D Euler–Bernoulli frame element (axial, two bending planes, torsion; rectangular, circular, tube, and I-beam sections or any A, Iy, Iz, J) and a 3-node facet shell (CST membrane + DKT bending + drilling stiffness). `solve_members` couples beams and shells that share nodes, with **mid-surface offsets** tied by rigid links (a flange offset on a web acts as a T-section). **In the Studio**, the viewport's *Edge* and *Face* selection modes are the assignment tools for FEA. Picking an edge opens the **1D beam** dialog: Rectangular, Circular, I-beam, or Tube, a to-scale SVG cross-section with dimensions, and live A, I<sub>y</sub>, I<sub>z</sub>, J computed with the same formulas as the solver (tested to 1e-12). Picking a face opens the **2D shell** dialog: thickness t and offset e, with a side-view preview of the mid-surface. FEA then meshes those entities with gmsh, applies the same face supports and loads (forces shared by member nodes on the face; pressure integrated over shells), solves, and shows beams as coloured lines and shells as contoured surfaces over a translucent solid. | Thin shells (Kirchhoff, no transverse shear); flat facets; no shear deformation in beams. Beam orientation defaults to global Z. Members are not mid-surfaced automatically: you pick the faces and edges. |
| **Solver manager** | `auto` resolves to OpenFOAM for CFD (any detected install), the native solid solver for FEA (or CalculiX when `ccx` is on PATH), and the 2D reference solvers if the CAE stack is missing. VTK results (`structure.vtk`, `flow.vtk`) download as task artifacts. | CalculiX gets the Cartesian voxel mesh, not the gmsh mesh. |
| **Deep code sync** (`auditors/`) | Constants *and formulas* in Python, C/C++ (`constexpr`, `#define`, `std::pow`), Rust (`const`, `.powf`, `_f64`) and OpenSCAD are parsed into one expression tree and compared structurally. Formulas sync as formulas, rendered per language with formatting and comments preserved. Cross-file drift report, plus resolution strategies (`fabric`, `source:<file>`, `newest`, `accept`). | Expressions are limited to the fabric grammar (`+ - * / **`, numbers, names). Constants must match parameter names. |
| **Task queue** (`server/tasks.py`) | Meshing and solves run as tasks: an in-process thread pool, or Redis with `paramfabric worker` on compute nodes. Progress and log streaming, cancellation, artifacts. | Redis is exercised in tests with an in-memory protocol double, not a live server. Workers need OpenFOAM installed locally; the workspace reaches them through object storage (below) or a shared disk. |
| **Artifact storage** (`server/storage.py`) | Every task artifact (`viewport.json`, `flow.vtk`, `structure.vtk`, `members.vtk`, `modes.vtk`, `cht.vtk`, `mesh.vtk`, `openfoam_case.zip`, `log.txt`) is published through one storage layer when the task ends: `LocalStorage` (in place, no copy) or `S3Storage` on any S3-compatible service (AWS S3, MinIO, Ceph) through `boto3` or `minio`. API servers fetch artifacts from the bucket on demand, so workers and servers need no shared artifact disk. | Tested against boto3 and minio protocol doubles, not a live bucket. Uploads happen once, when the task ends (no streaming of partial results). |
| **Workspace sync** (`server/workspace_sync.py`) | With remote storage, the API uploads what a mesh/solve task reads (`.paramfabric/workbench.json`, imported model records, source STEP/IGES and B-Rep) as content-addressed blobs plus a manifest (`workspaces/<org>/<project>/…`). Unchanged CAD is never uploaded twice. The task payload carries the manifest. The worker downloads it into a local cache, verifies every SHA-256, renames it into place atomically, and reuses it for later tasks; it then uploads the case archive, VTK, and `log.txt` when the task ends. `PARAMFABRIC_WORKSPACE_SYNC=auto\|always\|never`, `PARAMFABRIC_WORKSPACE_CACHE`. | The snapshot is taken at submission: later edits need a new task. Old blobs are not garbage-collected. |
| **Enterprise parity** | `/api/workbench` serves both modes. With enterprise auth, RBAC applies (`read`, `edit`, `simulate`, `export`), and edits that create parameters open **change requests** instead of writing. | Code sync stays local-only: the server doesn't own engineers' working copies. |
| **SaaS layer (0.5)** | Pull-request gate (`ci-check`) with signed approval locks and a blast-radius PR comment; sealed ISO 9001 / AS9100 HTML/PDF reports; organizations and projects with database-enforced boundaries; OIDC SSO; asynchronous signed webhooks; S3 artifacts through boto3, minio, or a built-in SigV4 client. | Tested against local stand-ins: a local OIDC issuer, an in-process S3 server, a local webhook receiver, and SQLite. Not yet run here against a live Okta/Entra tenant, AWS bucket, Redis server, or PostgreSQL. The Helm chart and Dockerfile were not rendered or built here (`docker compose config` validates). |
| **Asymmetric signing (0.6)** (`security/signing.py`) | Compliance reports and `paramfabric.lock` approvals are signed with **Ed25519**. Each organization's public keys live in the database (`signing_keys`, migration 0004) and are published at `GET /api/public/organizations/<org>/signing-keys` (JSON or `?format=pem`, no account needed). Auditors verify offline with `paramfabric report-verify report.pdf --public-key org.pem`, or upload to `POST /api/public/organizations/<org>/verify-report`. The CI gate verifies locks with only the public key. v0.5 HMAC records and seals still verify with `--legacy-key`. | Key rotation keeps old public keys (`revoked_at` is recorded but not yet enforced on verification). No timestamping authority: `signed_at` is the server's clock. |

### Validation

Every row is a test in the suite, or a run you can reproduce through the
Studio with the stated settings.

| Case | Result | Reference |
|---|---|---|
| gmsh tet + prism mesh → polyMesh | `checkMesh`: *Mesh OK*, upper-triangular ordering OK (real OpenFOAM in tests) | — |
| Demo bracket, external, air 10 m/s, Re 1.3×10⁵, k-ω SST, 51.7k cells (3 prism layers, mean y⁺ ≈ 4) | Cd = 1.144 on the bounding-box frontal area; Cd changed 0.2% between 150 and 1000 iterations; ±0.007% over the last 200 | qualitative (no published value) |
| Same bracket, native k-ε | Cd ≈ 1.8–1.9 where it converged | **disagrees with OpenFOAM by ~60%: native external CFD is not trustworthy** |
| Bracket, OpenFOAM, k-ε and Spalart–Allmaras (20 iterations, real runs in the test suite) | both run with their wall treatments; force coefficients finite | smoke test, not accuracy |
| Tee section: 100×5 mm shell flange offset 27.5 mm on a 5×50 mm beam web | tip deflection within 0.5% of composite beam theory; without the offset, 3.4× too flexible | Σ(I + A d²) |
| I-beam cantilever (member model) | deflection exact; root stress = M·c/I | Euler–Bernoulli |
| Browser section of a unit cube on X, Y, Z | area 1 exactly; linear fields interpolated exactly | analytic |
| Square duct, laminar, same 3 mm tet mesh in both solvers | OpenFOAM dp/dx +16%, native +10% | fRe = 56.91; both over-predict at ~7 cells per hydraulic diameter, so refine for internal-flow pressure drop |
| Plane Poiseuille, hex, native | dp/dx and u_max within 0.5% | 12μU/H², 1.5U |
| Cantilever 1000×100×50 mm, Tet10 / Hex20, 1 kN | tip deflection within 0.5%; reactions balance to 1e-6 N; root stress 12.3 MPa | Timoshenko; 12.0 MPa (M·c/I) |
| Free thermal expansion, Hex20, ΔT = 50 K | within 2% | αΔTL |
| Frame cantilever: axial, two bending planes, torsion; tilted member | exact to 1e-10 | PL/EA, PL³/3EI, TL/GJ |
| Simply supported square plate, uniform pressure, DKT shell | 8×8: −1.3%; 24×24: −0.1%; reactions balance exactly | w = 0.00406 q a⁴/D |
| Shell strip in a vertical plane, bending out of plane | within 1% | PL³/3EI |
| Cantilever 1000×100×50 mm, modal, Tet10 and Hex20 | f₁ (weak axis) 40.9 Hz, f₂ 81.2 Hz, within 2%; total mass exact; m<sub>eff,1</sub> = 0.61 | Euler–Bernoulli 40.77 / 81.54 Hz |
| Same cantilever as 40 frame elements | f₁, f₂, f₃ within 0.2%; m<sub>eff</sub> 0.613 and 0.188 | 40.77 / 81.54 / 255.5 Hz; 0.6131 / 0.1883 |
| Simply supported square plate 1 m × 10 mm, DKT, 24×24 | f₁₁ within 1%, degenerate f₁₂ = f₂₁ within 2% | f = π/2 (m² + n²)/a² √(D/ρh) = 47.98 Hz |
| Two stacked blocks (separate bodies), 1 MPa on top | contact force = p·A to 1e-6; penetration < 1e-7 m | equilibrium |
| Cantilever (1 m) closing onto a stop under the tip, 20 kN; gaps 0.5 / 5 / 20 mm | contact force within 15% of the rigid-stop beam estimate; open at 20 mm (free deflection 7.4 mm) | 3EI(δ − g)/a³ |
| CHT: 30×30×15 mm aluminium block in a water channel, 20 W, 0.02 m/s, real `chtMultiRegionSimpleFoam` | mass flow exact; energy balance closes to < 2% (3×10⁻⁵ at 3000 iterations); same for the immersed block with volumetric heating (0.09%) | ṁ c<sub>p</sub> ΔT = Q |
| Browser iso-surface \|x\| = 0.6 on a 24³ Kuhn tet grid | area within 3% of 4πr²; vertices within one cell; colour field exact | analytic |
| Browser RK2 tracer: rigid rotation; uniform flow | orbit radius drift < 2×10⁻³ over 400 steps; straight lines to the boundary | analytic |
| Push-pull 200 → 300 mm; fillet r = 4 mm | volume exact to 1e-6 | analytic |
| Stepped block lengthened 0.2 → 0.5 m after a push-pull of the lower end face | named face moved correctly; the point-and-normal selector alone moves the upper step instead | analytic |

Paramfabric is not a certified engineering tool: verify results before design
decisions.

## Studio workflow

**Desktop (Windows):** double-click [`Start Paramfabric.bat`](./Start%20Paramfabric.bat).
On first run it installs `.[desktop]`. It starts a loopback-only server and
opens the workbench with a Local Admin token injected, so there is no sign-in
prompt. `paramfabric-studio --no-window` serves without a window.

Pick a **skill level** in the top bar first: *Beginner* shows `?` badges on
every setting (hover for a summary and y⁺ target, double-click for equations
and references) and applies conservative CFD defaults; *Expert* hides the
help and unlocks the relaxation and tolerance controls.

1. **Geometry**: *Import CAD* (STEP/IGES exact; STL faceted) or *Load demo part*.
   Click a face for exact surface data, then *Push / pull*, *Refine mesh here*,
   or assign a boundary condition. Click an edge for *Fillet* / *Chamfer*. Each
   operation creates a fabric parameter (drag it in the morph dock) and a
   persistent name for the picked entity; the B-Rep regenerates after each
   change.
2. **Physics**: CFD or FEA. Inputs are fabric parameters (unit-checked).
   Assign fixed supports, forces, and pressures to faces (FEA), or inlets and
   outlets for internal flow. Thermal expansion inputs are optional. For a
   beam/shell model, switch the viewport selection to *Edge* (assign a beam
   section) or *Face* (assign a shell thickness and offset); the Physics panel
   lists the members and lets you switch back to the solid model.
3. **Mesh & Solver**: mesher (automatic / unstructured / Cartesian), elements
   (tet / hex), order, curvature refinement, inflation layers (y⁺, layers,
   growth), flow domain (external / internal), turbulence (automatic by
   Reynolds number / laminar / k-ω SST / k-ε / Spalart–Allmaras), with
   *Compare* tables next to the element and turbulence selectors and the
   engine list. The engine list shows the detected OpenFOAM install. Meshing
   and solving run as tasks with live logs and residual plots.
4. **Results**: wall pressure and wall shear, a mid-plane slice, and
   streamlines (CFD), with Cd, Cl, and y⁺; von Mises stress and displacement
   with a deformation scale (FEA). **Cut** opens the cutting plane: choose
   X/Y/Z, drag the slider to move through the domain, and *Vectors* toggles
   the velocity arrows. The VTK file and the zipped OpenFOAM case download for
   ParaView.

*Export Code/CAD* adds **STEP** and **IGES** of the regenerated geometry and
the body-fitted **OpenFOAM case**.

### Bi-directional code sync

Live sync reconciles every few seconds. For each constant it keeps a
baseline: if only the source changed, the source wins; if only the fabric
changed, the fabric wins; if both changed, it reports a conflict and never
overwrites. A `WING_SPAN = 5 * ROOT_CHORD` in Python syncs to the fabric
formula `wing_span = 5 * root_chord`. Editing that formula then rewrites
`5.0 * std::pow(ROOT_CHORD, 2.0)`-style expressions in C++ and
`ROOT_CHORD.powf(2.0)` in Rust. The *Drift report* lists parameters whose
definitions disagree across files and offers the resolution strategies.

### Remote execution

```console
# API server(s)
PARAMFABRIC_AUTH_REQUIRED=true PARAMFABRIC_REDIS_URL=redis://queue:6379/0 \
PARAMFABRIC_DATA_DIR=/shared/workbench paramfabric server --enterprise-auth ...
# compute nodes (same shared data directory, OpenFOAM installed)
PARAMFABRIC_REDIS_URL=redis://queue:6379/0 PARAMFABRIC_DATA_DIR=/shared/workbench paramfabric worker
```

With object storage, add the bucket settings to both servers and workers:

```console
PARAMFABRIC_S3_ENDPOINT=http://minio:9000 PARAMFABRIC_S3_BUCKET=paramfabric \
PARAMFABRIC_S3_ACCESS_KEY=... PARAMFABRIC_S3_SECRET_KEY=... paramfabric worker
```

Workers then need no shared data directory: each mesh/solve task carries a
content-addressed **workspace snapshot** (CAD files, workbench settings,
manifest) that the worker downloads, verifies, and caches before meshing or
running OpenFOAM. When the task ends, the case archive, VTK files, and the
full `log.txt` are uploaded back. `PARAMFABRIC_WORKSPACE_SYNC=always` forces
snapshots even with local storage, and `never` turns them off for shared disks.

Artifacts land under `<PARAMFABRIC_S3_PREFIX, default artifacts>/<task id>/`.
The bucket is created if it does not exist. `PARAMFABRIC_S3_CLIENT` picks the
client: `boto3` (install `.[s3]`), `minio`, or `native`, a built-in AWS
Signature V4 client with no dependency. `auto` (the default) tries them in
that order. All three clients stream uploads from disk and downloads to a
temporary file that is renamed into place. The native client switches to
multipart upload above 64 MB, retries 5xx/429/`SlowDown` with backoff
(`PARAMFABRIC_S3_ATTEMPTS`), and uses path-style addressing for custom
endpoints and virtual-hosted addressing for `*.amazonaws.com`
(`PARAMFABRIC_S3_ADDRESSING` overrides it).

`paramfabric worker --once` drains a single task; `--name` labels the node.
Set `solver_options.processors` to run OpenFOAM in parallel with MPI on a
worker. Task endpoints: `POST /api/workbench/mesh`,
`POST /api/workbench/solver/run`,
`GET /api/workbench/solver/jobs/{id}?residuals=&log=` (incremental),
`POST …/cancel`, `GET /api/workbench/tasks`, and
`GET /api/workbench/tasks/{id}/artifacts/{name}`. In enterprise mode, users
see only their own tasks; admins see all.

## CI/CD parameter gate

`paramfabric ci-check` compares the fabric at two Git revisions and fails the
build on blocking errors:

| Check | Blocks when |
|---|---|
| `unit-mismatch` | a value or formula is dimensionally inconsistent (Pint) |
| `circular-dependency` | formulas form a cycle |
| `invalid-expression`, `invalid-fabric` | unknown references, syntax errors, or a fabric that does not validate |
| `dimension-change` | a parameter changes physical dimension (for example m → s) |
| `unapproved-edit` | parameter definitions changed and `paramfabric.lock` has no signed approval for exactly this fabric |
| `approval-unverifiable` | approval is required but no public key is available (`--public-key` / `PARAMFABRIC_APPROVAL_PUBLIC_KEY`) |
| `source-drift` | Python/C++/Rust/OpenSCAD constants disagree with the fabric |

```console
paramfabric ci-check --base origin/main --head WORKTREE \
  --summary pr-summary.md --json result.json        # exit 0 = pass, 1 = blocking errors
paramfabric ci-check --base origin/main --approvals warn --drift warn   # advisory mode
```

The Markdown summary starts with `<!-- paramfabric-ci-check -->` so a CI job
can update one pull-request comment in place. It lists:
- every changed parameter (before → after, delta, and relative change);
- the blast radius (downstream parameters and their distance);
- the generated targets they feed (C++ headers, OpenFOAM dictionaries, CAD macros);
- CAD features and dimension bindings in the workbench;
- the simulation inputs they change (for example "OpenFOAM `0/U` inlet");
- every source constant by `file:line`.

On GitHub Actions it is also appended to the job summary.

**Approvals.** Reviews happen on the Paramfabric server:
1. After a change request is merged, fetch its signed approval record and
   commit it with `params.yaml`:

   ```console
   paramfabric approval-lock <change-request-id> --server https://paramfabric.example.com --token $TOKEN --project wing
   git add params.yaml paramfabric.lock
   ```

2. The record holds the merged fabric's SHA-256, the approvers, and the
   request id, signed with the organization's **Ed25519** key on the server
   (`algorithm`, `key_id`, `signature`). CI verifies it with the public key
   only, so no signing secret ever leaves the server.
3. Any later edit to `params.yaml` changes the hash, so the gate fails until
   a new review is merged. A tampered lock fails signature verification.

**GitHub Actions.** [`.github/workflows/paramfabric-gate.yml`](./.github/workflows/paramfabric-gate.yml)
is ready to use:
1. Add the repository **variable** `PARAMFABRIC_APPROVAL_PUBLIC_KEY` with the
   organization's public key(s):
   `curl https://<server>/api/public/organizations/<org>/signing-keys?format=pem`.
2. Make *Paramfabric gate / gate* a required status check.
3. Optionally set the repository variables `PARAMFABRIC_FABRIC`,
   `PARAMFABRIC_APPROVALS`, `PARAMFABRIC_DRIFT` and `PARAMFABRIC_PIP_SPEC`.

On every pull request the workflow:
- checks out the full history;
- runs the gate;
- posts or updates the PR comment;
- uploads `paramfabric-result.json`;
- fails the check on blocking errors.

The key is public, so approvals also verify on pull requests from forks.
v0.5 HMAC locks still verify when `PARAMFABRIC_APPROVAL_KEY` holds the old secret.

**GitLab CI:**

```yaml
paramfabric-gate:
  image: python:3.12
  rules: [{ if: $CI_PIPELINE_SOURCE == "merge_request_event" }]
  variables: { GIT_DEPTH: "0" }
  script:
    - pip install "${PARAMFABRIC_PIP_SPEC:-.}"
    - paramfabric ci-check --base "origin/$CI_MERGE_REQUEST_TARGET_BRANCH_NAME" --summary gate.md --json gate.json
  after_script:  # post gate.md as a merge-request note with a project access token
    - 'curl -sf -X POST -H "PRIVATE-TOKEN: $GITLAB_NOTE_TOKEN" --data-urlencode "body@gate.md" "$CI_API_V4_URL/projects/$CI_PROJECT_ID/merge_requests/$CI_MERGE_REQUEST_IID/notes" || true'
  artifacts: { when: always, paths: [gate.md, gate.json] }
```

## Compliance audit reports (ISO 9001 / AS9100)

One command (or the **Export Compliance Audit** buttons in the Studio sidebar
and the Export menu) produces a sealed, publication-ready report:

```console
paramfabric report --output audit_report.html --prepared-by "J. Quality"
paramfabric report --output audit_report.pdf --database $PARAMFABRIC_DATABASE_URL --organization acme --project wing
paramfabric report-verify audit_report.pdf --public-key acme.pem   # exit 0 = signature valid
paramfabric report-verify audit_report.pdf --keys-url https://paramfabric.example.com/api/public/organizations/acme/signing-keys
```

| Section | Content |
|---|---|
| 1. Executive summary | KPIs and pass/fail findings: Pint consistency, acyclic graph, traceability (description + rationale), audit-ledger chain, merged reviews, CFD/FEA convergence |
| 2. Parameter provenance | value, unit, description, design rationale, tags |
| 3. Dimensional unit verification | declared value, resolved value, SI base value, dimensionality, Pint status per parameter |
| 4. Impact analysis matrix | inputs, downstream parameters, generated targets, CAD features, simulation inputs, source constants |
| 5. Simulation evidence | latest CFD and FEA runs: engine, mesh statistics (cells, types, inflation layers, quality), Cd/Cl/drag/y⁺ or max von Mises/displacement/reactions, convergence criterion, residual convergence plot (log scale) |
| 6. Configuration management | merged change requests with approvers, immutable baselines, audit-ledger head and recent events |
| 7. Signature | Ed25519 signature over the canonical JSON of everything above, payload SHA-256, signing key id and public key, signer, time, standards references (ISO 9001:2015 §7.5, §8.3; AS9100D §8.1.3, §8.3.4) |

**HTML or PDF.**
- The HTML is self-contained, print-ready (A4 rules) and works at phone width.
- The PDF is written natively, with no external dependency: A4, standard
  fonts, vector tables and plots, running headers and page numbers.
- Both formats render only from the sealed data, and both embed that data:
  the HTML in a JSON block, the PDF in its document metadata.
- `report-verify` therefore detects any edited number, and any report signed
  by a key the auditor does not trust. Rendering is deterministic, so it also
  re-renders the document from the signed data and requires the file to match
  byte for byte: an edit to the visible pages or the PDF metadata fails even
  when the embedded data is untouched. Reports from other Paramfabric versions
  are checked on their signed data only (`document_intact` is then `null`). A forger who re-signs with their own key
  fails: the embedded public key is informational, never trusted.

**Keys.**
- The private key comes from `PARAMFABRIC_SIGNING_KEY` (PEM text or a path),
  then `PARAMFABRIC_SIGNING_KEYS_DIR/<org>.pem`, then
  `.paramfabric/signing-key.pem` (created on first use on a desktop).
  `PARAMFABRIC_SIGNING_KEY_PASSPHRASE` decrypts an encrypted key.
  `paramfabric keys generate --output key.pem` creates a pair;
  `paramfabric keys public` prints the public key.
- The server registers every public key it signs with under the organization,
  so verification needs no secret: anyone with the PEM (or the public endpoint)
  can check a report. In the Studio, *Verify a report…* checks a file through
  the public endpoint and *Public key* downloads the PEM for auditors.
- v0.5 HMAC-sealed reports verify with `--legacy-key`.

**Scope.**
- Simulation results come from the workspace's task artifacts. On a
  multi-tenant server they are limited to the caller's organization and project.
- Every Studio export is recorded in the audit ledger as `report.exported`.

## Multi-tenant SaaS deployment

**Organizations and projects.**
- Every change request, baseline, branch state, and simulation task belongs to
  one organization and one project.
- The organization comes from the access token (`org` claim, or the OIDC
  organization claim). The project comes from the `X-Paramfabric-Project`
  header, defaulting to `default`.
- Boundaries are enforced twice:
  - **in the database**, by composite foreign keys from every tenant-scoped
    table to `projects(organization_id, id)` (SQLite enforces them with
    `PRAGMA foreign_keys`);
  - **in every query**, by tenant filters. Another tenant's change requests,
    tasks, and artifacts answer 404.
- Each project has its own workbench workspace
  (`$PARAMFABRIC_DATA_DIR/tenants/<org>/<project>/`).
- Only the platform's default organization is seeded from the configured
  `params.yaml`.
- `paramfabric db-upgrade` applies migration `0003`, which moves existing data
  into `default/default`.

```console
GET  /api/organization                 # current organization, roles, projects
GET  /api/projects                     # projects of the caller's organization
POST /api/projects {"id": "wing", "name": "Wing programme"}   # admin
curl -H "Authorization: Bearer $TOKEN" -H "X-Paramfabric-Project: wing" .../api/change-requests
paramfabric token --subject ann --role admin --organization acme   # development tokens
```

Unknown organizations get 403 unless `PARAMFABRIC_AUTO_PROVISION_ORGS=true`
(first login creates the organization and its `default` project).

**Enterprise SSO (OIDC).**
- Set `PARAMFABRIC_OIDC_ISSUER` and `PARAMFABRIC_OIDC_AUDIENCE`. The API then
  accepts the identity provider's JWTs as bearer tokens, next to
  Paramfabric's own HS256 tokens.
- Keys come from the issuer's discovery document and JWKS. They are cached,
  and re-fetched when a token arrives with an unknown `kid` (key rotation).
- Only asymmetric algorithms are accepted. `iss`, `aud`, `exp`, `nbf`, and
  `iat` are enforced with 60 s leeway.

| Provider | Issuer | Roles claim | Organization claim |
|---|---|---|---|
| Okta | `https://<org>.okta.com/oauth2/default` | `groups` | custom `org_id`, or fixed `PARAMFABRIC_OIDC_ORGANIZATION` |
| Microsoft Entra ID | `https://login.microsoftonline.com/<tenant>/v2.0` | `roles` (app roles) | `tid` |
| Google Workspace | `https://accounts.google.com` | (none: use `PARAMFABRIC_OIDC_DEFAULT_ROLE`) | `hd` (domain) |
| Keycloak | `https://<host>/realms/<realm>` | `realm_access.roles` | custom claim |

Configure the claim mapping with:
- `PARAMFABRIC_OIDC_ROLES_CLAIM`;
- `PARAMFABRIC_OIDC_ROLE_MAP` (JSON `{"idp-group": "engineer"}`);
- `PARAMFABRIC_OIDC_DEFAULT_ROLE` (default `viewer`);
- `PARAMFABRIC_OIDC_ORG_CLAIM`;
- `PARAMFABRIC_OIDC_ORG_MAP` (JSON `{"<tid>": "acme"}`; unmapped values are slugified).

**Webhooks.**
- Set `PARAMFABRIC_WEBHOOK_URL` (comma-separated) and `PARAMFABRIC_WEBHOOK_SECRET`.
- Events: `parameter.merged`, `simulation.completed`, `simulation.failed`, and
  `drift.detected` (sent once per newly drifted constant).
- Delivery is asynchronous, from a background thread, and the API and workers
  both send it.
- Every event carries the organization and project and the
  `X-Paramfabric-Event` / `X-Paramfabric-Delivery` headers.
- Requests are signed: `X-Paramfabric-Signature: t=<time>,v1=<HMAC-SHA256 of "t.body">`.
  Verify with `paramfabric.server.webhooks.verify_signature`, which includes a
  replay window.
- Delivery retries with exponential backoff on network errors, 5xx, and 429.
  Other 4xx responses are not retried.

**Docker Compose.**
- `docker-compose.yml` runs:
  - the API;
  - `paramfabric worker` replicas (`PARAMFABRIC_WORKERS`, default 2);
  - Redis (append-only);
  - PostgreSQL;
  - optionally MinIO, with `--profile minio`.
- The API and workers share the workspace volume. Artifacts go to S3 when
  `PARAMFABRIC_S3_*` is set.
- The image includes the Redis and boto3 extras and gmsh's runtime libraries.
- For OpenFOAM CFD on workers, build a worker image with OpenFOAM installed.
  Otherwise CFD falls back to the native solver and is flagged as not reliable.

```console
export PARAMFABRIC_DATABASE_URL=postgresql+psycopg://paramfabric:...@postgres:5432/paramfabric
export PARAMFABRIC_JWT_SECRET=... PARAMFABRIC_AUDIT_KEY=... PARAMFABRIC_APPROVAL_KEY=... POSTGRES_PASSWORD=...
export PARAMFABRIC_S3_ENDPOINT=https://s3.eu-central-1.amazonaws.com PARAMFABRIC_S3_BUCKET=acme-paramfabric \
       PARAMFABRIC_S3_REGION=eu-central-1 PARAMFABRIC_S3_ACCESS_KEY=... PARAMFABRIC_S3_SECRET_KEY=...
export PARAMFABRIC_OIDC_ISSUER=https://acme.okta.com/oauth2/default PARAMFABRIC_OIDC_AUDIENCE=api://paramfabric
docker compose up -d --build
```

**Helm.** `deploy/helm/paramfabric` (chart 0.6.0) renders:
- the API Deployment;
- a worker Deployment (`worker.enabled`, which requires `redis.enabled`);
- ConfigMap entries for tenancy, OIDC, S3, and webhooks;
- secret references for the JWT/audit/approval/report/webhook keys, the Redis
  URL, and the S3 credentials. Optional keys are skipped when absent.

With workers enabled, set `persistence.accessModes: [ReadWriteMany]` so API
and worker pods share tenant workspaces.

```console
helm upgrade --install paramfabric deploy/helm/paramfabric \
  --set redis.enabled=true --set worker.enabled=true --set worker.replicas=4 \
  --set objectStorage.s3.enabled=true --set objectStorage.s3.bucket=acme-paramfabric \
  --set oidc.enabled=true --set oidc.issuer=https://login.microsoftonline.com/<tid>/v2.0 \
  --set oidc.audience=api://paramfabric --set oidc.orgClaim=tid --set persistence.accessModes={ReadWriteMany}
```

## Local development

Python 3.11+ is required.

```console
python -m pip install -e ".[dev]"
pytest
```

To build the standalone Windows executable:

```console
python -m pip install -e ".[desktop]"
python -m PyInstaller --onefile --windowed --name ParamfabricStudio --collect-all paramfabric --collect-all webview --collect-all gmsh paramfabric/desktop.py
```

Put `dist/ParamfabricStudio.exe` in the workspace folder whose fabric,
geometry, and source files it should use. Local state (database, signing
keys, audit ledger, imported geometry, solver cases, sync baselines) lives
in `.paramfabric/` in that folder. Local mode accepts loopback connections
only and is intended for one trusted user.

Initialize and validate a fabric from the command line:

```console
paramfabric init
paramfabric check
paramfabric tree
paramfabric build
```

The example with engineering, CAD, and solver bindings is at
[`examples/params.yaml`](./examples/params.yaml).

## Enterprise server quickstart

Token authentication is enabled explicitly with `--enterprise-auth` or
`PARAMFABRIC_AUTH_REQUIRED=true`. Use randomly generated JWT and audit secrets
of at least 32 bytes each:

```powershell
$env:PARAMFABRIC_JWT_SECRET = "<random secret, at least 32 bytes>"
$env:PARAMFABRIC_AUDIT_KEY = "<different random secret, at least 32 bytes>"
$env:PARAMFABRIC_DATABASE_URL = "sqlite:///paramfabric-enterprise.db"
paramfabric server --enterprise-auth --host 127.0.0.1 --port 8000 --fabric examples/params.yaml
```

Open `http://127.0.0.1:8000/studio`. For a local demonstration, mint short-lived
tokens from the configured signing secret:

```powershell
paramfabric token --subject alice --role engineer
paramfabric token --subject bob --role approver
paramfabric token --subject admin --role admin
```

The local token command is for development/operations under control of the
signing key. Production deployments should have their trusted identity service
issue tokens and should not distribute the signing secret to end users. Tokens
must contain `sub`, `roles`, `iss=paramfabric`, and `aud=paramfabric-api`.

The built-in default roles are:

| Role | Permissions |
|---|---|
| `viewer` | Read and export all tags |
| `engineer` | Read, propose edits, export, and run simulations on all tags |
| `approver` | Read and approve/reject all tags |
| `admin` | Read, edit, approve, merge, export, simulate, and administer all tags (including projects) |

Configure stricter role/tag maps through the `RBACPolicy` API when creating the
server app. A grant pattern such as `thermal.*` matches parameter tags such as
`thermal.loads`; actions are independently scoped.

### Reviewed REST workflow

The server stores pending proposals and merged branch state in SQL. There is no
direct parameter-write endpoint:

```powershell
$env:PARAMFABRIC_TOKEN = "<engineer token>"
paramfabric sync push --url http://127.0.0.1:8000 --rationale "Updated wing sizing"
paramfabric sync requests --url http://127.0.0.1:8000

$env:PARAMFABRIC_TOKEN = "<approver token>"
paramfabric sync review <request-id> --url http://127.0.0.1:8000 --decision approve

$env:PARAMFABRIC_TOKEN = "<admin token>"
paramfabric sync merge <request-id> --url http://127.0.0.1:8000
paramfabric sync pull --url http://127.0.0.1:8000
paramfabric baseline create v2.1.0-flight-hardware --url http://127.0.0.1:8000
paramfabric baseline list --url http://127.0.0.1:8000
paramfabric baseline diff v1.0 v2.1.0-flight-hardware --url http://127.0.0.1:8000
```

Supply `--token` or set `PARAMFABRIC_TOKEN`. All API routes except `/health`
and the Studio shell require a bearer token; Studio API requests are
authenticated using the token entered into the UI. In enterprise mode the
Studio edits parameters through change requests; CAD, meshing, solver, and
code-sync tools are available only in the local desktop mode. The server refuses
startup unless separate `PARAMFABRIC_JWT_SECRET` and
`PARAMFABRIC_AUDIT_KEY` values are configured.

Core endpoints include `GET /api/parameters`, `GET /api/graph`,
`POST/GET /api/change-requests`, request `/review` and `/merge`, baseline
creation/list/diff, `GET /api/audit`, and `GET /api/export/{kind}`.

## Engineering source format

```yaml
version: 1
parameters:
  - name: root_chord
    value: 2.0
    unit: m
    type: float
    description: Wing root chord.
    rationale: Initial reference geometry.
    tags: [geometry, aerodynamics]
    bindings:
      cpp_header: generated/params.hpp
      py_module: generated/params.py
      markdown: docs/parameters.md
      solidworks: generated/update_solidworks.bas
      nx: generated/update_nx.py
      ansys: generated/params.inp
      openfoam: generated/paramfabricDict
      comsol: generated/params.java
  - name: wing_span
    value: 5 * root_chord
    unit: m
    type: float
    description: Full wing span.
    rationale: Aspect ratio target.
    tags: [geometry, aerodynamics]
```

Expressions support numbers, references, `+`, `-`, `*`, `/`, `**`, and unary
signs. The evaluator does not execute Python. Declared dimensions are verified
with Pint; for example, adding meters to seconds or declaring `kg*m/s` as
newtons fails validation.

## CAD/solver exports

`paramfabric build` generates each file based on the binding key:

| Binding | Generated output |
|---|---|
| `solidworks` | VBA macro to update global variables in an open model |
| `nx` | NXOpen journal for named expressions |
| `ansys` | APDL scalar parameter input |
| `openfoam` | OpenFOAM dictionary with resolved scalars |
| `comsol` | COMSOL Java API parameter statements |
| `openscad` | OpenSCAD top-level variable assignments |
| `cpp_header`, `py_module`, `rust`, `latex`, `markdown` | Software/docs |

Parameter values retain their declared units in generated output/comments.
Vendor releases differ in script APIs, unit spellings, and document conventions;
the generated scripts are starting points and must be run and reviewed in the
target product. Neutral CAD import (STEP/IGES/STL) is handled by the Studio
workbench described above.

## Offline licensing

The verifier accepts a compact RS256 JWT license containing `license_id`,
`seats`, `features`, `iat`, and `exp`, signed by the issuer's RSA-2048 key.
Only the public key belongs on the server. To enforce licensing at startup,
provide the license and public-key path:

```powershell
$env:PARAMFABRIC_LICENSE_REQUIRED = "true"
$env:PARAMFABRIC_LICENSE = "<issuer-signed compact license>"
$env:PARAMFABRIC_LICENSE_PUBLIC_KEY = "C:\secure\license-public.pem"
```

Feature flags gate Studio, governance, CAD, and simulation API capabilities.
The private signing key must remain with the license issuer and must never be
included in this repository or deployment image.

## Deployment

See [Multi-tenant SaaS deployment](#multi-tenant-saas-deployment) for the
full stack (API, workers, Redis, PostgreSQL, S3, OIDC, webhooks) with Docker
Compose or Helm. The container runs `paramfabric db-upgrade` before starting,
so migrations apply automatically.

The API binds to loopback by default; put it behind a corporate TLS reverse
proxy before exposing it to a network. The Helm chart expects an
operator-managed Secret (database URL, JWT/audit keys, license values, and
the optional approval/report/webhook keys, Redis URL, and S3 credentials) and
a PostgreSQL service managed outside the chart.

The audit ledger is an append-only file on the shared volume, with
cross-process file locking. That is safe for one API replica, or several
replicas on one node. For API replicas on several nodes, use storage with
reliable POSIX locks or a database-backed ledger. Configure ingress TLS,
network policies, backups, and secret rotation in the target cluster.

## Python API

```python
from paramfabric import ParameterGraph, load_fabric
from paramfabric.governance.impact import blast_radius

fabric = load_fabric("params.yaml")
graph = ParameterGraph(fabric)
resolved = graph.evaluate()
impact = blast_radius(graph, {"root_chord"})
print(resolved["wing_span"], impact)
```

Beam and shell models:

```python
import numpy as np
from paramfabric.solvers.structural import FrameModel, Section, ShellModel, rectangle_mesh, solve_frame, solve_shell

nodes = np.c_[np.linspace(0, 2.0, 9), np.zeros(9), np.zeros(9)]
beam = FrameModel(nodes, np.c_[np.arange(8), np.arange(1, 9)], Section.rectangle(0.05, 0.1), young=2e11)
tip = solve_frame(beam, fixed={0: "all"}, loads={8: [0, 0, -1000, 0, 0, 0]}).displacement[8]

plate_nodes, triangles = rectangle_mesh(1.0, 1.0, 24, 24)
edge = np.isclose(plate_nodes[:, :2] % 1.0, 0).any(axis=1)
fixed = {i: [0, 1, 5] + ([2] if edge[i] else []) for i in range(len(plate_nodes))}
plate = solve_shell(ShellModel(plate_nodes, triangles, thickness=0.01, young=2e11), fixed, pressure=-1000)
```

## Tests

`pytest` covers, in addition to governance, security, licensing, and the
generators:

* **Geometry**: exact STEP/IGES import and export round trips; push-pull,
  fillet, split, and scale regeneration against analytic volumes; persistent
  face and edge names surviving dimension changes.
* **Meshing**: element node ordering against gmsh; prism layers growing into
  the fluid with wall nodes on the exact surface.
* **FEA**: Tet10/Hex20 cantilever, pressure, and thermal cases; frame
  elements (axial, bending, torsion, arbitrary orientation, mechanism
  detection); DKT rigid modes, a simply supported plate, and a shell strip.
* **OpenFOAM**: polyMesh ordering rules; SST, k-ε, and Spalart–Allmaras
  case dictionaries, expert relaxation and tolerances, and function objects;
  case archives; volume and inlet/outlet glyph payloads; field, patch, force-coefficient, and legacy-VTK readers; WSL,
  Docker, and sourced launchers; residual streaming and graceful cancel with
  a stand-in solver; engine resolution; body-fitted case export. When
  OpenFOAM is installed, real `checkMesh` and `simpleFoam` runs (k-ω SST,
  k-ε, Spalart–Allmaras) are included; otherwise they are skipped.
* **Members**: I-beam sections, beam stress recovery, the offset-shell tee
  section, and the Studio member API through to `members.vtk`.
* **Storage**: local and S3 storage (boto3 and minio doubles), and artifacts
  produced on a worker and fetched by an API node without a shared disk.
* **Studio assets**: knowledge-registry completeness, every `data-help-id` in
  the UI resolving to a topic, and (in Node, when installed) the browser
  slicer cutting exact areas, iso-surface area and level accuracy, the RK2
  particle tracer, and beam-section properties matching the solver.
* **Dynamics**: modal analysis of solid (Tet10, Hex20), frame and plate
  models against Euler–Bernoulli and Kirchhoff frequencies and effective
  masses; contact between stacked blocks and a cantilever closing onto a stop
  (three gaps); the Studio modal pipeline for solids and members.
* **CHT**: conforming region interfaces, case dictionaries (thermo, coupled
  BCs, mappedWall, heater or volumetric source), and, with OpenFOAM, real runs
  that close the energy balance, plus the Studio CHT pipeline end to end.
* **Workspace sync**: content addressing and deduplication, hash
  verification against tampering, a worker that meshes from the bucket after
  the original workspace is deleted, and API submission with snapshots.
* **Native CFD**: hex and tet Poiseuille flow.
* **CI gate**: real Git repositories with unchanged, unapproved, approved,
  tampered-lock, dimension-change, unit-mismatch, cyclic, invalid, committed,
  and newly added fabrics; CLI exit codes, Markdown/JSON/job-summary outputs;
  server-issued approval records.
* **Reports**: report content, HTML and PDF rendering (PDF cross-reference
  table, page structure, and decompressed text checked), Ed25519 verification
  with only the public key, tamper, untrusted-key and forged re-signature
  detection, v0.5 HMAC seals, CLI, the Studio endpoint, and the public
  verification endpoint.
* **Webhooks**: a real HTTP receiver, signatures and replay window, retry and
  no-retry rules, dead letters, and all four events from their real sources.
* **Tenancy and SSO**: store scoping, database-enforced boundaries, API
  isolation across organizations and projects, provisioning; OIDC against a
  local issuer (discovery, JWKS, rotation, audience/issuer/expiry/algorithm
  rejection, role and organization mapping) and through the API.
* **S3**: the AWS Signature V4 reference vector; an in-process S3 server that
  verifies every signature over HTTP (URI encoding, pagination, multipart,
  batch delete, retries, wrong credentials); artifacts moving between nodes
  through the bucket. A live-bucket test runs when
  `PARAMFABRIC_TEST_S3_ENDPOINT` is set.
* **Migrations**: upgrade of a populated 0002 database to 0003, foreign-key
  enforcement, and downgrade; the 0004 `signing_keys` table.
* **Platform**: formula sync across four languages, drift diffing, and
  resolution strategies; the local and Redis task queues (cross-thread
  worker, cancel, artifacts); end-to-end FEA and CFD through the API task
  queue.
