"""Behavior contracts for POWER edits in the shipped canonical Wiring Matrix.

The JavaScript functions are extracted from the root UI and run against
synthetic state. Tests never load or write project-owned data.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_MATRIX = ROOT / "routing_matrix.html"
GENERATOR = ROOT / "generate_point_to_point.py"
TAB_MANIFEST = ROOT / "web" / "manifests" / "tabs.json"


def extract_inline_script(html: str) -> str:
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.DOTALL | re.IGNORECASE)
    if not scripts:
        raise AssertionError("canonical Wiring Matrix has no inline script")
    return "\n".join(scripts)


def extract_function(source: str, name: str) -> str:
    match = re.search(rf"function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", source)
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


def extract_json_constant(source: str, name: str) -> dict[str, object]:
    match = re.search(rf"const\s+{re.escape(name)}\s*=\s*", source)
    if not match:
        raise AssertionError(f"JSON constant {name} was not found")
    payload, _ = json.JSONDecoder().raw_decode(source[match.end() :])
    if not isinstance(payload, dict):
        raise AssertionError(f"JSON constant {name} is not an object")
    return payload


class PowerMatrixContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = CANONICAL_MATRIX.read_text(encoding="utf-8")
        cls.script = extract_inline_script(cls.html)
        cls.generator_source = GENERATOR.read_text(encoding="utf-8")

    def run_node(self, body: str) -> dict[str, object]:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is unavailable; POWER behavior check skipped")
        completed = subprocess.run(
            [node, "-e", body],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        payload = json.loads(completed.stdout)
        self.assertIsInstance(payload, dict)
        return payload

    def patch_harness(self, tail: str) -> str:
        function_names = [
                "normalizeFamily",
                "portsForFamily",
                "sharedFamiliesForPorts",
                "resolveLinkFamily",
                "powerConnectorAdvisory",
                "powerPortBadge",
                "endpointKey",
                "findConnectionIndex",
                "findStrictConflict",
                "connectionsForEndpoint",
                "normalizedPowerSourceTransport",
                "deviceSuppliesPower",
                "movablePowerConnection",
                "powerMoveHoverTitle",
                "guessTypeTag",
                "parseCableIdParts",
                "familySortIndex",
                "sortedConnectionsForIdSequence",
                "resolveCableIds",
                "normalizeConnectionIdsInState",
                "normalizePatchAction",
                "performPatchAction",
        ]
        # Include this legacy helper only while the implementation references it.
        if re.search(r"function\s+normalizedPowerSourceTransport\s*\(", self.script):
            function_names.append("normalizedPowerSourceTransport")
        functions = "\n".join(extract_function(self.script, name) for name in function_names)
        return "\n".join(
            (
                'const FAMILY_ALL = "ALL";',
                'const FAMILY_ORDER = ["AUDIO", "COMP", "DIGI", "NETWORK", "POWER"];',
                'const famDefs = { AUDIO: { prefix: "AUDIO" }, POWER: { prefix: "POWER" } };',
                "const portsByDevice = new Map([",
                "  ['Old PDU', [{ device: 'Old PDU', port: 'Outlet 4', direction: 'out', families: ['POWER'], transport: 'SCHUKO', visible: true, enabled: true }]],",
                "  ['New PDU', [{ device: 'New PDU', port: 'Outlet 2', direction: 'out', families: ['POWER'], transport: 'SCHUKO', visible: true, enabled: true }]],",
                "  ['Mismatched PDU', [{ device: 'Mismatched PDU', port: 'Outlet 2', direction: 'out', families: ['POWER'], transport: 'IEC C19', visible: true, enabled: true }]],",
                "  ['Powering Processor', [",
                "    { device: 'Powering Processor', port: 'AC In', direction: 'in', families: ['POWER'], transport: 'IEC C14', visible: true, enabled: true },",
                "    { device: 'Powering Processor', port: 'Power Out', direction: 'out', families: ['POWER'], transport: 'SCHUKO', visible: true, enabled: true },",
                "  ]],",
                "]);",
                'let selectedPatchMode = "single";',
                "const overrideToggle = null;",
                "const typeTagInput = null;",
                "function findPortMeta(device, port) { return (portsByDevice.get(device) || []).find((item) => item.port === port) || null; }",
                "function compareTextNatural(a, b) { return String(a).localeCompare(String(b)); }",
                "function comparePortsForModel(a, b) { return String(a.port).localeCompare(String(b.port)); }",
                functions,
                tail,
            )
        )

    def test_manifest_serves_the_canonical_matrix_with_power_available(self) -> None:
        manifest = json.loads(TAB_MANIFEST.read_text(encoding="utf-8"))
        tabs = manifest.get("tabs", manifest)
        self.assertIsInstance(tabs, list)
        matrix_tabs = [tab for tab in tabs if isinstance(tab, dict) and tab.get("key") == "connection-overview"]
        self.assertEqual(len(matrix_tabs), 1)
        self.assertRegex(str(matrix_tabs[0].get("src") or ""), r"(?:^|/)routing_matrix\.html(?:\?|$)")

        family_match = re.search(r"const\s+FAMILY_ORDER\s*=\s*(\[[^;]+\]);", self.script)
        self.assertIsNotNone(family_match)
        self.assertIn('"POWER"', family_match.group(1))
        init_family = extract_function(self.script, "initFamilySelect")
        self.assertIn("available.add(family)", init_family)
        self.assertIn("FAMILY_ORDER.filter", init_family)

        embedded_model = extract_json_constant(self.script, "EMBEDDED_MODEL")
        power_ports = [
            port
            for device in embedded_model.get("devices", [])
            if isinstance(device, dict)
            for port in device.get("ports", [])
            if isinstance(port, dict) and "POWER" in port.get("families", [])
        ]
        self.assertTrue(any(port.get("direction") in {"out", "io"} for port in power_ports))
        self.assertTrue(any(port.get("direction") in {"in", "io"} for port in power_ports))

    def test_canonical_ui_and_generator_keep_patch_functions_in_sync(self) -> None:
        for name in (
            "portsForFamily",
            "sharedFamiliesForPorts",
            "resolveLinkFamily",
            "powerConnectorAdvisory",
            "powerPortBadge",
            "findConnectionIndex",
            "findStrictConflict",
            "connectionsForEndpoint",
            "deviceSuppliesPower",
            "movablePowerConnection",
            "powerMoveHoverTitle",
            "guessTypeTag",
            "normalizePatchAction",
            "performPatchAction",
            "initFamilySelect",
        ):
            canonical = " ".join(extract_function(self.script, name).split())
            generated = " ".join(extract_function(self.generator_source, name).split())
            self.assertEqual(canonical, generated, name)

    def test_selected_family_requires_shared_family_and_axes_filter_by_family_and_direction(self) -> None:
        result = self.run_node(
            self.patch_harness(
                """
