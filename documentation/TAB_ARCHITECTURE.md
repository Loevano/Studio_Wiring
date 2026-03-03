# Tab Architecture (Manifest-Routed Shell)

Last updated: 2026-03-02

## Goal

Use one stable shell and route each tab directly via `web/manifests/tabs.json`.

## Entry Points

- Shell: `web/shell/index.html`
- Manifest: `web/manifests/tabs.json`
- Main app: `routing_matrix.html`

## Tab Routing

Each manifest tab points directly to `routing_matrix.html` with tab-specific query params, for example:

- `../../routing_matrix.html?embedded=1&tab=matrix&matrix_subtab=prototype`
- `../../routing_matrix.html?embedded=1&tab=devices`
- `../../routing_matrix.html?embedded=1&tab=visibility`
- `../../routing_matrix.html?embedded=1&tab=visuals`

## How Version Swaps Work

1. Update the tab `src` in `web/manifests/tabs.json`.
2. Reload shell page. Other tabs are untouched.
3. Optional: point one tab to a new standalone page later without changing shell code.

## Embedded Mode

Shell tabs open `routing_matrix.html` with `embedded=1` and the selected `tab` value.

`embedded=1` hides top-level header/tab buttons so each tab host can focus on one panel.

This keeps behavior stable and avoids an extra iframe hop during tab switches.
