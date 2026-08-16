from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "web/manifests/tabs.json"
SHELL_HTML_PATH = ROOT / "web/shell/index.html"
SHELL_JS_PATH = ROOT / "web/shell/shell.js"
SHELL_CSS_PATH = ROOT / "web/shared/styles/base.css"
GENERATED_APP_PATH = ROOT / "routing_matrix.html"
CANONICAL_MATRIX_PATH = ROOT / "prototypes/routing_matrix_prototype_compact.html"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function_body(source: str, function_name: str) -> str:
    marker = f"function {function_name}("
    start = source.find(marker)
    if start < 0:
        raise AssertionError(f"Missing JavaScript function {function_name}")
    brace = source.find("{", start)
    if brace < 0:
        raise AssertionError(f"Missing function body for {function_name}")
    depth = 0
    quote = ""
    escaped = False
    for index in range(brace, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {'"', "'", "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[brace + 1:index]
    raise AssertionError(f"Unterminated function body for {function_name}")


class ShellTabManifestContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(_read(MANIFEST_PATH))
        cls.tabs = cls.manifest["tabs"]
        cls.by_key = {str(tab["key"]): tab for tab in cls.tabs}

    def test_routing_is_the_only_canonical_matrix_host(self) -> None:
        self.assertEqual(self.manifest["default_tab"], "routing")
        self.assertEqual(
            set(self.by_key),
            {
                "routing",
                "audio-routing",
                "connection-overview",
                "devices",
                "rack-editor",
                "visibility",
                "visuals",
            },
        )
        canonical_hosts = [
            tab["key"]
            for tab in self.tabs
            if "routing_matrix_prototype_compact.html" in str(tab["src"])
        ]
        self.assertEqual(canonical_hosts, ["routing"])
        routing_url = urlsplit(str(self.by_key["routing"]["src"]))
        self.assertTrue(routing_url.path.endswith("/prototypes/routing_matrix_prototype_compact.html"))

        tab_keys = [str(tab["key"]) for tab in self.tabs]
        self.assertEqual(tab_keys.index("audio-routing"), tab_keys.index("routing") + 1)
        audio_routing_url = urlsplit(str(self.by_key["audio-routing"]["src"]))
        self.assertTrue(audio_routing_url.path.endswith("/routing-matrix/index.html"))

    def test_each_sibling_tab_selects_one_unique_embedded_panel(self) -> None:
        sibling_keys = [key for key in self.by_key if key not in {"routing", "audio-routing"}]
        selected_panels: list[str] = []
        for key in sibling_keys:
            source = urlsplit(str(self.by_key[key]["src"]))
            query = parse_qs(source.query, keep_blank_values=True)
            self.assertTrue(source.path.endswith("/routing_matrix.html"), key)
            self.assertEqual(query.get("embedded"), ["1"], key)
            self.assertEqual(query.get("tab"), [key], key)
            self.assertNotIn("matrix_subtab", query, key)
            selected_panels.append(query["tab"][0])
        self.assertEqual(len(selected_panels), len(set(selected_panels)))


class ShellFrameRoutingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.shell_html = _read(SHELL_HTML_PATH)
        cls.shell_js = _read(SHELL_JS_PATH)

    def test_shell_has_one_frame_and_embedded_apps_have_none(self) -> None:
        self.assertEqual(len(re.findall(r"<iframe\b", self.shell_html, re.IGNORECASE)), 1)
        self.assertNotRegex(_read(GENERATED_APP_PATH), re.compile(r"<iframe\b", re.IGNORECASE))
        self.assertNotRegex(_read(CANONICAL_MATRIX_PATH), re.compile(r"<iframe\b", re.IGNORECASE))

    def test_initial_url_and_frame_reuse_derive_the_same_selection(self) -> None:
        load_tab = _function_body(self.shell_js, "loadTab")
        post_selection = _function_body(self.shell_js, "postTabSelectionToFrame")
        self.assertIn("const selection = parseEmbeddedTabSelection(src);", load_tab)
        self.assertIn('srcUrl.searchParams.set("_shell_ui", EMBEDDED_APP_CACHE_VERSION)', load_tab)
        self.assertIn("postTabSelectionToFrame(selection.tab, selection.matrixSubTab)", load_tab)
        self.assertIn("tabFrame.src = src;", load_tab)
        self.assertIn('type: "studio-shell-main-tab-set"', post_selection)
        self.assertIn("tab: normalizedTab", post_selection)
        self.assertIn("matrix_subtab:", post_selection)

    def test_shell_owns_one_non_scrolling_viewport_for_the_frame(self) -> None:
        """The shell chrome must not create a second page scroll area around its iframe."""
        css = _read(SHELL_CSS_PATH)
        page_rule = re.search(r"\.shell-page\s*\{(?P<body>.*?)\}", css, re.DOTALL)
        frame_wrap_rule = re.search(r"\.shell-frame-wrap\s*\{(?P<body>.*?)\}", css, re.DOTALL)
        frame_rule = re.search(r"\.shell-frame\s*\{(?P<body>.*?)\}", css, re.DOTALL)
        document_rule = re.search(r"html\s*,\s*body\s*\{(?P<body>.*?)\}", css, re.DOTALL)
        self.assertIsNotNone(document_rule)
        self.assertIsNotNone(page_rule)
        self.assertIsNotNone(frame_wrap_rule)
        self.assertIsNotNone(frame_rule)

        document_css = document_rule.group("body")
        page_css = page_rule.group("body")
        frame_wrap_css = frame_wrap_rule.group("body")
        frame_css = frame_rule.group("body")
        self.assertRegex(document_css, r"\bheight\s*:\s*100%")
        self.assertRegex(css, r"body\s*\{[^}]*\boverflow\s*:\s*hidden\b", re.DOTALL)
        self.assertRegex(page_css, r"\bheight\s*:\s*100(?:d)?(?:vh|%)\b")
        self.assertRegex(page_css, r"grid-template-rows\s*:\s*(?:auto|max-content)\s+(?:auto|max-content)\s+minmax\(\s*0\s*,\s*1fr\s*\)")
        self.assertRegex(page_css, r"\boverflow\s*:\s*hidden\b")
        self.assertRegex(frame_wrap_css, r"\bmin-width\s*:\s*0\b")
        self.assertRegex(frame_wrap_css, r"\bmin-height\s*:\s*0\b")
        self.assertRegex(frame_wrap_css, r"\boverflow\s*:\s*hidden\b")
        self.assertNotRegex(frame_wrap_css, r"\b(?:min-)?height\s*:\s*calc\(")
        self.assertNotRegex(frame_wrap_css, r"\bmin-height\s*:\s*[1-9]\d*(?:px|rem|vh)\b")
        self.assertRegex(frame_css, r"\bdisplay\s*:\s*block\b")
        self.assertRegex(frame_css, r"\bwidth\s*:\s*100%")
        self.assertRegex(frame_css, r"\bheight\s*:\s*100%")

    def test_autosave_is_a_persistent_shell_owned_preference(self) -> None:
        self.assertIn('const AUTO_SAVE_STORAGE_KEY = "studioWiringAutoSaveToDiskV1"', self.shell_js)
        self.assertIn("function loadAutoSavePreference()", self.shell_js)
        self.assertIn("function persistAutoSavePreference(enabled)", self.shell_js)
        self.assertIn("applyAutoSaveButtonState(loadAutoSavePreference())", self.shell_js)
        button_state = _function_body(self.shell_js, "applyAutoSaveButtonState")
        self.assertIn("persistAutoSavePreference(autoSaveEnabled)", button_state)
        self.assertIn('setAttribute("aria-pressed"', button_state)
        frame_load = re.search(
            r'tabFrame\.addEventListener\("load",\s*\(\)\s*=>\s*\{(?P<body>.*?)\n\s*\}\);',
            self.shell_js,
            re.DOTALL,
        )
        self.assertIsNotNone(frame_load)
        self.assertIn("postAutoSaveToFrame(autoSaveEnabled)", frame_load.group("body"))

    def test_shell_flushes_autosave_before_switching_tabs(self) -> None:
        request_flush = _function_body(self.shell_js, "requestAutoSaveFlushFromFrame")
        self.assertIn('type: "studio-shell-autosave-flush"', request_flush)
        self.assertIn("request_id: requestId", request_flush)
        load_tab = _function_body(self.shell_js, "loadTab")
        flush_call = load_tab.index("await requestAutoSaveFlushFromFrame(")
        activate = load_tab.index("activeKey = tab.key")
        self.assertLess(flush_call, activate)
        self.assertIn("if (!flushed)", load_tab)
        self.assertIn("tab switch was cancelled", load_tab)
        self.assertIn('data.type === "studio-shell-autosave-flushed"', self.shell_js)


class GeneratedEmbeddedAppContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = _read(GENERATED_APP_PATH)

    def test_embedded_chrome_and_connection_overview_controls_are_hidden(self) -> None:
        self.assertRegex(
            self.html,
            re.compile(
                r"html\.embedded-mode\s+body\s+\.app-title-panel\s*,\s*"
                r"html\.embedded-mode\s+body\s+\.tab-bar\s*\{[^}]*display:\s*none\s*!important",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            self.html,
            re.compile(
                r"html\.embedded-mode\s+body\.connection-overview-mode\s+#panelMatrix\s*>\s*:not\(#connectionList\)"
                r"\s*\{[^}]*display:\s*none\s*!important",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            self.html,
            re.compile(
                r"html\.embedded-mode\s+body\.connection-overview-mode\s+#connectionList\s*\{"
                r"[^}]*display:\s*block\s*!important",
                re.DOTALL,
            ),
        )

    def test_external_tabs_map_to_exactly_one_intended_panel(self) -> None:
        show_main_tab = _function_body(self.html, "showMainTab")
        expected = {
            "matrix": ("showMatrix", "panelMatrix"),
            "devices": ("showDevices", "panelDevices"),
            "rack-editor": ("showRack", "panelRack"),
            "visibility": ("showVisibility", "panelVisibility"),
            "visuals": ("showVisuals", "panelVisuals"),
        }
        for tab, (flag, panel) in expected.items():
            if tab == "rack-editor":
                self.assertIn('target === "rack" || target === "rack-editor"', show_main_tab)
            else:
                self.assertIn(f'target === "{tab}"', show_main_tab)
            self.assertIn(f'{panel}.classList.toggle("hidden", !{flag})', show_main_tab)

        external = _function_body(self.html, "applyExternalMainTab")
        self.assertIn('target === "connection-overview"', external)
        connection_branch = external.split('target === "connection-overview"', 1)[1].split("return;", 1)[0]
        self.assertIn('showMainTab("matrix")', connection_branch)
        self.assertIn('showMatrixSubTab("connections")', connection_branch)
        self.assertIn('classList.add("connection-overview-mode")', connection_branch)
        self.assertIn('["matrix", "devices", "visibility", "visuals"].includes(target)', external)
        self.assertIn('showMainTab("rack-editor")', external)

    def test_initial_query_and_reuse_message_call_the_same_router(self) -> None:
        self.assertIn(
            'applyExternalMainTab(initialUrlParameters.get("tab") || "matrix", '
            'initialUrlParameters.get("matrix_subtab") || "")',
            self.html,
        )
        message_match = re.search(
            r'if\s*\(data\.type\s*===\s*"studio-shell-main-tab-set"\)\s*\{(?P<body>.*?)\n\s*\}',
            self.html,
            re.DOTALL,
        )
        self.assertIsNotNone(message_match)
        self.assertIn("applyExternalMainTab(data.tab, data.matrix_subtab)", message_match.group("body"))

    def test_initial_matrix_subtab_cannot_override_external_panel(self) -> None:
        initial = self.html.rfind("const initialUrlParameters")
        self.assertGreaterEqual(initial, 0)
        tail = self.html[initial:]
        subtab = tail.find("showMatrixSubTab(selectedMatrixSubTab);")
        external = tail.find("applyExternalMainTab(initialUrlParameters.get")
        self.assertGreaterEqual(subtab, 0)
        self.assertGreater(external, subtab)
        self.assertNotIn("showMatrixSubTab(selectedMatrixSubTab);", tail[external:])

    def test_project_selection_is_shared_across_iframe_apps(self) -> None:
        canonical = _read(CANONICAL_MATRIX_PATH)
        generated = self.html
        storage_key = 'studioWiringProjectSelectionV1'
        for source in (canonical, generated):
            self.assertIn(storage_key, source)
            self.assertIn("function loadProjectSelectionPreference()", source)
            self.assertIn("function persistProjectSelectionPreference(", source)
            self.assertIn("window.localStorage.setItem(PROJECT_SELECTION_STORAGE_KEY", source)

        load_catalog = _function_body(canonical, "loadProjectCatalog")
        self.assertIn("loadProjectSelectionPreference()", load_catalog)
        self.assertLess(
            load_catalog.index("projectByKey(preferredProjectKey)"),
            load_catalog.index("projectByKey(payload?.active_project_key)"),
            "stored user selection must win over the server startup project",
        )

        generated_update = _function_body(generated, "updateProjectSelectorsFromConfig")
        self.assertIn("storedSelection.project_key", generated_update)
        self.assertIn("persistProjectSelectionPreference(", generated_update)
        detect_save_api = _function_body(generated, "detectSaveApi")
        self.assertLess(
            detect_save_api.index("updateProjectSelectorsFromConfig(payload)"),
            detect_save_api.index('loadModelAndConnectionsFromSelection("saved-selection-restore")'),
            "the restored dropdown selection must load its matching model and patch",
        )


class JavaScriptSyntaxContractTests(unittest.TestCase):
    def test_shell_and_generated_inline_javascript_parse_with_node(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not installed")
        generated = _read(GENERATED_APP_PATH)
        scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", generated, re.DOTALL | re.IGNORECASE)
        executable = [script for script in scripts if script.strip()]
        self.assertTrue(executable, "Generated app has no inline JavaScript")
        with tempfile.TemporaryDirectory() as temp_dir:
            inline_path = Path(temp_dir) / "routing_matrix_inline.js"
            inline_path.write_text("\n".join(executable), encoding="utf-8")
            for path in (SHELL_JS_PATH, inline_path):
                result = subprocess.run(
                    [node, "--check", str(path)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
