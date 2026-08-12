# Visual Routing Rules

Last updated: 2026-03-01

This file is the canonical rule set for SVG wire rendering. The generator reads:

- the selected project's device configuration (devices + ports)
- the selected device configuration's patch configuration (connections)
- `json/routing_rules.json` (global label/routing policy)

## 1. Device/Port Rendering

- Device name is centered in the header.
- IN ports render on the left, OUT ports on the right.
- Port order follows model order; grouped ports use signal-flow order (Mic, Line, then others).
- In layer views: hide ports not relevant to the active layer family.
- In overview (`all-connections.svg`): show only wired ports.
- Unwired ports are light gray.

## 2. Wire Direction & Color

- Wires route source -> destination based on matrix connection rows.
- Unidirectional links show arrow at destination end.
- Bidirectional links (USB/Ethernet/HDMI/etc.) show arrows on both ends.
- Color comes from resolved connection family/type.

## 3. Label Placement

- Source-side label: above wire (from `routing_rules.labels.source_side`).
- Destination-side label: below wire (from `routing_rules.labels.destination_side`).
- Labels are placed on straight lead segments, not on 90-degree corners.
- Label collision avoidance moves labels to nearest legal offset lane.

## 4. Routing Geometry

- Reserve straight lead near both ports so labels stay readable.
- Forward fan-outs preserve FIFO turn order when enabled.
- Right-to-left OUT -> IN links wrap below by default.
- Long HDMI/video links bias early vertical turn (if enabled).
- Route scoring prefers:
  - fewer crossings through device boxes,
  - fewer overlaps with different-family wires,
  - less out-of-band excursion,
  - fewer bends,
  - shorter total path length.

## 5. Matrix Integration

- Matrix and visuals share the same source JSON files.
- Server regenerate (`/api/regenerate`) passes model + connections + routing rules to generator.
- Regenerate updates:
  - `projects/studio-sidecar/outputs/html/studio_wiring_point_to_point.html`
  - `projects/studio-sidecar/outputs/svgs/*.svg`
  - `projects/studio-sidecar/outputs/debug/route-debug.json`

## 6. Wire Debug Artifact

- The selected project's `outputs/debug/route-debug.json` includes per cable:
  - source/destination endpoints + columns,
  - chosen route points/path,
  - route mode (forward/backward/backward_wrap_below),
  - route slot metadata,
  - score breakdown.
- Use cable ID in this file to trace weird lines quickly.