let connections = [];
const source = { device: "Source", port: "Out", direction: "out", families: ["AUDIO"], transport: "XLR", visible: true, enabled: true };
const destination = { device: "Destination", port: "In", direction: "in", families: ["NETWORK"], transport: "RJ45", visible: true, enabled: true };
const resolved = resolveLinkFamily("POWER", source, destination);
const action = performPatchAction("POWER", source, destination, "connect", { override: false });
console.log(JSON.stringify({ resolved, action, connections }));
"""
            )
        )
        self.assertEqual(result["resolved"], "")
        self.assertFalse(result["action"]["changed"])
        self.assertIn("Incompatible port families", result["action"]["error"])
        self.assertEqual(result["connections"], [])

        ports_for_family = extract_function(self.script, "portsForFamily")
        axis_result = self.run_node(
            "\n".join(
                (
                    'const FAMILY_ALL = "ALL";',
                    "const modelDevices = [{ name: 'Device', visible: true }];",
                    "const portsByDevice = new Map([['Device', [",
                    "  { port: 'Audio Out', direction: 'out', families: ['AUDIO'], visible: true },",
                    "  { port: 'Power Out', direction: 'out', families: ['POWER'], visible: true },",
                    "  { port: 'Network In', direction: 'in', families: ['NETWORK'], visible: true },",
                    "  { port: 'Power In', direction: 'in', families: ['POWER'], visible: true },",
                    "  { port: 'Hidden Out', direction: 'out', families: ['POWER'], visible: false },",
                    "]]]);",
                    "function isDeviceVisible(device) { return device.visible !== false; }",
                    "function isPatchbayDeviceName() { return false; }",
                    "function comparePortsForSide() { return 0; }",
                    ports_for_family,
                    "console.log(JSON.stringify({",
                    "  sources: portsForFamily('POWER', true).map((port) => port.port),",
                    "  destinations: portsForFamily('POWER', false).map((port) => port.port),",
                    "}));",
                )
            )
        )
        self.assertEqual(axis_result, {"sources": ["Power Out"], "destinations": ["Power In"]})

    def test_power_badges_are_accessible_on_source_and_destination_labels_only(self) -> None:
        result = self.run_node(
            self.patch_harness(
                """
