from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "web" / "manifests" / "tabs.json"
HTML = ROOT / "web" / "visibility" / "index.html"
SCRIPT = ROOT / "web" / "visibility" / "app.js"


class VisibilityUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = HTML.read_text(encoding="utf-8")
        cls.script = SCRIPT.read_text(encoding="utf-8")

    def test_shell_uses_the_dedicated_visibility_app(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        visibility = next(tab for tab in manifest["tabs"] if tab["key"] == "visibility")
        self.assertEqual(visibility["src"], "../visibility/index.html")
        self.assertNotIn("routing_matrix.html", visibility["src"])

    def test_visibility_app_exposes_all_four_targets(self) -> None:
        for key, label in (
            ("wiring_matrix", "Show in Wiring Matrix"),
            ("routing_matrix", "Show in Routing Matrix"),
            ("connection_overview", "Show in Connection Overview"),
            ("visuals", "Show in Visuals"),
        ):
            self.assertIn(f'key: "{key}"', self.script)
            self.assertIn(f'label: "{label}"', self.script)

    def test_group_selection_and_option_click_are_native_to_current_app(self) -> None:
        for token in (
            "modifiers.altKey",
            "modifiers.shiftKey",
            "modifiers.metaKey",
            "modifiers.ctrlKey",
            "event.altKey ? selectionNames()",
            "keepSelectedGroup && state.selected.has(name)",
        ):
            self.assertIn(token, self.script)
        self.assertIn("⌥-click applies a checkbox to all devices", self.html)

    def test_visibility_app_saves_the_active_model_and_supports_shell_autosave(self) -> None:
        self.assertIn('const SAVE_MODEL_API = "/api/save-model"', self.script)
        self.assertIn('fetch("/api/set-targets"', self.script)
        self.assertIn("loadSharedSelection()", self.script)
        self.assertIn('headers["X-If-Unmodified-Model-Hash"]', self.script)
        self.assertIn('data.type === "studio-shell-autosave-flush"', self.script)
        self.assertIn('type: "studio-shell-autosave-flushed"', self.script)


if __name__ == "__main__":
    unittest.main()
