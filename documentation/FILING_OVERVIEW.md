# Studio Wiring

JSON-first workflow for studio device models, patch matrices, and generated visuals.

## Folder Overview
- `documentation/`: architecture, project structure, and extended reference material.
- `prototypes/`: all matrix/UI prototype HTML files.
- `defaults/`: reusable device templates and clean empty templates.
- `projects/`: per-project data/output folders.
- `json/`: optional scratch JSON files (for shared rules or ad-hoc imports).

## Core Docs
- `USER_MANUAL.md`
- `documentation/PROJECT_STRUCTURE.md`
- `DATA_FLOW.md` (canonical data-flow contract)
- `documentation/VISUAL_RULES.md` (wire-routing/rendering rules)

## Current Studio Sidecar Files
- `projects/studio-sidecar/device-configurations/basis.json`
- `projects/studio-sidecar/patch-configurations/basis/basis.json`
- `json/routing_rules.json`
- `routing_matrix.html`
- `projects/studio-sidecar/outputs/html/studio_wiring_point_to_point.html`
- `projects/studio-sidecar/outputs/svgs/*.svg`

## Prototypes
- `prototypes/routing_matrix_prototype_compact.html`

## Recommended Per-Project Workflow
Use a project folder (example):
- `projects/studio-sidecar/device-configurations/basis.json`
- `projects/studio-sidecar/patch-configurations/basis/basis.json`
- `projects/studio-sidecar/outputs/html/`
- `projects/studio-sidecar/outputs/svgs/`

Run the server against a project:
```bash
python3 routing_matrix_server.py \
  --host 127.0.0.1 \
  --port 8000 \
  --root . \
  --model projects/studio-sidecar/device-configurations/basis.json \
  --connections projects/studio-sidecar/patch-configurations/basis/basis.json \
  --routing-rules json/routing_rules.json \
  --route-debug projects/studio-sidecar/outputs/debug/route-debug.json \
  --preview-html projects/studio-sidecar/outputs/html/studio_wiring_point_to_point.html \
  --preview-svg-dir projects/studio-sidecar/outputs/svgs
```

## Regenerate Visuals
```bash
python3 generate_point_to_point.py \
  --model projects/studio-sidecar/device-configurations/basis.json \
  --connections-json projects/studio-sidecar/patch-configurations/basis/basis.json \
  --routing-rules json/routing_rules.json \
  --debug-routes-json projects/studio-sidecar/outputs/debug/route-debug.json \
  --output projects/studio-sidecar/outputs/html/studio_wiring_point_to_point.html \
  --svg-dir projects/studio-sidecar/outputs/svgs \
  --matrix-output routing_matrix.html
```