let connections = [];
console.log(JSON.stringify({
  power: powerPortBadge({ families: ["POWER"] }),
  mixed: powerPortBadge({ families: ["AUDIO", "POWER"] }),
  audio: powerPortBadge({ families: ["AUDIO"] }),
}));
"""
            )
        )
        for key in ("power", "mixed"):
            markup = result[key]
            self.assertIn('class="port-family-badge"', markup)
            self.assertIn('title="POWER family port"', markup)
            self.assertIn('aria-label="POWER family port"', markup)
            self.assertRegex(markup, r">PWR</span>$")
        self.assertEqual(result["audio"], "")

        render_matrix = extract_function(self.script, "renderMatrix")
        self.assertIn("familyBadge = powerPortBadge(entry.port)", render_matrix)
        self.assertRegex(render_matrix, r'dest-port[^`]*\$\{familyBadge\}')
        self.assertIn('sourceFamilyBadge = sourceEntry.kind === "port" ? powerPortBadge(sourceEntry.port) : ""', render_matrix)
        self.assertRegex(render_matrix, r'class="prt"[^`]*\$\{sourceFamilyBadge\}')

    def test_power_connection_with_different_connector_transports_can_be_added(self) -> None:
        result = self.run_node(
            self.patch_harness(
                """
let connections = [];
const source = { device: "Wall", port: "Outlet 1", direction: "out", families: ["POWER"], transport: "SCHUKO", enabled: true };
const destination = { device: "Processor", port: "AC In", direction: "in", families: ["POWER"], transport: "IEC C14", enabled: true };
const advisory = powerConnectorAdvisory(resolveLinkFamily("POWER", source, destination), source, destination);
const action = performPatchAction("POWER", source, destination, "connect", { override: false });
normalizeConnectionIdsInState();
console.log(JSON.stringify({ advisory, action, connections }));
"""
            )
        )
        self.assertIn("SCHUKO", result["advisory"])
        self.assertIn("IEC C14", result["advisory"])
        self.assertIn("cable or adapter", result["advisory"])
        self.assertIn("Connection allowed", result["advisory"])
        self.assertTrue(result["action"]["changed"])
        self.assertEqual(result["action"]["linkFamily"], "POWER")
        self.assertEqual(len(result["connections"]), 1)
        self.assertEqual(result["connections"][0]["family"], "POWER")
        self.assertRegex(result["connections"][0]["cable_id"], r"^POWER-\d{3}$")

    def test_direct_existing_power_connection_remains_removable(self) -> None:
        result = self.run_node(
            self.patch_harness(
                """
let connections = [{ cable_id: "POWER-001", family: "POWER", source_device: "Wall", source_port: "Outlet 1",
  dest_device: "Processor", dest_port: "AC In", connection_type: "SCHUKO-IEC C13", status: "Connected",
  notes: "", override_1to1: false }];
