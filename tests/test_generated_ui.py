from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "routing_matrix.html"


class GeneratedConfigurationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = MATRIX.read_text(encoding="utf-8")

    def test_configuration_dialog_structure_is_unique_and_accessible(self) -> None:
        for element_id in (
            "configDialog",
            "configDialogTitle",
            "configDialogDescription",
            "configDialogInput",
            "configDialogTarget",
            "configDialogOverwriteCheck",
            "configDialogError",
        ):
            self.assertEqual(
                self.html.count(f'id="{element_id}"'),
                1,
                f"expected exactly one #{element_id}",
            )
        self.assertIn('aria-labelledby="configDialogTitle"', self.html)
        self.assertIn('aria-describedby="configDialogDescription"', self.html)
        self.assertIn('role="alert" aria-live="polite"', self.html)

    def test_configuration_workflows_do_not_use_browser_prompts(self) -> None:
        self.assertNotIn("window.prompt(", self.html)
        for function_name in (
            "createNewProject",
            "createNewDeviceConfig",
            "createNewPatchConfig",
            "saveCurrentModelAs",
            "saveCurrentPatchAs",
        ):
            self.assertIn(f"function {function_name}(", self.html)
        self.assertIn('postJsonApi("/api/create-project"', self.html)
        self.assertIn('postJsonApi("/api/save-transaction"', self.html)

    def test_dialog_has_focus_escape_and_overwrite_guards(self) -> None:
        self.assertIn("function configDialogFocusableElements()", self.html)
        self.assertIn('configDialog.addEventListener("cancel"', self.html)
        self.assertIn('configDialog.addEventListener("keydown"', self.html)
        self.assertIn("configDialogReturnFocus", self.html)
        self.assertIn("Confirm that the existing file may be replaced.", self.html)

    def test_inline_javascript_parses_when_node_is_available(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", self.html, flags=re.DOTALL)
        self.assertTrue(scripts, "no inline script found")
        with tempfile.TemporaryDirectory(prefix="generated-ui-js-") as temp_dir:
            script_path = Path(temp_dir) / "routing-matrix-inline.js"
            script_path.write_text("\n".join(scripts), encoding="utf-8")
            completed = subprocess.run(
                [node, "--check", str(script_path)],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_device_and_port_edits_commit_at_navigation_boundaries(self) -> None:
        self.assertIn("function commitPendingDeviceEditorEdits(", self.html)
        self.assertIn('querySelectorAll("[data-port-name]")', self.html)
        self.assertIn('commitPendingDeviceEditorEdits("Saved before leaving Devices & Ports")', self.html)
        self.assertIn('commitPendingDeviceEditorEdits("Saved before selecting another device")', self.html)
        self.assertIn('commitPendingDeviceEditorEdits("Saved before switching port view")', self.html)
        self.assertIn('window.addEventListener("blur"', self.html)
        self.assertIn('document.addEventListener("visibilitychange"', self.html)

    def test_device_exit_flushes_through_normal_autosave_pipeline(self) -> None:
        self.assertIn("function flushPendingDeviceEditorAutoSave(", self.html)
        self.assertIn("function flushAutoSaveForShell(", self.html)
        self.assertIn("if (!autoSaveEnabled || !saveApiEnabled || !pendingModelEditSave) return", self.html)
        self.assertIn('saveJsonToDisk(reason, true, false)', self.html)
        self.assertIn('flushPendingDeviceEditorAutoSave("devices-and-ports-tab-exit")', self.html)
        self.assertIn('flushAutoSaveForShell("application-window-blur")', self.html)
        self.assertIn('flushAutoSaveForShell("application-hidden")', self.html)
        self.assertIn('data.type === "studio-shell-autosave-flush"', self.html)
        self.assertIn('type: "studio-shell-autosave-flushed"', self.html)

    def test_save_device_button_uses_the_same_validated_commit_path(self) -> None:
        match = re.search(
            r"if\s*\(saveDeviceMetaBtn\)\s*saveDeviceMetaBtn\.onclick\s*=\s*\(\)\s*=>\s*\{(?P<body>.*?)\n\s*\};",
            self.html,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        self.assertIn("commitPendingDeviceEditorEdits", match.group("body"))
        self.assertIn("flushPendingDeviceEditorAutoSave", match.group("body"))

    def test_power_is_a_first_class_family(self) -> None:
        self.assertIn('const FAMILY_ORDER = ["AUDIO", "COMP", "DIGI", "NETWORK", "POWER"]', self.html)
        self.assertIn('{ id: "POWER_IN", label: "AC Power In"', self.html)
        self.assertIn('{ id: "POWER_OUT", label: "AC Power Out"', self.html)

        for relative_path in (
            "defaults/default_template/studio_model_template_empty.json",
            "projects/_template/device-configurations/studio-model.json",
        ):
            model = json.loads((ROOT / relative_path).read_text(encoding="utf-8"))
            self.assertEqual(model["families"]["POWER"]["prefix"], "POWER", relative_path)
            self.assertEqual(model["families"]["POWER"]["layer"], "Power", relative_path)

    def test_visibility_panel_has_independent_targets_and_group_modifiers(self) -> None:
        for key in (
            "wiring_matrix",
            "routing_matrix",
            "connection_overview",
            "visuals",
        ):
            self.assertIn(f'key: "{key}"', self.html)
        self.assertIn('data-visibility-target="${esc(target.key)}"', self.html)
        self.assertIn("function deviceVisibleForTarget(", self.html)
        self.assertIn("function setDeviceVisibilityForTarget(", self.html)
        self.assertIn("function updateVisibilitySelection(", self.html)
        self.assertIn("event.metaKey || event.ctrlKey", self.html)
        self.assertIn("altKey: Boolean(event.altKey)", self.html)
        self.assertIn('visibleDeviceNameSet("connection_overview")', self.html)
        self.assertIn('deviceVisibleForTarget(device, "wiring_matrix")', self.html)

    def test_svg_exports_use_current_files_and_offer_individual_downloads(self) -> None:
        self.assertIn('id="downloadSvgsBtn"', self.html)
        self.assertIn("Download SVG Folder (.zip)", self.html)
        self.assertEqual(
            len(re.findall(r'<button[^>]+data-preview-download="[^"]+"', self.html)),
            7,
        )
        self.assertNotIn('window.showDirectoryPicker({', self.html)
        self.assertIn('window.location.assign(`/api/svg-archive?${query.toString()}`)', self.html)
        self.assertIn('saveJsonToDisk("save-svg-folder", true, true)', self.html)
        self.assertIn("function datedSvgFilename(", self.html)
        self.assertIn("a.download = datedSvgFilename(filename)", self.html)
        self.assertIn("document.body.appendChild(a)", self.html)
        self.assertIn('appendCacheBuster(base, "_open", Date.now())', self.html)


if __name__ == "__main__":
    unittest.main()
