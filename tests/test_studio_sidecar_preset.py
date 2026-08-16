"""Regression contracts for the Studio Sidecar preset and its shipped artifacts."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "projects" / "studio-sidecar"
MODEL_PATH = SIDECAR / "device-configurations" / "basis.json"
PATCH_DIR = SIDECAR / "patch-configurations" / "basis"
GENERATOR_PATH = ROOT / "generate_point_to_point.py"
ROUTING_MATRIX_PATH = ROOT / "routing_matrix.html"
CURRENT_HEADPHONE_AMP = "IMG STAGELINE PPA-100/SW"
FORMER_GENERIC_NAME = "Headphone Amp"
POWER_DISTRIBUTORS = {
    "the t.racks Power MS6": 6,
    "the t.racks Power 8 #1": 8,
    "the t.racks Power 8 #2": 8,
    "the t.racks Power 8 #3": 8,
    "Black Lion Audio PG-1 Type F MKII": 8,
}
POWERED_LOADS = {
    "Allen & Heath GS3000",
    "Allen & Heath RPS11",
    "Audient ASP 880",
    "Avid S1 #1",
    "Avid S1 #2",
    "Behringer A800 #1",
    "Behringer A800 #2",
    "Focusrite Platinum Voice Master",
    "IMG STAGELINE PPA-100/SW",
    "Lucid 88192",
    "MAO Preamp (confirm model)",
    "Mac mini",
    "Netgear Unmanaged Switch",
    "RME UFX III",
    "SSL AX MADI",
    "Sony DPS-R7 Reverb",
    "TC Electronic Clarity M Stereo",
    "TC Electronic Finalizer 48K",
    "Tascam MS-16",
    "TV Screen",
    "Thunderbolt Dock",
}
POWER_EXEMPT_DEVICES = {
    "ATC SCM 11",
    "Talkback Mic",
    "Tannoy System 10",
    "Streamdeck #1",
    "Streamdeck #2",
    *{f"Switchcraft 1U Solder-Lug Patchbay #{index}" for index in range(1, 5)},
}
MODELS_WITH_UNVERIFIED_POWER_INLETS = {
    "MAO Preamp (confirm model)",
    "Netgear Unmanaged Switch",
    "Tascam MS-16",
    "Thunderbolt Dock",
    "TV Screen",
}


def load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


class StudioSidecarPresetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.model = load_json(MODEL_PATH)
        cls.devices = {
            str(device.get("name")): device
            for device in cls.model.get("devices", [])
            if isinstance(device, dict) and str(device.get("name") or "").strip()
        }
        cls.patch_paths = sorted(PATCH_DIR.glob("*.json"))
        cls.patches = {path.name: load_json(path) for path in cls.patch_paths}

    def test_actual_headphone_amp_has_rack_metadata_and_complete_port_inventory(self) -> None:
        device = self.devices.get(CURRENT_HEADPHONE_AMP)
        self.assertIsNotNone(device)
        self.assertTrue(device.get("rack_mountable"))
        self.assertEqual(device.get("rack_units"), 1)
        self.assertEqual(device.get("layout_group"), "Monitoring")

        all_ports = [port for port in device.get("ports", []) if isinstance(port, dict)]
        ports = [
            port
            for port in all_ports
            if "POWER" not in port.get("families", [])
        ]
        names = [str(port.get("name") or "") for port in ports]
        self.assertEqual(len(names), len(set(names)), "headphone amp port names must be unique")
        self.assertEqual(
            names,
            [*[f"HA In {index}" for index in range(1, 7)], "Main In", *[f"HP Out {index}" for index in range(1, 7)]],
        )
        self.assertEqual([port.get("order") for port in all_ports], list(range(len(all_ports))))

        for prefix, direction, expected_group, positions in (
            ("HA In", "in", "HA In", range(1, 7)),
            ("HP Out", "out", "HP Out", range(1, 7)),
        ):
            for position in positions:
                port = next(port for port in ports if port.get("name") == f"{prefix} {position}")
                self.assertEqual(port.get("direction"), direction)
                self.assertIn("AUDIO", port.get("families", []))
                self.assertEqual(
                    port.get("group"),
                    {"name": expected_group, "member": str(position), "index": position, "size": 6},
                )
        main_input = next(port for port in ports if port.get("name") == "Main In")
        self.assertEqual(main_input.get("direction"), "in")
        self.assertIn("AUDIO", main_input.get("families", []))

    def test_every_saved_patch_variant_uses_valid_model_endpoints(self) -> None:
        self.assertEqual(
            set(self.patches),
            {"basis.json", "patch-default-001.json", "patch-default.json", "patch-empty.json", "patch-v3-backup.json"},
        )
        ports_by_device = {
            name: {str(port.get("name")): port for port in device.get("ports", []) if isinstance(port, dict)}
            for name, device in self.devices.items()
        }
        for patch_name, patch in self.patches.items():
            rows = patch.get("connections", [])
            self.assertIsInstance(rows, list, patch_name)
            actual_amp_rows = []
            for row in rows:
                self.assertIsInstance(row, dict, patch_name)
                source_device = str(row.get("source_device") or "")
                dest_device = str(row.get("dest_device") or "")
                source_port = str(row.get("source_port") or "")
                dest_port = str(row.get("dest_port") or "")
                family = str(row.get("family") or "")
                self.assertNotEqual(source_device, FORMER_GENERIC_NAME, patch_name)
                self.assertNotEqual(dest_device, FORMER_GENERIC_NAME, patch_name)
                self.assertIn(source_device, ports_by_device, f"{patch_name}: unknown source device")
                self.assertIn(dest_device, ports_by_device, f"{patch_name}: unknown destination device")
                self.assertIn(source_port, ports_by_device[source_device], f"{patch_name}: unknown source port")
                self.assertIn(dest_port, ports_by_device[dest_device], f"{patch_name}: unknown destination port")
                source = ports_by_device[source_device][source_port]
                dest = ports_by_device[dest_device][dest_port]
                self.assertIn(source.get("direction"), {"out", "io"}, f"{patch_name}: source direction")
                self.assertIn(dest.get("direction"), {"in", "io"}, f"{patch_name}: destination direction")
                self.assertIn(family, source.get("families", []), f"{patch_name}: source family")
                self.assertIn(family, dest.get("families", []), f"{patch_name}: destination family")
                if CURRENT_HEADPHONE_AMP in {source_device, dest_device}:
                    actual_amp_rows.append(row)

            if patch_name == "patch-empty.json":
                self.assertEqual(rows, [])
            else:
                self.assertTrue(actual_amp_rows, f"{patch_name} must preserve headphone-amp wiring")

    def test_power_inventory_captures_real_distributor_capacity_and_load_inlets(self) -> None:
        """POWER is an explicit electrical topology, not a decorative device category."""
        def power_ports(device: dict[str, object], direction: str) -> list[dict[str, object]]:
            return [
                port
                for port in device.get("ports", [])
                if isinstance(port, dict)
                and port.get("direction") == direction
                and "POWER" in port.get("families", [])
            ]

        def is_type_f(port: dict[str, object]) -> bool:
            return str(port.get("transport") or "").upper() in {"SCHUKO", "TYPE F", "CEE 7/7"}

        self.assertIn("Studio Wall Power", self.devices)
        self.assertIn("Allen & Heath RPS11", self.devices)
        wall_outlets = power_ports(self.devices["Studio Wall Power"], "out")
        self.assertEqual(len(wall_outlets), len(POWER_DISTRIBUTORS))
        self.assertTrue(all(is_type_f(port) for port in wall_outlets))

        for name, outlet_count in POWER_DISTRIBUTORS.items():
            device = self.devices.get(name)
            self.assertIsNotNone(device, name)
            self.assertEqual(len(power_ports(device, "in")), 1, name)
            outputs = power_ports(device, "out")
            self.assertEqual(len(outputs), outlet_count, name)
            self.assertTrue(all(is_type_f(port) for port in outputs), name)

        black_lion = self.devices["Black Lion Audio PG-1 Type F MKII"]
        black_lion_groups = Counter(
            str(port.get("group", {}).get("name"))
            for port in power_ports(black_lion, "out")
            if isinstance(port.get("group"), dict)
        )
        self.assertEqual(
            black_lion_groups,
            Counter(
                {
                    "Digital Outlet": 2,
                    "Analog Outlet": 2,
                    "High Current Outlet": 2,
                    "Front Unswitched Outlet": 2,
                }
            ),
        )

        rps11 = self.devices["Allen & Heath RPS11"]
        self.assertEqual(len(power_ports(rps11, "in")), 1)
        self.assertEqual(len(power_ports(rps11, "out")), 1)

        for name in POWERED_LOADS:
            self.assertIn(name, self.devices)
            self.assertEqual(len(power_ports(self.devices[name], "in")), 1, name)
        for name in POWER_EXEMPT_DEVICES:
            self.assertIn(name, self.devices)
            self.assertEqual(power_ports(self.devices[name], "in"), [], name)

        for name, device in self.devices.items():
            for port in power_ports(device, "in"):
                unverified_inlet = "verify" in str(port.get("name") or "").lower() or str(port.get("transport") or "").upper() == "TBD"
                if unverified_inlet:
                    self.assertIn(name, MODELS_WITH_UNVERIFIED_POWER_INLETS, name)

    def test_active_patch_has_a_complete_non_daisy_chained_power_topology(self) -> None:
        active_rows = [
            row
            for row in self.patches["basis.json"].get("connections", [])
            if isinstance(row, dict) and row.get("family") == "POWER"
        ]
        self.assertEqual(len(active_rows), 26, "one route per powered inlet is required")
        self.assertEqual(len({row.get("cable_id") for row in active_rows}), len(active_rows))

        upstream_by_destination = Counter(
            (str(row.get("dest_device") or ""), str(row.get("dest_port") or ""))
            for row in active_rows
        )
        expected_inlets = {
            (name, str(port.get("name") or ""))
            for name in {*POWER_DISTRIBUTORS, *POWERED_LOADS}
            for port in self.devices[name].get("ports", [])
            if isinstance(port, dict)
            and port.get("direction") == "in"
            and "POWER" in port.get("families", [])
        }
        self.assertEqual(set(upstream_by_destination), expected_inlets)
        self.assertTrue(all(count == 1 for count in upstream_by_destination.values()))

        wall_feeds = [row for row in active_rows if row.get("dest_device") in POWER_DISTRIBUTORS]
        self.assertEqual(len(wall_feeds), len(POWER_DISTRIBUTORS))
        self.assertTrue(all(row.get("source_device") == "Studio Wall Power" for row in wall_feeds))
        self.assertFalse(
            any(
                row.get("source_device") in POWER_DISTRIBUTORS
                and row.get("dest_device") in POWER_DISTRIBUTORS
                for row in active_rows
            ),
            "power distributors must not be daisy-chained",
        )
        self.assertTrue(
            any(
                row.get("source_device") == "Allen & Heath RPS11"
                and row.get("dest_device") == "Allen & Heath GS3000"
                for row in active_rows
            )
        )

        for name in ("Streamdeck #1", "Streamdeck #2"):
            self.assertFalse(any(row.get("dest_device") == name for row in active_rows), name)
            self.assertTrue(
                any(
                    row.get("family") == "COMP" and row.get("dest_device") == name
                    for row in self.patches["basis.json"].get("connections", [])
                    if isinstance(row, dict)
                ),
                f"{name} must remain bus-powered through its computer/data route",
            )

    def test_generator_behavior_and_layout_use_the_actual_device_identity(self) -> None:
        spec = importlib.util.spec_from_file_location("studio_generator", GENERATOR_PATH)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)

        self.assertEqual(module.classify_port_family("12V DC In"), "power")
        self.assertNotEqual(module.classify_port_family("ADC In 1"), "power")

        connection = module.Connection
        power_tag_cases = {
            "SCHUKOIECC13": "C13",
            "SCHUKOIECC7": "C7",
            "SCHUKO": "SCHUKO",
            "AHRPS11DC": "RPS11",
            "SCHUKO12VADAPTER": "ADAPTER",
            "SCHUKODCADAPTER": "ADAPTER",
            "SCHUKOADAPTER": "ADAPTER",
            "SCHUKOVERIFY": "TBD",
        }
        for raw_type, expected_tag in power_tag_cases.items():
            power_connection = connection(
                "POWER-999",
                "Power Source",
                "Outlet",
                "Powered Load",
                "Power In",
                "Power",
                "Mains Power",
                "Connected",
                "AC Power",
                raw_type,
                "",
            )
            self.assertEqual(module.display_connection_type_tag(power_connection), expected_tag, raw_type)
            self.assertEqual(module.render_cable_label(power_connection), f"POWER-999 {expected_tag}", raw_type)

        non_power_connection = connection(
            "AUDIO-999",
            "Audio Source",
            "Line Out",
            "Audio Destination",
            "Line In",
            "Audio Analog",
            "Analog Audio",
            "Connected",
            "Analog",
            "MONO",
            "",
        )
        self.assertEqual(module.display_connection_type_tag(non_power_connection), "MONO")
        self.assertEqual(module.render_cable_label(non_power_connection), "AUDIO-999 MONO")

        stereo_pair = [
            connection("AUDIO-001", "SSL AX MADI", "Line Out 1", CURRENT_HEADPHONE_AMP, "HA In 1", "Audio Analog", "Analog Audio", "Connected", "", "MONO", ""),
            connection("AUDIO-002", "SSL AX MADI", "Line Out 2", CURRENT_HEADPHONE_AMP, "HA In 1", "Audio Analog", "Analog Audio", "Connected", "", "MONO", ""),
        ]
        collapsed = module.collapse_stereo_headphone_connections_for_render(stereo_pair)
        self.assertEqual(len(collapsed), 1)
        self.assertEqual(collapsed[0].source_jack, "Line Out 1+Line Out 2")
        self.assertEqual(collapsed[0].connection_type, "ST")

        generic_pair = [
            connection("AUDIO-003", "SSL AX MADI", "Line Out 3", FORMER_GENERIC_NAME, "HA In 2", "Audio Analog", "Analog Audio", "Connected", "", "MONO", ""),
            connection("AUDIO-004", "SSL AX MADI", "Line Out 4", FORMER_GENERIC_NAME, "HA In 2", "Audio Analog", "Analog Audio", "Connected", "", "MONO", ""),
        ]
        self.assertEqual(len(module.collapse_stereo_headphone_connections_for_render(generic_pair)), 2)

        analog = module.apply_layer_column_overrides(
            "Audio Analog",
            [
                ["Talkback Mic"],
                ["Allen & Heath GS3000"],
                ["SSL AX MADI"],
                [CURRENT_HEADPHONE_AMP, "Behringer A800 #2", "Behringer A800 #1"],
                ["Tannoy System 10"],
            ],
        )
        self.assertEqual(analog[3], ["Behringer A800 #1", "Behringer A800 #2", CURRENT_HEADPHONE_AMP])
        overview = module.apply_layer_column_overrides(
            "All Connections",
            [
                ["Talkback Mic"],
                ["Mac mini"],
                ["TV Screen"],
                ["RME UFX III"],
                ["SSL AX MADI"],
                [CURRENT_HEADPHONE_AMP],
                ["Tannoy System 10"],
            ],
        )
        self.assertEqual(overview[5], [CURRENT_HEADPHONE_AMP])

    def test_generated_artifacts_and_rack_eligibility_match_the_preset(self) -> None:
        self.assertEqual(
            self.model["families"]["POWER"],
            {
                "prefix": "POWER",
                "layer": "Power",
                "signal_type": "Mains Power",
                "default_cable_type": "AC Power",
            },
        )
        generated = [
            ROUTING_MATRIX_PATH,
            SIDECAR / "outputs" / "html" / "studio_wiring_point_to_point.html",
            SIDECAR / "outputs" / "debug" / "route-debug.json",
            *(SIDECAR / "outputs" / "svgs").glob("*.svg"),
        ]
        power_svg = SIDECAR / "outputs" / "svgs" / "power.svg"
        self.assertTrue(power_svg.is_file(), "the POWER layer must ship as a dedicated SVG schematic")
        power_svg_text = power_svg.read_text(encoding="utf-8")
        self.assertIn("Studio Wall Power", power_svg_text)
        self.assertIn("Allen &amp; Heath RPS11", power_svg_text)
        self.assertIn("Black Lion Audio PG-1 Type F MKII", power_svg_text)
        visible_power_labels = re.findall(r"<text\b[^>]*>(POWER-\d{3}(?: [^<]+)?)</text>", power_svg_text)
        self.assertTrue(visible_power_labels)
        self.assertTrue(
            all(re.fullmatch(r"POWER-\d{3} (?:C13|C7|SCHUKO|RPS11|ADAPTER|TBD)", label) for label in visible_power_labels),
            visible_power_labels,
        )
        self.assertTrue(
            {"C13", "C7", "SCHUKO", "RPS11", "ADAPTER", "TBD"}
            <= {label.rsplit(" ", 1)[-1] for label in visible_power_labels}
        )
        self.assertFalse(
            any(re.search(r"SCHUKOIEC|SCHUKO.*ADAPTER|VERIFY", label) for label in visible_power_labels),
            visible_power_labels,
        )
        all_svg_text = (SIDECAR / "outputs" / "svgs" / "all-connections.svg").read_text(encoding="utf-8")
        self.assertIn('stroke="#f8fafc"', all_svg_text, "wire crossings need a background under-stroke")
        last_signal_wire = max(
            all_svg_text.rfind("<title>AUDIO-"),
            all_svg_text.rfind("<title>COMP-"),
            all_svg_text.rfind("<title>DIGI-"),
            all_svg_text.rfind("<title>NETWORK-"),
        )
        first_power_wire = all_svg_text.find("<title>POWER-")
        self.assertGreater(first_power_wire, last_signal_wire, "power wires must be painted on top")

        route_debug = load_json(SIDECAR / "outputs" / "debug" / "route-debug.json")
        overview_routes = route_debug.get("layers", {}).get("All Connections", [])
        crossing_routes = [
            route.get("cable_id")
            for route in overview_routes
            if isinstance(route, dict)
            and int(route.get("score", {}).get("box_crossings", 0)) > 0
        ]
        self.assertEqual(crossing_routes, [], "overview wires must route around device boxes")
        headphone_amp_artifacts = [
            ROUTING_MATRIX_PATH,
            SIDECAR / "outputs" / "html" / "studio_wiring_point_to_point.html",
            SIDECAR / "outputs" / "debug" / "route-debug.json",
            SIDECAR / "outputs" / "svgs" / "all-connections.svg",
            SIDECAR / "outputs" / "svgs" / "audio-analog.svg",
        ]
        for artifact in headphone_amp_artifacts:
            self.assertIn(CURRENT_HEADPHONE_AMP, artifact.read_text(encoding="utf-8"), artifact)
        for artifact in generated:
            text = artifact.read_text(encoding="utf-8")
            self.assertNotRegex(text, r'"name"\\s*:\\s*"Headphone Amp"', artifact)

        for name in ("ATC SCM 11", "Tannoy System 10", "Avid S1 #1", "Avid S1 #2"):
            self.assertFalse(self.devices[name].get("rack_mountable"), name)
        for name in ("the t.racks Power MS6", "the t.racks Power 8 #1", "the t.racks Power 8 #2", "the t.racks Power 8 #3"):
            self.assertIn(name, self.devices)
            self.assertTrue(self.devices[name].get("rack_mountable"), name)
            self.assertEqual(self.devices[name].get("rack_units"), 1, name)
        for index in range(1, 5):
            name = f"Switchcraft 1U Solder-Lug Patchbay #{index}"
            self.assertIn(name, self.devices)
            self.assertEqual(self.devices[name].get("device_type"), "Patchbay", name)
            self.assertTrue(self.devices[name].get("rack_mountable"), name)
            self.assertEqual(self.devices[name].get("rack_units"), 1, name)
            self.assertEqual(self.devices[name].get("ports"), [], name)

        power_conditioner = self.devices["Black Lion Audio PG-1 Type F MKII"]
        self.assertEqual(power_conditioner.get("device_type"), "Power Distribution")
        self.assertEqual(power_conditioner.get("layout_group"), "Rack Infrastructure")
        self.assertTrue(power_conditioner.get("rack_mountable"))
        self.assertEqual(power_conditioner.get("rack_units"), 1)
        self.assertEqual(len(power_conditioner.get("ports", [])), 9)

        lucid = self.devices["Lucid 88192"]
        self.assertEqual(lucid.get("device_type"), "Converter")
        self.assertTrue(lucid.get("rack_mountable"))
        self.assertEqual(lucid.get("rack_units"), 2)
        lucid_signal_ports = [
            port
            for port in lucid.get("ports", [])
            if "POWER" not in port.get("families", [])
        ]
        self.assertEqual(len(lucid_signal_ports), 30)
        self.assertEqual(
            Counter((port["direction"], port["transport"]) for port in lucid_signal_ports),
            Counter(
                {
                    ("in", "XLR"): 8,
                    ("out", "XLR"): 8,
                    ("in", "AES"): 4,
                    ("out", "AES"): 4,
                    ("in", "ADAT"): 2,
                    ("out", "ADAT"): 2,
                    ("in", "CLOCK"): 1,
                    ("out", "CLOCK"): 1,
                }
            ),
        )
        self.assertEqual(
            {(port["direction"], tuple(port["families"]), port["transport"]) for port in lucid_signal_ports},
            {
                ("in", ("AUDIO",), "XLR"),
                ("out", ("AUDIO",), "XLR"),
                ("in", ("DIGI",), "AES"),
                ("out", ("DIGI",), "AES"),
                ("in", ("DIGI",), "ADAT"),
                ("out", ("DIGI",), "ADAT"),
                ("in", ("DIGI",), "CLOCK"),
                ("out", ("DIGI",), "CLOCK"),
            },
        )

        production_files = [MODEL_PATH, *self.patch_paths, GENERATOR_PATH, *generated]
        for path in production_files:
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r'"(?:name|source_device|dest_device)"\\s*:\\s*"Headphone Amp"', path)


if __name__ == "__main__":
    unittest.main()
