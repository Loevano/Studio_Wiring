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

        ports = [port for port in device.get("ports", []) if isinstance(port, dict)]
        names = [str(port.get("name") or "") for port in ports]
        self.assertEqual(len(names), len(set(names)), "headphone amp port names must be unique")
        self.assertEqual(
            names,
            [*[f"HA In {index}" for index in range(1, 7)], "Main In", *[f"HP Out {index}" for index in range(1, 7)]],
        )
        self.assertEqual([port.get("order") for port in ports], list(range(13)))

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

        connection = module.Connection
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
        self.assertEqual(power_conditioner.get("ports"), [])

        lucid = self.devices["Lucid 88192"]
        self.assertEqual(lucid.get("device_type"), "Converter")
        self.assertTrue(lucid.get("rack_mountable"))
        self.assertEqual(lucid.get("rack_units"), 2)
        self.assertEqual(len(lucid.get("ports", [])), 30)
        self.assertEqual(
            Counter((port["direction"], port["transport"]) for port in lucid["ports"]),
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
            {(port["direction"], tuple(port["families"]), port["transport"]) for port in lucid["ports"]},
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
