from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATED_APP = ROOT / "routing_matrix.html"


class ShellPrepaintContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = GENERATED_APP.read_text(encoding="utf-8")

    def test_embedded_mode_is_applied_in_head_before_css_and_body(self) -> None:
        head_end = self.html.index("</head>")
        head = self.html[:head_end]
        bootstrap = head.index("function applyInitialShellRoute")
        embedded_class = head.index('classList.add("embedded-mode")', bootstrap)
        stylesheet = head.index("<style>")

        self.assertLess(bootstrap, embedded_class)
        self.assertLess(embedded_class, stylesheet)
        self.assertNotIn("<body", head.lower())

    def test_no_generated_panel_is_visible_before_initial_routing(self) -> None:
        for panel_id in (
            "panelMatrix",
            "panelDevices",
            "panelRack",
            "panelVisibility",
            "panelVisuals",
        ):
            self.assertRegex(
                self.html,
                rf'<div\s+id="{panel_id}"\s+class="[^"]*\bhidden\b[^"]*"',
                f"{panel_id} can flash before applyExternalMainTab runs",
            )

    def test_connection_overview_prepaint_css_exposes_only_connection_list(self) -> None:
        self.assertRegex(
            self.html,
            re.compile(
                r"html\.embedded-mode\s+body\.connection-overview-mode\s+"
                r"#panelMatrix\s*>\s*:not\(#connectionList\)\s*\{"
                r"[^}]*display:\s*none\s*!important",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            self.html,
            re.compile(
                r"html\.embedded-mode\s+body\.connection-overview-mode\s+"
                r"#connectionList\s*\{[^}]*display:\s*block\s*!important",
                re.DOTALL,
            ),
        )


if __name__ == "__main__":
    unittest.main()
