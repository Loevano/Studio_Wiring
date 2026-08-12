# Project Structure

## Goal
Separate stable studio definition from patch snapshots.

- Studio Layout = device/port model (infrequent changes)
- Patch Layout = cable/connection set (frequent changes, multiple variants per studio)

## Recommended Layout

```text
projects/
  <project-name>/
    project.json
    device-configurations/
      studio-model.json
      studio-model.template-empty.json
    patch-configurations/
      studio-model/
        patch-default.json
        patch-tracking.json
      alt-device-config/
        patch-mixdown.json
    outputs/
      html/
        routing_matrix.html
        studio_wiring_point_to_point.html
      svgs/
        audio-analog.svg
        digital-audio.svg
        computer-data.svg
        network.svg
        all-connections.svg
      debug/
        route-debug.json
```

Global rules file:

```text
json/
  routing_rules.json
```

Frontend shell/tab layout:

```text
web/
  shell/
    index.html
    shell.js
  manifests/
    tabs.json
  shared/styles/
    tokens.css
    base.css
routing_matrix.html
```

## Save Semantics
- Save device edits to `device-configurations/studio-model.json`.
- Save patch edits to one file in `patch-configurations/<device-config-stem>/`.
- Keep multiple patch files per studio.
- Generated outputs go to `outputs/html` and `outputs/svgs`.

## Launch Example

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

## Naming Suggestions
- Project folder: kebab-case (`studio-sidecar`)
- Patch files: `patch-<purpose>.json` inside `patch-configurations/<device-config-stem>/`
- Backups: `patch-<purpose>.<YYYYMMDD-HHMM>.json`
