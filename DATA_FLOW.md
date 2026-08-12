# Studio Wiring Data Flow

Last updated: 2026-08-12

This is the canonical data-flow reference for:
- `generate_point_to_point.py` (generator)
- `routing_matrix.html` (interactive matrix UI)
- `routing_matrix_server.py` (save/regenerate API)
- JSON model/patch files

## 1. Folder Intent

- `documentation/`
  - Architecture, project-layout, and extended reference documentation.
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
  - Provides the transactional save API (`/api/save-transaction`) and regenerate API (`/api/regenerate`).
  - Watches active config files (`model`, `connections`, `routing_rules`) and auto-regenerates visuals on file change.
  - Supports project APIs (`/api/projects`, `/api/create-project`, `/api/save-project`).
  - Keeps the older per-file save and mutable-target endpoints for compatibility only.
- `routing_matrix.html`
  - Generated device editor, overview, visibility, and visual-preview application.
  - Supports import/export and live-save when server is running.
- `prototypes/routing_matrix_prototype_compact.html`
  - Canonical routing-matrix application loaded directly by the shell.
  - Owns patch editing, history, dirty state, and transactional patch saves.

### Documentation
- `README.md`
- `USER_MANUAL.md`
- `documentation/PROJECT_STRUCTURE.md`
- `documentation/VISUAL_RULES.md`
- `documentation/EXTENDED_USER_MANUAL.md`

### Defaults
- `defaults/default_template/studio_model_template_empty.json`
- `defaults/default_template/routing_matrix_connections_empty.json`
- `defaults/device_templates/studio_sidecar_common_devices.json`
- `defaults/device_templates/minimal_device_template.json`

### Project data (current example)
- `projects/studio-sidecar/device-configurations/basis.json`
- `projects/studio-sidecar/patch-configurations/basis/basis.json`
- `projects/studio-sidecar/outputs/html/*.html`
- `projects/studio-sidecar/outputs/svgs/*.svg`
- `json/routing_rules.json` (global routing/label policy)

### Legacy/scratch JSONs
- `json/` is optional scratch/shared storage.
- Primary workflow uses `projects/<name>/...` paths.

## 3. Source Of Truth

- Studio device/port model: the selected file under `projects/<project>/device-configurations/`
- Patch links: the selected file under `projects/<project>/patch-configurations/<model>/`
- Routing behavior/rules: `json/routing_rules.json` (global across projects)
- UI state persistence: stored in model under `ui_config`
  - Matrix save/pattern settings include:
    - `ui_config.matrix.allow_double_patching`
- Physical device placement: stored on each model device
  - missing `rack_mountable` means `false`; only the boolean value `true` makes a device eligible for the Rack Editor
  - missing `location` means `Desk`; otherwise `Desk` or `Rack`
  - missing `rack_units` means `1`; otherwise integer 1–16
  - `rack_position` is null/omitted for unplaced devices, or `{ "rack": 1..4, "start_u": 1..16 }`
  - `start_u` is the lowest occupied unit; the occupied range extends upward by `rack_units`

## 4. Data Flow

### A) Matrix UI runtime
1. The shell routes the Routing Matrix tab directly to the canonical matrix application.
2. The application reads `/api/config`, then loads the selected project, device config, and patch config.
3. User edits patch links in browser memory; the generated application owns device and visibility editing.
4. Normal saves use `POST /api/save-transaction` with an explicit `project_key`, explicit root-relative targets, and expected content hashes.
5. The server validates project containment, rejects stale writes with HTTP 409, commits all requested files together, and invokes regeneration at most once.
6. Configuration creation and Save As use the same transactional path after the user confirms the exact target and any overwrite.
7. Server watcher baselines are refreshed after a successful transaction so the same request does not cause a second regeneration.
8. If files are edited outside the UI, the watcher detects the change and regenerates visuals automatically.

### Rack Editor runtime

1. The shell routes `rack-editor` to the generated application with `tab=rack-editor`.
2. The editor reads physical placement from the selected device configuration.
3. Only devices with `rack_mountable: true` enter the editor inventory; their current `Desk` or `Rack` location and four 16U racks are rendered from the same in-memory model.
4. Placement validates mountability, rack bounds, and overlaps before mutating the model. Dropping an eligible Desk device into a rack changes its location to `Rack`.
5. Valid changes enter the normal model dirty/save pipeline; no separate rack-layout file is created.

### B) Regeneration
1. Triggered either manually (`POST /api/regenerate`) or automatically by watcher/save hooks.
2. Regenerate uses selected model + selected connections + routing rules.
3. Generator produces:
   - point-to-point overview HTML
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
      debug/
```

Global:
- `json/routing_rules.json`

## 7. Important Rule

Whenever path conventions, save behavior, API endpoints, or generated outputs change, update this file in the same commit/change-set.
