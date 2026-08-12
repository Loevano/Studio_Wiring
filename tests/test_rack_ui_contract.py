from __future__ import annotations

import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "routing_matrix.html"
GENERATOR = ROOT / "generate_point_to_point.py"
MANIFEST = ROOT / "web/manifests/tabs.json"


def extract_function(source: str, name: str) -> str:
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", source)
    if not match:
        raise AssertionError(f"function {name} was not found")
    opening = source.find("{", match.start())
    depth = 0
    quote = ""
    escaped = False
    index = opening
    while index < len(source):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
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


class _ElementInventory(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: dict[str, tuple[str, dict[str, str | None]]] = {}
        self.all_ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        element_id = values.get("id")
        if element_id:
            self.all_ids.append(element_id)
            self.elements[element_id] = (tag, values)


class RackUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = MATRIX.read_text(encoding="utf-8")
        cls.generator = GENERATOR.read_text(encoding="utf-8")
        cls.inventory = _ElementInventory()
        cls.inventory.feed(cls.html)

    def test_shell_exposes_rack_editor_tab(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        entries = [tab for tab in manifest.get("tabs", []) if tab.get("key") == "rack-editor"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].get("label"), "Rack Editor")
        self.assertEqual(entries[0].get("src"), "../../routing_matrix.html?embedded=1&tab=rack-editor")
        self.assertIn('const target = String(tabName || "matrix").toLowerCase()', self.html)
        self.assertIn('target === "rack" || target === "rack-editor"', self.html)
        self.assertIn('data.type === "studio-shell-main-tab-set"', self.html)

    def test_rack_editor_structure_is_unique_and_accessible(self) -> None:
        duplicates = sorted({value for value in self.inventory.all_ids if self.inventory.all_ids.count(value) > 1})
        self.assertEqual(duplicates, [], f"duplicate HTML IDs: {duplicates}")

        expected_tags = {
            "mainTabRack": "button",
            "panelRack": "div",
            "rackEditorDeviceSelect": "select",
            "rackEditorLocationSelect": "select",
            "rackEditorUnitsInput": "input",
            "rackEditorRackSelect": "select",
            "rackEditorStartUSelect": "select",
            "applyRackPlacementBtn": "button",
            "removeRackPlacementBtn": "button",
            "rackEditorStatus": "div",
            "rackEditorRacks": "div",
            "rackUnplacedList": "ul",
            "newDeviceRackMountable": "input",
            "deviceRackMountableInput": "input",
        }
        for element_id, expected_tag in expected_tags.items():
            self.assertIn(element_id, self.inventory.elements)
            tag, _attrs = self.inventory.elements[element_id]
            self.assertEqual(tag, expected_tag, f"#{element_id} must be a {expected_tag}")

        _tag, status_attrs = self.inventory.elements["rackEditorStatus"]
        self.assertEqual(status_attrs.get("role"), "status")
        self.assertEqual(status_attrs.get("aria-live"), "polite")
        for control_id in (
            "rackEditorDeviceSelect",
            "rackEditorLocationSelect",
            "rackEditorUnitsInput",
            "rackEditorRackSelect",
            "rackEditorStartUSelect",
        ):
            self.assertRegex(
                self.html,
                rf"<label[^>]*>[^<]*[\s\S]*?id=\"{control_id}\"",
                f"#{control_id} must have a visible label",
            )

    def test_normalization_preserves_legacy_defaults_and_bounds(self) -> None:
        location = extract_function(self.html, "normalizeDeviceLocation")
        units = extract_function(self.html, "normalizeRackUnits")
        position = extract_function(self.html, "normalizeRackPosition")

        self.assertIn('"Rack"', location)
        self.assertIn('"Desk"', location)
        self.assertRegex(location, r"return[\s\S]*Desk", "missing/invalid location must normalize to Desk")
        self.assertRegex(units, r"\b1\b")
        self.assertRegex(units, r"\b16\b")
        self.assertRegex(position, r"rack")
        self.assertRegex(position, r"start_u")
        self.assertRegex(position, r"return\s+null")

    def test_placement_rejects_wrong_location_bounds_and_overlap(self) -> None:
        placement = extract_function(self.html, "canPlaceRackDevice")
        apply_placement = extract_function(self.html, "applyRackEditorPlacement")
        capability = extract_function(self.html, "isRackMountableDevice")

        self.assertIn("rack_mountable === true", capability)
        self.assertIn("isRackMountableDevice", placement)
        self.assertIn("normalizeRackUnits", placement)
        self.assertRegex(placement, r"start_u|startU")
        self.assertRegex(placement, r"end_u|endU|rackUnits")
        self.assertRegex(placement, re.compile(r"overlap|occupied|conflict", re.IGNORECASE))
        self.assertIn("canPlaceRackDevice", apply_placement)
        self.assertIn("rack_position", apply_placement)
        self.assertIn('location === "Desk"', apply_placement)
        self.assertIn("delete device.rack_position", apply_placement)
        self.assertIn("refreshFromModelEdit", apply_placement)

    def test_render_includes_only_unplaced_rack_devices_and_four_descending_16u_racks(self) -> None:
        render = extract_function(self.html, "renderRackEditor")
        self.assertIn("rackUnplacedList", render)
        self.assertIn("rackEditorRacks", render)
        self.assertNotIn("rackDeskList", self.html)
        self.assertNotIn("rackDeskHeading", self.html)
        self.assertRegex(render, r"Array\.from\(\{\s*length:\s*4\s*\}")
        self.assertRegex(render, r"Array\.from\(\{\s*length:\s*16\s*\}")
        self.assertRegex(render, r"16\s*-\s*rowIndex")
        self.assertRegex(render, r"rack-device|rackDevice|rack-block|rackBlock")

    def test_rack_manager_uses_explicit_mountability_and_exposes_drag_sources_and_targets(self) -> None:
        render = extract_function(self.html, "renderRackEditor")
        rack_list = extract_function(self.html, "rackDeviceListHtml")
        self.assertRegex(
            render,
            r"const\s+rackDevices\s*=\s*sortedDevicesForEditor\(\)\.filter\([\s\S]*?isRackMountableDevice\(device\)",
            "Rack editor inventory must derive only from explicitly rack-mountable devices.",
        )
        rack_devices_start = render.index("const rackDevices")
        rack_only_render = render[rack_devices_start:]
        self.assertIn("rackDevices.map", rack_only_render)
        self.assertNotIn("deskDevices", rack_only_render)
        self.assertIn('draggable="true"', rack_only_render)
        self.assertIn("data-rack-drag-device", rack_only_render)
        self.assertIn("data-rack-drop-rack", rack_only_render)
        self.assertIn("data-rack-drop-unit", rack_only_render)
        self.assertIn('draggable="true"', rack_list)
        self.assertIn("data-rack-drag-device", rack_list)

    def test_drag_drop_delegates_to_validated_placement_without_removing_form_fallback(self) -> None:
        move = extract_function(self.html, "moveRackDeviceToPosition")
        drop_position = extract_function(self.html, "rackDropPlacementFromPointer")
        validation = move.index("canPlaceRackDevice")
        mutation = move.index("device.rack_position")
        refresh = move.index("refreshFromModelEdit")
        self.assertLess(validation, mutation, "A rejected drop must not mutate a device placement.")
        self.assertLess(mutation, refresh, "A successful drop must use the normal model-refresh path.")
        self.assertIn("isRackMountableDevice(device)", move)
        self.assertIn('device.location = "Rack"', move)
        self.assertRegex(drop_position, r"clientY")
        self.assertRegex(drop_position, r"grabOffsetU")
        self.assertRegex(drop_position, r"startU|start_u")

        event_block_start = self.html.index("if (panelRack)")
        event_block_end = self.html.index("if (deviceListPanel)", event_block_start)
        events = self.html[event_block_start:event_block_end]
        for event_name in ("ondragstart", "ondragover", "ondragleave", "ondrop", "ondragend"):
            self.assertIn(event_name, events)
        self.assertIn("application/x-studio-rack-device", events)
        self.assertIn("moveRackDeviceToPosition", events)
        self.assertIn("applyRackPlacementBtn", self.html)
        self.assertIn("applyRackEditorPlacement()", self.html)

    def test_device_rename_waits_for_rack_validation(self) -> None:
        commit = extract_function(self.html, "commitPendingDeviceEditorEdits")
        validation = commit.index("canPlaceRackDevice")
        endpoint_rename = commit.index("row.source_device = nextName")
        self.assertLess(
            validation,
            endpoint_rename,
            "connection endpoints must not be renamed before rack validation can fail",
        )

    def test_generator_is_source_of_shipped_rack_ui(self) -> None:
        for marker in (
            'id="mainTabRack"',
            'id="panelRack"',
            "function normalizeDeviceLocation(",
            "function isRackMountableDevice(",
            "function canPlaceRackDevice(",
            "function renderRackEditor(",
            "function applyRackEditorPlacement(",
            "function moveRackDeviceToPosition(",
            "data-rack-drag-device",
            "data-rack-drop-rack",
        ):
            self.assertIn(marker, self.generator)
            self.assertIn(marker, self.html)


if __name__ == "__main__":
    unittest.main()
