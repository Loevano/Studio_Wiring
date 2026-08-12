# Architecture consolidation

The shell now routes its **Routing Matrix** tab directly to the canonical compact matrix application. The active path is therefore:

`web/shell/index.html` → `prototypes/routing_matrix_prototype_compact.html`

This removes the former shell → generated app → prototype iframe chain. The canonical matrix owns routing interaction, keyboard behavior, rendering, patch history, dirty state, and transactional patch saves. It loads the project catalog itself and exposes project, device-config, and patch-config selectors.

The other shell tabs still route to `routing_matrix.html` because that generated application continues to own the device editor, connection overview, visibility settings, and visual previews.

## Remaining boundary

`routing_matrix.html` still contains the old matrix renderer and prototype-host code for backward compatibility when opened directly. It is no longer on the shell's routing path. A later extraction should remove those dormant branches from `build_routing_matrix_html`, then move the canonical matrix's inline CSS and JavaScript into `web/routing/` modules. That extraction should preserve the architecture contract tests and the canonical matrix behavior tests.
