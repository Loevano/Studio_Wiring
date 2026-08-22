# Tab Architecture (Manifest-Routed Shell)

Last updated: 2026-08-12

## Goal

Use one stable shell and route each tab directly via `web/manifests/tabs.json`.

## Entry Points

- Shell: `web/shell/index.html`
- Manifest: `web/manifests/tabs.json`
- Canonical routing app: `prototypes/routing_matrix_prototype_compact.html`
- Logical routing app: `web/routing-matrix/index.html`
- Editor/visual app: `routing_matrix.html`

## Tab Routing

The Wiring Matrix tab points directly to the canonical physical matrix. Routing Matrix follows it and points to its own logical-routing page. The remaining tabs point to the generated editor/visual app:

- `../../prototypes/routing_matrix_prototype_compact.html?use_save_api=1`
- `../routing-matrix/index.html`
- `../../routing_matrix.html?embedded=1&tab=connection-overview`
- `../../routing_matrix.html?embedded=1&tab=devices`
- `../../routing_matrix.html?embedded=1&tab=visibility`
- `../../routing_matrix.html?embedded=1&tab=visuals`

The shell creates one iframe per distinct tab app and keeps loaded frames mounted while they are inactive. Returning to Wiring Matrix therefore restores the same live editor state, including unsaved edits, undo history, filters, collapsed folders, and scroll position. Tabs backed by `routing_matrix.html` share one cached frame and continue to switch panels through shell messages. The generated app is not an intermediary for either matrix tab.

## How Version Swaps Work

1. Update the tab `src` in `web/manifests/tabs.json`.
2. Reload shell page. Other tabs are untouched.
3. Optional: point one tab to a new standalone page later without changing shell code.

## App Boundaries

The canonical Wiring Matrix owns physical-patch editing, accessibility, history, dirty state, and transactional patch saves. It loads the project catalog and selected device/patch files directly.

Routing Matrix owns logical audio-route editing, accessibility, history, dirty state, and route saves. Its data is a separate routing configuration and does not represent or modify physical cables.

The other tabs open `routing_matrix.html` with `embedded=1`; this hides its top-level navigation so each tab host displays only the selected panel.

See `ARCHITECTURE_CONSOLIDATION.md` for the remaining compatibility boundary.
