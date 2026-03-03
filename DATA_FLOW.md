# Studio Wiring Data Flow

Last updated: 2026-03-01

This is the canonical data-flow reference for:
- `generate_point_to_point.py` (generator)
- `routing_matrix.html` (interactive matrix UI)
- `routing_matrix_server.py` (save/regenerate API)
- JSON model/patch files

## 1. Folder Intent

- `instructions/`
  - Documentation and user guidance.
- `prototypes/`
  - Experimental matrix/layout HTML prototypes.
- `defaults/`
  - Reusable starter files and device templates.
- `projects/`
  - Per-studio project data and generated outputs.
- `json/`
  - Optional scratch/shared JSONs (not primary project storage).

## 2. File Responsibilities

### Application / generator
- `generate_point_to_point.py`
  - Reads model + connections + routing rules.
  - Generates matrix HTML, overview HTML, and SVG layer files.
- `routing_matrix_server.py`
  - Serves static files.
  - Provides save API (`/api/save-model`, `/api/save-connections`) and regenerate API (`/api/regenerate`).
  - Watches active config files (`model`, `connections`, `routing_rules`) and auto-regenerates visuals on file change.
  - Supports project APIs (`/api/projects`, `/api/create-project`, `/api/save-project`, `/api/set-targets`).
- `routing_matrix.html`
  - Interactive patch matrix and device editor.
  - Supports import/export and live-save when server is running.

### Documentation
- `instructions/USER_MANUAL.md`
- `instructions/PROJECT_STRUCTURE.md`
- `instructions/README.md`
- `VISUAL_RULES.md`
- `README_STUDIO_CABLING.md`

### Prototypes
- `prototypes/routing_matrix_prototype_compact.html`

### Defaults
- `defaults/default_template/studio_model_template_empty.json`
- `defaults/default_template/routing_matrix_connections_empty.json`
- `defaults/device_templates/studio_sidecar_common_devices.json`
- `defaults/device_templates/minimal_device_template.json`

### Project data (example)
- `projects/studio-sidecar/device-configurations/studio-model-001.json`
- `projects/studio-sidecar/patch-configurations/studio-model-001/patch-default.json`
- `projects/studio-sidecar/outputs/html/*.html`
- `projects/studio-sidecar/outputs/svgs/*.svg`
- `json/routing_rules.json` (global routing/label policy)

### Legacy/scratch JSONs
- `json/` is optional scratch/shared storage.
- Primary workflow uses `projects/<name>/...` paths.

## 3. Source Of Truth

- Studio device/port model: `studio-model.json`
- Patch links: `patch-*.json`
- Routing behavior/rules: `json/routing_rules.json` (global across projects)
- UI state persistence: stored in model under `ui_config`
  - Matrix save/pattern settings include:
    - `ui_config.matrix.allow_double_patching`

## 4. Data Flow

### A) Matrix UI runtime
1. UI loads embedded model/matrix snapshot.
2. In shell embedded mode, UI starts with an empty model/matrix to avoid startup flash before project selections load.
3. User edits devices, visibility, and patch links in browser memory.
4. If server API is available:
   - `Save Device Config` uses `POST /api/save-model` for the selected device config file.
   - `Save Device Config As` switches target path (`/api/set-targets`), then writes via `POST /api/save-model`.
   - `Save Patch` uses `POST /api/save-connections` for the selected patch config file.
   - `Save Patch As` / `Save Patch Config As` switches target path (`/api/set-targets`), then writes via `POST /api/save-connections`.
   - Server auto-regenerates visuals after save.
5. If files are edited outside the UI (on disk), server watcher detects mtime/size change and regenerates visuals automatically.

### B) Regeneration
1. Triggered either manually (`POST /api/regenerate`) or automatically by watcher/save hooks.
2. Regenerate uses selected model + selected connections + routing rules.
3. Generator produces:
   - `routing_matrix.html`
   - overview HTML
   - per-layer SVG files
   - optional route-debug JSON

### C) Patch binding / selector flow
1. Project selection exposes that project's device configs.
2. Device config selection filters available patch configs by `device_patch_map`.
3. New or changed device/patch pairing is persisted via `/api/save-project` (`device_patch_map`, defaults).

## 5. Save Model

Recommended per project:
- `projects/<name>/device-configurations/studio-model.json`

Recommended per patch set:
- `projects/<name>/patch-configurations/<device-config-stem>/patch-default.json`
- `projects/<name>/patch-configurations/<device-config-stem>/patch-<variant>.json`

This enables multiple patches for the same fixed studio model.

## 6. Project Structure Contract

```text
projects/
  <project-name>/
    project.json
    device-configurations/
      studio-model.json
      studio-model.template-empty.json
    patch-configurations/
      <device-config-stem>/
        patch-default.json
        patch-<variant>.json
    outputs/
      html/
      svgs/
```

Global:
- `json/routing_rules.json`

## 7. Important Rule

Whenever path conventions, save behavior, API endpoints, or generated outputs change, update this file in the same commit/change-set.
