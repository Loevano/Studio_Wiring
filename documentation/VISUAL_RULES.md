# Visual Routing Rules

Last updated: 2026-03-01

This file is the canonical rule set for SVG wire rendering. The generator reads:

- the selected project's device configuration (devices + ports)
- the selected device configuration's patch configuration (connections)
- `json/routing_rules.json` (global label/routing policy)

## 1. Device/Port Rendering

- Device name is centered in the header.
- IN ports render on the left, OUT ports on the right.
- A physical bidirectional (`io`) socket renders once, on the side facing its connected peer.
- Port order follows model order; grouped ports use signal-flow order (Mic, Line, then others).
- In layer views: hide ports not relevant to the active layer family.
- In overview (`all-connections.svg`): show only wired ports.
- In the audio overview (`all-audio.svg`): combine AUDIO and DIGI routes while preserving their individual connection colors and legend entries.
- Unwired ports are light gray.

## 2. Wire Direction & Color

- Wires route source -> destination based on matrix connection rows.
- Unidirectional links show arrow at destination end.
- Bidirectional links (USB/Ethernet/HDMI/etc.) show arrows on both ends.
- Matching reciprocal rows for a single-link transport (such as optical MADI) collapse into one bidirectional visual cable; source JSON rows are not changed.
- Color comes from resolved connection family/type.
- Crossings use a narrow background under-stroke so the upper wire remains distinct.
- Device and group-frame clearance are hard constraints; ordinary routes may cross
  another wire but may not hug or pass through a device/group boundary.
- A route may leave its source group and enter its destination group, but it
  must pass above or below any unrelated functional group instead of using an
  apparent gap between that group's device blocks.
- Compact in-band paths and consistent bend grammar take priority over soft
  parallel-wire clearance, so one unrelated cable cannot send a bundle member
  around the outside of the diagram.
- Repeated links between the same device pair use one shared route grammar and
  ordered neighbouring rails. When a descending turn spans the next source
  row, the upper cable turns later so the pair cannot cross. Stereo-to-mono collectors and collapsed
  multichannel trunks are the intentional exceptions.
- When a tall intermediate block would force a whole bundle into outer detours,
  move the block within its signal-stage column before adding route exceptions.
- In the overview, power wires are painted after signal wires and therefore form the top wire layer.

## 3. Label Placement

- Source-side label: above wire (from `routing_rules.labels.source_side`).
- Destination-side label: below wire (from `routing_rules.labels.destination_side`).
- Labels are placed on straight lead segments, not on 90-degree corners.
- Label collision avoidance moves labels to nearest legal offset lane.

## 4. Routing Geometry

- Reserve straight lead near both ports so labels stay readable.
- Bidirectional links are oriented left-to-right for drawing, independent of stored source/destination order.
- Forward fan-outs preserve FIFO turn order when enabled.
- Right-to-left OUT -> IN links wrap below by default.
- Long HDMI/video links bias early vertical turn (if enabled).
- HDMI stays inside the endpoint band when the compact route clears device boxes; wire crossings use the normal under-stroke instead of forcing an outer detour.
- In Computer/Data, the direct TV display stage shares the dock column and is ordered first so the Mac HDMI rows align; dock peripherals remain one column farther right.
- Digital Audio follows Audient → RME → SSL/Clarity stage order, preventing the ADAT feed from becoming a same-column outer wrap.
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