const source = { device: "Wall", port: "Outlet 1", direction: "out", families: ["POWER"], transport: "SCHUKO", enabled: true };
const destination = { device: "Processor", port: "AC In", direction: "in", families: ["AUDIO"], transport: "IEC C14", enabled: true };
const conflictBefore = findStrictConflict("Wall", "Outlet 1", "Processor", "AC In");
const action = performPatchAction("POWER", source, destination, "disconnect", { override: false });
console.log(JSON.stringify({ action, conflictBefore: conflictBefore?.cable_id || "", connections }));
"""
            )
        )
        self.assertEqual(result["conflictBefore"], "POWER-001")
        self.assertTrue(result["action"]["changed"])
        self.assertEqual(result["action"]["action"], "disconnect")
        self.assertEqual(result["connections"], [])

    def test_strict_conflict_is_reported_separately_from_family_compatibility(self) -> None:
        result = self.run_node(
            self.patch_harness(
                """
let connections = [{ cable_id: "POWER-001", family: "POWER", source_device: "Wall", source_port: "Outlet 1",
  dest_device: "Existing", dest_port: "AC In", connection_type: "SCHUKO", status: "Connected",
  notes: "", override_1to1: false }];
const source = { device: "Wall", port: "Outlet 1", direction: "out", families: ["POWER"], transport: "SCHUKO", enabled: true };
const destination = { device: "Processor", port: "AC In", direction: "in", families: ["POWER"], transport: "IEC C14", enabled: true };
const action = performPatchAction("POWER", source, destination, "connect", { override: false });
console.log(JSON.stringify({ action, connections }));
"""
            )
        )
        self.assertFalse(result["action"]["changed"])
        self.assertEqual(result["action"]["linkFamily"], "POWER")
        self.assertIn("1:1 blocked", result["action"]["error"])
        self.assertNotIn("Incompatible port families", result["action"]["error"])

    def test_free_power_source_can_repatch_an_occupied_power_destination_in_place(self) -> None:
        result = self.run_node(
            self.patch_harness(
                """
let connections = [{ cable_id: "POWER-017", family: "POWER", source_device: "Old PDU", source_port: "Outlet 4",
  dest_device: "Processor", dest_port: "AC In", connection_type: "SCHUKO-IEC C13", status: "Connected",
  notes: "Keep this installation note", override_1to1: false }];
const source = { device: "New PDU", port: "Outlet 2", direction: "out", families: ["POWER"], transport: "SCHUKO", enabled: true };
const destination = { device: "Processor", port: "AC In", direction: "in", families: ["POWER"], transport: "IEC C14", enabled: true };
const action = performPatchAction("POWER", source, destination, "connect", { override: false, connectionType: "DO NOT REPLACE", reassignOccupiedPowerDestination: true });
console.log(JSON.stringify({ action, connections }));
"""
            )
        )
        self.assertTrue(result["action"]["changed"])
        self.assertEqual(result["action"]["action"], "move")
        self.assertEqual(result["action"]["linkFamily"], "POWER")
        self.assertEqual(result["action"]["previousSource"], {"device": "Old PDU", "port": "Outlet 4"})
        self.assertEqual(len(result["connections"]), 1)
        self.assertEqual(
            result["connections"][0],
            {
                "cable_id": "POWER-017",
                "family": "POWER",
                "source_device": "New PDU",
                "source_port": "Outlet 2",
                "dest_device": "Processor",
                "dest_port": "AC In",
                "connection_type": "SCHUKO-IEC C13",
                "status": "Connected",
                "notes": "Keep this installation note",
                "override_1to1": False,
            },
        )

    def test_power_repatch_requires_explicit_single_mode_opt_in(self) -> None:
        result = self.run_node(
            self.patch_harness(
                """
let connections = [{ cable_id: "POWER-017", family: "POWER", source_device: "Old PDU", source_port: "Outlet 4",
  dest_device: "Processor", dest_port: "AC In", connection_type: "SCHUKO-IEC C13", status: "Connected",
  notes: "original", override_1to1: false }];
