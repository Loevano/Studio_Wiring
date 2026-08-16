"""Static regression contracts for the canonical routing-matrix prototype.

These tests intentionally inspect the shipped, dependency-free HTML artifact. They
guard integration seams which are easy to regress while the prototype remains a
single file: transactional saves, patch history, accessible matrix controls, and
parent-frame dirty-state reporting.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = ROOT / "prototypes" / "routing_matrix_prototype_compact.html"


def extract_inline_script(html: str) -> str:
    matches = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.DOTALL | re.IGNORECASE)
    if not matches:
        raise AssertionError("prototype has no inline script")
    return "\n".join(matches)


def extract_function(source: str, name: str) -> str:
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", source)
    if not match:
        raise AssertionError(f"function {name} was not found")
    opening = match.end() - 1
    depth = 0
    quote = ""
    escaped = False
    in_line_comment = False
    in_block_comment = False
    index = opening
    while index < len(source):
        char = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if in_line_comment:
            if char == "\n":
                in_line_comment = False
        elif in_block_comment:
            if char == "*" and following == "/":
                in_block_comment = False
                index += 1
        elif quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char == "/" and following == "/":
            in_line_comment = True
            index += 1
        elif char == "/" and following == "*":
            in_block_comment = True
            index += 1
        elif char in {'"', "'", "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[match.start() : index + 1]
        index += 1
    raise AssertionError(f"function {name} has no closing brace")


class _MarkupInventory(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.live_regions: list[dict[str, str | None]] = []

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(str(values["id"]))
        if values.get("aria-live"):
            self.live_regions.append(values)


class PrototypeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = PROTOTYPE.read_text(encoding="utf-8")
        cls.script = extract_inline_script(cls.html)

    def test_inline_javascript_syntax_with_node(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable; inline JavaScript syntax check skipped")
        completed = subprocess.run(
            [node, "--check", "-"],
            input=self.script,
            text=True,
            capture_output=True,
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_any_power_connector_pair_is_compatible_without_misclassifying_adc_inputs(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable; power compatibility check skipped")
        function_names = (
            "normalizeFamily",
            "isPowerPort",
            "iconForPort",
            "isSpeakerToAnalogPair",
            "controlProtocolForPort",
            "supportsTransportCompatibilityForFamily",
            "sharedFamilies",
            "computeLinkCompatibility",
        )
        harness = "\n".join(
            [
                'const FAMILY_ALL = "ALL";',
                'const FAMILY_ORDER = ["AUDIO", "COMP", "DIGI", "NETWORK", "POWER", "CONTROL"];',
                *(extract_function(self.script, name) for name in function_names),
                "const source = { port: 'Outlet 1', families: ['POWER'], transport: 'SCHUKO' };",
                "const fixedLead = { port: 'AC In', families: ['AUDIO'], transport: 'CEE 7/7 FIXED' };",
                "const dcInput = { port: 'DC In', families: [], transport: '24V DC' };",
                "const adcInput = { port: 'ADC In 1', families: ['AUDIO'], transport: 'DB25' };",
                "console.log(JSON.stringify({",
                "  fixed: computeLinkCompatibility('POWER', source, fixedLead),",
                "  dc: computeLinkCompatibility('ALL', source, dcInput),",
                "  adcIsPower: isPowerPort(adcInput),",
                "  adcIcon: iconForPort(adcInput).label,",
                "}));",
            ]
        )
        completed = subprocess.run(
            [node, "-"],
            input=harness,
            text=True,
            capture_output=True,
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            '{"fixed":{"family":"POWER","reason":""},"dc":{"family":"POWER","reason":""},"adcIsPower":false,"adcIcon":"ANA"}',
            completed.stdout.strip(),
        )

    def test_matrix_uses_one_native_scroll_viewport_inside_a_fixed_app(self) -> None:
        """The app owns vertical space; only its matrix viewport owns matrix scrolling."""
        app_rule = re.search(r"\.app\s*\{(?P<body>.*?)\}", self.html, re.DOTALL)
        matrix_rule = re.search(r"\.matrix-wrap\s*\{(?P<body>.*?)\}", self.html, re.DOTALL)
        document_rule = re.search(r"html\s*,\s*body\s*\{(?P<body>.*?)\}", self.html, re.DOTALL)
        self.assertIsNotNone(app_rule)
        self.assertIsNotNone(matrix_rule)
        self.assertIsNotNone(document_rule)
        app_css = app_rule.group("body")
        matrix_css = matrix_rule.group("body")
        document_css = document_rule.group("body")

        self.assertRegex(document_css, r"\boverflow\s*:\s*hidden\b")
        self.assertRegex(app_css, r"\bheight\s*:\s*100(?:d)?(?:vh|%)\b")
        self.assertRegex(app_css, r"grid-template-rows\s*:\s*(?:auto|max-content)\s+minmax\(\s*0\s*,\s*1fr\s*\)")
        self.assertRegex(app_css, r"\bmin-height\s*:\s*0\b")
        self.assertRegex(app_css, r"\boverflow\s*:\s*hidden\b")
        self.assertRegex(matrix_css, r"\boverflow\s*:\s*auto\b")
        for declaration in (r"\bheight\s*:\s*auto\b", r"\bmin-height\s*:\s*0\b", r"\bmin-width\s*:\s*0\b", r"\bmax-width\s*:\s*100%"):
            self.assertRegex(matrix_css, declaration)
        self.assertNotRegex(matrix_css, r"\b(?:min-)?height\s*:\s*calc\(")
        self.assertNotIn("function bindMatrixTrackpadDiagonalScroll", self.script)
        self.assertNotIn("studio-prototype-scroll-handoff", self.script)

    def test_sticky_matrix_header_has_a_single_scroll_container_and_stable_layer_order(self) -> None:
        """Destination headers must stay above cells and the frozen source column."""
        matrix_rule = re.search(r"\.matrix-wrap\s*\{(?P<body>.*?)\}", self.html, re.DOTALL)
        sticky_top_rule = re.search(r"\.sticky-top\s*\{(?P<body>.*?)\}", self.html, re.DOTALL)
        sticky_left_rule = re.search(r"\.sticky-left\s*\{(?P<body>.*?)\}", self.html, re.DOTALL)
        body_sticky_rule = re.search(r"tbody\s+\.sticky-left\s*\{(?P<body>.*?)\}", self.html, re.DOTALL)
        head_sticky_rule = re.search(r"thead\s+\.sticky-left\s*\{(?P<body>.*?)\}", self.html, re.DOTALL)
        top_left_rule = re.search(r"\.top-left\s*\{(?P<body>.*?)\}", self.html, re.DOTALL)
        cell_rule = re.search(r"tbody\s+td\s*\{(?P<body>.*?)\}", self.html, re.DOTALL)
        self.assertTrue(all((matrix_rule, sticky_top_rule, sticky_left_rule, body_sticky_rule, head_sticky_rule, top_left_rule, cell_rule)))

        def z_index(rule: re.Match[str]) -> int:
            found = re.search(r"\bz-index\s*:\s*(\d+)", rule.group("body"))
            self.assertIsNotNone(found)
            return int(found.group(1))

        self.assertRegex(matrix_rule.group("body"), r"\boverflow\s*:\s*auto\b")
        self.assertRegex(sticky_top_rule.group("body"), r"\bposition\s*:\s*sticky\b")
        self.assertRegex(sticky_top_rule.group("body"), r"\btop\s*:\s*0\b")
        self.assertRegex(sticky_top_rule.group("body"), r"\bbackground\s*:")
        self.assertLess(z_index(cell_rule), z_index(body_sticky_rule))
        self.assertLess(z_index(body_sticky_rule), z_index(sticky_top_rule))
        self.assertLess(z_index(sticky_top_rule), z_index(head_sticky_rule))
        self.assertLess(z_index(head_sticky_rule), z_index(top_left_rule))
        render = extract_function(self.script, "renderMatrix")
        self.assertRegex(render, r"<thead>[\s\S]*sticky-top")

    def test_transaction_conflicts_never_fall_back_to_another_save_target(self) -> None:
        self.assertRegex(self.script, r"class\s+SaveApiError\b")
        conflict_helper = extract_function(self.script, "isSaveConflictError")
        self.assertRegex(conflict_helper, r"\b409\b")
        self.assertRegex(conflict_helper, r"\b412\b")

        save_patch = extract_function(self.script, "savePatch")
        conflict_start = save_patch.find("if (isSaveConflictError(error))")
        fallback_start = save_patch.find("if (USE_SAVE_API)", conflict_start)
        self.assertGreaterEqual(conflict_start, 0, "savePatch must branch explicitly on stale-save conflicts")
        self.assertGreater(fallback_start, conflict_start, "conflict handling must precede generic API fallback")
        branch = save_patch[conflict_start:fallback_start]
        self.assertRegex(branch, r"reload|retry", "conflict status must tell the user how to recover")
        self.assertRegex(branch, r"return\s+finishSave\s*\(\s*false(?:\s*,\s*saveRequestId)?\s*\)")
        self.assertNotRegex(branch, r"writePatchToHandle|writePatchToSourceUrl|downloadPatchPayload")

    def test_save_completion_is_sequenced_and_only_latest_advances_baseline(self) -> None:
        for name in (
            "patchSaveRequestSequence",
            "latestPatchSaveRequestId",
            "activePatchSaveRequests",
        ):
            self.assertRegex(self.script, rf"\b{name}\b", f"missing save sequencing state: {name}")

        started = extract_function(self.script, "markPatchSaveStarted")
        self.assertRegex(started, r"(?:patchSaveRequestSequence\s*\+=\s*1|\+\+patchSaveRequestSequence)")
        self.assertRegex(started, r"latestPatchSaveRequestId\s*=")
        self.assertRegex(started, r"activePatchSaveRequests\s*\+=\s*1")
        self.assertRegex(started, r"return\s+\w+", "save start must return its request token")

        finished = extract_function(self.script, "markPatchSaveFinished")
        self.assertRegex(finished, r"activePatchSaveRequests\s*=.*-\s*1")
        self.assertRegex(finished, r"requestId\s*===\s*latestPatchSaveRequestId")
        baseline_assignment = finished.find("savedPatchFingerprint =")
        latest_guard = finished.find("requestId === latestPatchSaveRequestId")
        self.assertGreater(baseline_assignment, latest_guard, "baseline must advance only inside the latest-save guard")

        save_patch = extract_function(self.script, "savePatch")
        self.assertRegex(save_patch, r"saveRequestId\s*=\s*markPatchSaveStarted\s*\(")
        finish_wrapper = re.search(r"const\s+finishSave\s*=\s*\([^)]*\)\s*=>\s*\{(.*?)\};", save_patch, re.DOTALL)
        self.assertIsNotNone(finish_wrapper)
        self.assertIn("saveRequestId", finish_wrapper.group(1))

    def test_history_transactions_are_bounded_and_atomic(self) -> None:
        self.assertRegex(self.script, r"PATCH_HISTORY_LIMIT\s*=\s*\d+")
        record = extract_function(self.script, "recordPatchHistory")
        self.assertRegex(record, r"patchHistory\.splice\(patchHistoryIndex\s*\+\s*1\)")
        self.assertRegex(record, r"patchHistory\.length\s*>\s*PATCH_HISTORY_LIMIT")

        apply_range = extract_function(self.script, "applyPatchRangeFromStart")
        self.assertNotIn("recordPatchHistory", apply_range, "range members must not become individual undo entries")
        paint_cell = extract_function(self.script, "paintCell")
        self.assertNotIn("recordPatchHistory", paint_cell, "paint movement must not create per-cell history entries")
        end_paint = extract_function(self.script, "endPaintSession")
        self.assertEqual(1, end_paint.count("recordPatchHistory("), "a paint gesture must create one history entry")
        report = extract_function(self.script, "reportPatchResult")
        self.assertEqual(2, report.count("recordPatchHistory("), "connect and disconnect each need one atomic history entry")

        restore = extract_function(self.script, "restorePatchHistoryEntry")
        self.assertIn("rebuildConnectionLookups()", restore)
        self.assertIn("scheduleRender()", restore)
        self.assertIn("queuePatchAutoSave(direction)", restore)

    def test_load_and_successful_save_define_dirty_baselines(self) -> None:
        reload_data = extract_function(self.script, "reloadData")
        self.assertRegex(reload_data, r"rebuildConnectionLookups\s*\(\s*\)\s*;\s*resetPatchHistory\s*\(")

        finished = extract_function(self.script, "markPatchSaveFinished")
        baseline_assignment = finished.find("savedPatchFingerprint =")
        combined_guard = re.search(
            r"if\s*\(\s*success\s*&&\s*requestId\s*===\s*latestPatchSaveRequestId\s*\)",
            finished,
        )
        if combined_guard:
            self.assertGreater(baseline_assignment, combined_guard.start())
        else:
            latest_guard = finished.find("requestId === latestPatchSaveRequestId")
            success_guard = finished.find("if (success)", latest_guard)
            self.assertGreaterEqual(latest_guard, 0)
            self.assertGreater(success_guard, latest_guard)
            self.assertGreater(baseline_assignment, success_guard)
        self.assertIn("savedPatchFingerprint", finished)
        self.assertIn("refreshPatchDirtyState()", finished)

    def test_parent_receives_complete_dirty_and_history_state(self) -> None:
        post_state = extract_function(self.script, "postPatchDirtyState")
        self.assertIn('type: "studio-prototype-patch-state"', post_state)
        for field in ("dirty", "saving", "last_save_succeeded", "can_undo", "can_redo"):
            self.assertRegex(post_state, rf"\b{field}\s*:")
        self.assertIn('window.parent.postMessage', post_state)

        before_unload = re.search(
            r"addEventListener\s*\(\s*[\"']beforeunload[\"']\s*,(.*?)\n\s*\}\s*\)",
            self.script,
            re.DOTALL,
        )
        self.assertIsNotNone(before_unload)
        self.assertIn("patchDirty", before_unload.group(1))
        self.assertIn("lastSaveSucceeded", before_unload.group(1))
        self.assertIn("event.returnValue", before_unload.group(1))

    def test_autosave_preference_persists_and_flushes_before_shell_navigation(self) -> None:
        self.assertIn('const AUTO_SAVE_STORAGE_KEY = "studioWiringAutoSaveToDiskV1"', self.script)
        self.assertIn("function loadAutoSavePreference()", self.script)
        self.assertIn("function persistAutoSavePreference(enabled)", self.script)
        set_enabled = extract_function(self.script, "setAutoSaveConfigsEnabled")
        self.assertIn("persistAutoSavePreference(autoSaveConfigsEnabled)", set_enabled)
        self.assertIn('queuePatchAutoSave("auto-save-enabled")', set_enabled)

        flush = extract_function(self.script, "flushPatchAutoSave")
        self.assertIn("patchAutoSaveTimer", flush)
        self.assertIn("patchAutoSavePromise", flush)
        self.assertIn("await patchAutoSavePromise", flush)
        self.assertIn('data.type === "studio-shell-autosave-flush"', self.script)
        self.assertIn('type: "studio-shell-autosave-flushed"', self.script)
        self.assertIn('flushPatchAutoSave("window-hidden")', self.script)

    def test_save_target_normalizer_keeps_project_paths_at_server_root(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable; save-target normalization check skipped")
        normalizer_start = self.script.index("function normalizeApiTargetPath")
        normalizer_end = self.script.index("\n    function buildSaveTargetsPayload", normalizer_start)
        normalizer = self.script[normalizer_start:normalizer_end].strip()
        harness = "\n".join(
            [
                'const window = { location: new URL("http://127.0.0.1:8000/prototypes/routing_matrix_prototype_compact.html") };',
                normalizer,
                "console.log(JSON.stringify([",
                '  normalizeApiTargetPath("projects/studio-sidecar/device-configurations/basis.json"),',
                '  normalizeApiTargetPath("/projects/studio-sidecar/patch-configurations/basis/basis.json"),',
                '  normalizeApiTargetPath("../projects/studio-sidecar/outputs/svgs"),',
                '  normalizeApiTargetPath("https://example.com/projects/foreign/model.json"),',
                "]));",
            ]
        )
        completed = subprocess.run(
            [node, "-"],
            input=harness,
            text=True,
            capture_output=True,
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            '["projects/studio-sidecar/device-configurations/basis.json",'
            '"projects/studio-sidecar/patch-configurations/basis/basis.json",'
            '"projects/studio-sidecar/outputs/svgs",""]',
            completed.stdout.strip(),
        )

    def test_matrix_uses_native_table_cells_with_inner_keyboard_controls(self) -> None:
        render = extract_function(self.script, "renderMatrix")
        self.assertNotRegex(render, r"<table[^>]*\brole=[\\\"']grid")
        self.assertNotRegex(render, r"<td[^>]*\brole=[\\\"'](?:checkbox|button)")
        self.assertIn("matrix-cell-control", render)
        self.assertRegex(render, r"matrix-cell-control[^>]*role=\\\"checkbox\\\"")
        self.assertRegex(render.replace("\n", " "), r"<td[^>]*>.*?<button", "actionable cells should contain native buttons")

        self.assertRegex(self.script, r"ArrowUp[\s\S]*ArrowDown[\s\S]*ArrowLeft[\s\S]*ArrowRight")
        self.assertRegex(self.script, r"event\.key\s*!==\s*[\"']Enter[\"'][\s\S]*event\.key\s*!==\s*[\"'] [\"']")

        self.assertRegex(
            render,
            r"table\.onkeydown[\s\S]*?eventClosest\s*\([^,]+,\s*[\"']\.matrix-cell-control",
            "keyboard delegation must target the inner focusable control",
        )
        self.assertRegex(
            render,
            r"table\.onfocusin[\s\S]*?eventClosest\s*\([^,]+,\s*[\"']\.matrix-cell-control",
            "focus delegation must target the inner focusable control",
        )
        focus_handler = re.search(r"table\.onfocusin\s*=\s*\([^)]*\)\s*=>\s*\{(.*?)\n\s*\};", render, re.DOTALL)
        self.assertIsNotNone(focus_handler)
        self.assertRegex(
            focus_handler.group(1),
            r"closest\s*\(\s*[\"']td\.cell[\"']\s*\)[\s\S]*?applyAxisHover",
            "keyboard focus hover must resolve the control's owning table cell",
        )
        roving = re.search(r"function\s+setRovingCellFocus\s*\([^)]*\)\s*\{(.*?)\n\s*\}", render, re.DOTALL)
        self.assertIsNotNone(roving)
        self.assertIn("HTMLButtonElement", roving.group(1))
        sync = re.search(r"function\s+syncChangedPatchCells\s*\([^)]*\)\s*\{(.*?)\n\s*\}", render, re.DOTALL)
        self.assertIsNotNone(sync)
        self.assertRegex(
            sync.group(1),
            r"matrix-cell-control[\s\S]*?setAttribute\(\s*[\"']aria-checked[\"']",
            "incremental patch sync must update ARIA state on the checkbox control",
        )

    def test_accessible_controls_and_live_state_have_unique_ids(self) -> None:
        parser = _MarkupInventory()
        parser.feed(self.html)
        duplicates = sorted({value for value in parser.ids if parser.ids.count(value) > 1})
        self.assertEqual([], duplicates, f"duplicate element IDs: {duplicates}")
        for required in ("undoBtn", "redoBtn", "saveState", "status", "matrixKeyboardHelp"):
            self.assertIn(required, parser.ids)
        self.assertGreaterEqual(len(parser.live_regions), 2)
        self.assertRegex(self.html, r'id="undoBtn"[^>]*aria-keyshortcuts=')
        self.assertRegex(self.html, r'id="redoBtn"[^>]*aria-keyshortcuts=')


if __name__ == "__main__":
    unittest.main()
