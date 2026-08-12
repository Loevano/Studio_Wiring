# User Manual

Last updated: 2026-08-12

## 1. What You Edit
- Device model: `device-configurations/studio-model.json`
- Connections (patch): `patch-configurations/<device-config-stem>/*.json`

## 2. Start The Local App
From project root:

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

Open:
- `http://127.0.0.1:8000/web/shell/index.html` (versioned tab shell)
- `http://127.0.0.1:8000/routing_matrix.html` (standalone single-file UI)

## 3. Build / Regenerate Visuals (CLI)

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

## 4. Save Strategy
- Keep one `studio-model.json` per studio.
- Keep many patch files per studio.
- Duplicate patch JSON before major repatches.
- Normal UI saves are transactional and reject a stale browser copy instead of overwriting a newer disk edit.
- If a save conflict is reported, reload the current config, review the newer data, and retry the edit.

## 5. Project / Config Selection Flow
- When no `Project` is selected:
  - `Device Config` and `Patch Config` selectors remain unavailable.
- When a `Project` is selected:
  - `Device Config` options for that project become available.
- When a `Device Config` is selected:
  - `Patch Config` options are filtered to the patch files bound to that device config.
- Creating a new project initializes:
  - Empty default device config.
  - Empty default patch config.

## 6. Save Buttons (UI)
- `Save Device Config`: overwrite the currently selected device config JSON.
- `Save Device Config As`: save model to a new device config JSON path.
- `Save Patch`: overwrite the currently selected patch config JSON.
- `Save Patch As` / `Save Patch Config As`: save patch to a new patch config JSON path.
- Create and Save As actions show the exact destination and require explicit confirmation before overwriting an existing file.
- The Routing Matrix shows Saved, Saving, or Unsaved state. Undo/redo covers patch edits, including one atomic step for stereo, range, and paint operations.

## 7. Patching Constraints
- `Allow Double Patching` toggle in Routing controls:
  - Off (default): each source/destination endpoint can only be part of one active connection.
  - On: a source or destination endpoint may have multiple active connections.

## 8. Create A New Studio Project
1. Choose `New Project` in the application.
2. Enter the project name and confirm the displayed destination.
3. Select the new project, then create or select its device and patch configurations.

For a manual setup, copy `projects/_template/` to `projects/<new-name>/` and edit its `project.json`.

## 9. Device Templates
- Folder: `defaults/device_templates/`
- Use these as copy/paste sources for `devices[]` in the model file.
- `minimal_device_template.json` is a clean starting object.

## 10. Defaults / Empty Canvas
- Empty model: `defaults/default_template/studio_model_template_empty.json`
- Empty patch: `defaults/default_template/routing_matrix_connections_empty.json`

## 11. Prototypes
- All prototype HTMLs are in `prototypes/`.
- Use the shell tab URL (`/web/shell/index.html?tab=routing`) for day-to-day workflow.

## 12. Troubleshooting
- If save is unavailable, verify server is started from project root.
- If visuals don’t match current patch, run regenerate command again.
- If routes look wrong, inspect `projects/studio-sidecar/outputs/debug/route-debug.json`.
- Run `python3 tools/project_health.py projects/<project-name>` to validate a project without changing it.
- Run `python3 tools/check_project.py` for the fixture-based development checks; these checks do not write into `projects/`.
