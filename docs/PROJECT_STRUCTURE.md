# ELEVEN Slicer project structure

## Repository purpose

This repository is the canonical reconstruction point for ELEVEN Slicer. It is intentionally separating **source lineage**, **behavioral references**, **regression evidence**, and **future production code**.

## Current main snapshot

The current imported source on `main` is the recovered Google Drive snapshot whose README identifies it as **S1.1.10.0**, built on **S1.1.9.0** with optional wave panels.

It is a recoverable working snapshot, not a declaration that S1.1.10.0 is the final production version.

## Current top-level structure

```
ELEVEN-Slicer/
├─ README_FA.md
├─ INSTALL_FIRST.bat
├─ RUN_ELEVEN_WEBGL.bat
├─ requirements.txt
├─ start_eleven.py
├─ .gitignore
│
├─ eleven/
│  ├─ __init__.py
│  ├─ geometry.py
│  ├─ stl.py
│  ├─ models.py
│  ├─ validation.py
│  ├─ dxf.py
│  ├─ back_panel.py
│  ├─ rods.py
│  ├─ cache.py
│  └─ wave_panels.py
│
├─ static/
│  └─ index.html
│
├─ tests/
│  └─ test_core.py
│
└─ docs/
   ├─ MIGRATION_REPORT.md
   ├─ KNOWN_ISSUES.md
   ├─ REFERENCE_BUILDS.md
   └─ PROJECT_STRUCTURE.md
```

## Functional layers

### 1. Input / STL
Primary files:
- `eleven/stl.py`
- input handling in `start_eleven.py`

Responsibilities:
- STL parsing
- basic triangle validity
- mesh fingerprinting
- model loading/preflight hooks

### 2. Geometry and slicing core
Primary file:
- `eleven/geometry.py`

Responsibilities:
- dimension transforms
- orientation-dependent transforms
- plane intersections
- contour creation/repair
- slice generation
- numbering geometry helpers

This layer should become independent of assembly policy.

### 3. Model/state definitions
Primary file:
- `eleven/models.py`

Responsibilities:
- dimensions
- slice settings
- mesh and job structures
- part structures
- feature configuration

### 4. Assembly / back attachment
Primary file on current main:
- `eleven/back_panel.py`

Historical/reference implementations additionally contain:
- `assembly.py`
- `assembly_bridges.py`
- `bac_topology.py`

This is currently one of the highest-risk architectural areas because assembly behavior can affect visible geometry and topology.

### 5. Validation
Primary file:
- `eleven/validation.py`

Current checks include structural/output invariants, but final production validation must also protect visual silhouette/topology invariants.

### 6. DXF output
Primary file:
- `eleven/dxf.py`

Responsibilities:
- closed cut entities
- numbering/labels
- holes and supporting layers
- CAD/Corel-compatible export

DXF export should remain downstream of geometry and should not be used to repair topology problems.

### 7. UI / local web server
Primary files:
- `start_eleven.py`
- `static/index.html`

Responsibilities:
- model upload
- controls
- preview
- job execution
- downloadable output

### 8. Optional decorative features
Primary file:
- `eleven/wave_panels.py`

This feature is optional and must not modify the main slice geometry.

## Target architecture

```
Input STL
   │
   ▼
Mesh Preflight
   │
   ▼
Orientation + Dimensions
   │
   ▼
Common Slicing Core
   │
   ▼
Topology Classification
   │
   ├───────────────┐
   ▼               ▼
Gap-preserving     General back-attachment
assembly           assembly
   │               │
   └───────┬───────┘
           ▼
      Validation
           │
           ▼
        DXF export
```

## Non-negotiable design rules

1. Slicing geometry and assembly strategy are separate concerns.
2. Visible contour fidelity has priority over convenience of back-panel attachment.
3. No STL-specific hard-coded fix may become the general algorithm.
4. A fix is not considered complete until representative regression models pass.
5. Orientation must not arbitrarily change island topology.
6. Generated DXF files and test outputs are artifacts, not source code.
7. Historical ZIP timestamps must not be converted into fake Git history.
8. Reference builds with known strengths must be preserved even when they are not the main line.

## Current reference roles

- `S1.1.9.0 / S1.1.10.0`: recovered baseline lineage
- `ورژن اصلی با فاصله`: Angel / gap-preserving behavioral reference
- `S1.1.5.0 ورژن پایه اتصال به بک`: broad general back-attachment baseline
- `BAC`: topology-regression investigation/reference
- `C1`: targeted assembly and part-count experiments
- `NOPANEL`: no-back-panel experimental lineage

## Next repository cleanup milestone

Create a regression fixture set that represents behavior classes rather than individual fixes:

- Angel-like
- Pic-of-Omen-like
- complex organic model
- multiple-island model
- orientation/back-face matrix
- invalid/problematic STL preflight case

Then compare candidate assembly strategies against the same fixed fixture matrix before merging them into the production path.