const before = JSON.stringify(connections);
const source = { device: "New PDU", port: "Outlet 2", direction: "out", families: ["POWER"], transport: "SCHUKO", enabled: true };
const destination = { device: "Processor", port: "AC In", direction: "in", families: ["POWER"], transport: "IEC C14", enabled: true };
const withoutOptIn = performPatchAction("POWER", source, destination, "connect", { override: false });
const singleHover = powerMoveHoverTitle(source, destination, "POWER");
selectedPatchMode = "stereo";
const stereoHover = powerMoveHoverTitle(source, destination, "POWER");
console.log(JSON.stringify({ withoutOptIn, singleHover, stereoHover, before, after: JSON.stringify(connections) }));
"""
            )
        )
        self.assertFalse(result["withoutOptIn"]["changed"])
        self.assertIn("1:1 blocked", result["withoutOptIn"]["error"])
        self.assertEqual(result["before"], result["after"])
        self.assertIn("Click to move POWER", result["singleHover"])
        self.assertIn("Old PDU [Outlet 4]", result["singleHover"])
        self.assertEqual(result["stereoHover"], "")

    def test_power_repatch_allows_connector_mismatch_but_rejects_power_supplying_destinations(self) -> None:
        result = self.run_node(
            self.patch_harness(
                """
const sourceMismatch = { device: "Mismatched PDU", port: "Outlet 2", direction: "out", families: ["POWER"], transport: "IEC C19", enabled: true };
const leafDestination = { device: "Processor", port: "AC In", direction: "in", families: ["POWER"], transport: "IEC C14", enabled: true };
let connections = [{ cable_id: "POWER-001", family: "POWER", source_device: "Old PDU", source_port: "Outlet 4",
  dest_device: "Processor", dest_port: "AC In", connection_type: "SCHUKO-IEC C13", status: "Connected", notes: "", override_1to1: false }];
const mismatchAdvisory = powerConnectorAdvisory("POWER", sourceMismatch, leafDestination);
const mismatch = performPatchAction("POWER", sourceMismatch, leafDestination, "connect", { override: false, reassignOccupiedPowerDestination: true });

connections = [{ cable_id: "POWER-002", family: "POWER", source_device: "Old PDU", source_port: "Outlet 4",
  dest_device: "Powering Processor", dest_port: "AC In", connection_type: "SCHUKO-IEC C13", status: "Connected", notes: "", override_1to1: false }];
const matchingSource = { device: "New PDU", port: "Outlet 2", direction: "out", families: ["POWER"], transport: "SCHUKO", enabled: true };
const supplyingDestination = { device: "Powering Processor", port: "AC In", direction: "in", families: ["POWER"], transport: "IEC C14", enabled: true };
const suppliesPower = deviceSuppliesPower("Powering Processor");
const supplier = performPatchAction("POWER", matchingSource, supplyingDestination, "connect", { override: false, reassignOccupiedPowerDestination: true });
console.log(JSON.stringify({ mismatchAdvisory, mismatch, suppliesPower, supplier }));
"""
            )
        )
        self.assertIn("IEC C19", result["mismatchAdvisory"])
        self.assertIn("IEC C14", result["mismatchAdvisory"])
        self.assertTrue(result["mismatch"]["changed"])
        self.assertEqual(result["mismatch"]["action"], "move")
        self.assertEqual(result["mismatch"]["previousSource"], {"device": "Old PDU", "port": "Outlet 4"})
        self.assertTrue(result["suppliesPower"])
        self.assertFalse(result["supplier"]["changed"])
        self.assertIn("1:1 blocked", result["supplier"]["error"])

    def test_occupied_power_source_cannot_displace_another_power_row(self) -> None:
        result = self.run_node(
            self.patch_harness(
                """
let connections = [
  { cable_id: "POWER-001", family: "POWER", source_device: "Old PDU", source_port: "Outlet 4",
    dest_device: "Processor", dest_port: "AC In", connection_type: "SCHUKO-IEC C13", status: "Connected",
    notes: "target", override_1to1: false },
  { cable_id: "POWER-002", family: "POWER", source_device: "Busy PDU", source_port: "Outlet 2",
    dest_device: "Other Load", dest_port: "AC In", connection_type: "SCHUKO-IEC C13", status: "Connected",
    notes: "source already used", override_1to1: false },
];
const before = JSON.stringify(connections);
const source = { device: "Busy PDU", port: "Outlet 2", direction: "out", families: ["POWER"], transport: "SCHUKO", enabled: true };
const destination = { device: "Processor", port: "AC In", direction: "in", families: ["POWER"], transport: "IEC C14", enabled: true };
const action = performPatchAction("POWER", source, destination, "connect", { override: false, reassignOccupiedPowerDestination: true });
console.log(JSON.stringify({ action, before, after: JSON.stringify(connections), connections }));
"""
            )
        )
        self.assertFalse(result["action"]["changed"])
        self.assertEqual(result["action"]["linkFamily"], "POWER")
        self.assertIn("1:1 blocked", result["action"]["error"])
        self.assertEqual(result["before"], result["after"])
        self.assertEqual(len(result["connections"]), 2)

    def test_disabled_ports_remain_read_only(self) -> None:
        result = self.run_node(
            self.patch_harness(
                """
let connections = [];
const source = { device: "Wall", port: "Outlet", direction: "out", families: ["POWER"], transport: "SCHUKO", enabled: false };
const destination = { device: "Load", port: "In", direction: "in", families: ["POWER"], transport: "IEC C14", enabled: true };
const action = performPatchAction("POWER", source, destination, "connect", { override: false });
console.log(JSON.stringify({ action, connections }));
"""
            )
        )
        self.assertFalse(result["action"]["changed"])
        self.assertIn("Disabled port", result["action"]["error"])
        self.assertEqual(result["connections"], [])

    def test_non_power_destination_reuse_remains_strictly_blocked(self) -> None:
        result = self.run_node(
            self.patch_harness(
                """
let connections = [{ cable_id: "AUDIO-001", family: "AUDIO", source_device: "Old Source", source_port: "Line Out",
  dest_device: "Processor", dest_port: "Line In", connection_type: "MONO", status: "Connected",
  notes: "original audio route", override_1to1: false }];
const before = JSON.stringify(connections);
const source = { device: "New Source", port: "Line Out", direction: "out", families: ["AUDIO"], transport: "XLR", enabled: true };
const destination = { device: "Processor", port: "Line In", direction: "in", families: ["AUDIO"], transport: "TRS", enabled: true };
const action = performPatchAction("AUDIO", source, destination, "connect", { override: false });
console.log(JSON.stringify({ action, before, after: JSON.stringify(connections), connections }));
"""
            )
        )
        self.assertFalse(result["action"]["changed"])
        self.assertEqual(result["action"]["linkFamily"], "AUDIO")
        self.assertIn("1:1 blocked", result["action"]["error"])
        self.assertEqual(result["before"], result["after"])
        self.assertEqual(len(result["connections"]), 1)

    def test_existing_non_power_add_behavior_is_unchanged(self) -> None:
        result = self.run_node(
            self.patch_harness(
                """
let connections = [];
const source = { device: "Source", port: "Line Out", direction: "out", families: ["AUDIO"], transport: "XLR", enabled: true };
const destination = { device: "Destination", port: "Line In", direction: "in", families: ["AUDIO"], transport: "TRS", enabled: true };
const action = performPatchAction("AUDIO", source, destination, "connect", { override: false });
normalizeConnectionIdsInState();
console.log(JSON.stringify({ action, connections }));
"""
            )
        )
        self.assertTrue(result["action"]["changed"])
        self.assertEqual(result["connections"][0]["family"], "AUDIO")
        self.assertEqual(result["connections"][0]["connection_type"], "MONO")
        self.assertEqual(result["connections"][0]["cable_id"], "AUDIO-001")


if __name__ == "__main__":
    unittest.main()
