from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from generate_point_to_point import is_model_device_visible
from studio_wiring_schema.health import check_project
from studio_wiring_schema.migrations import migrate_document
from studio_wiring_schema.validation import validate_document


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def load_fixture(relative: str) -> dict[str, Any]:
    payload = json.loads((FIXTURES / relative).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


class ValidatorTests(unittest.TestCase):
    def test_all_supported_document_kinds_accept_valid_version_one_payloads(self) -> None:
        model = load_fixture("generator/model.json")
        patch = load_fixture("generator/connections.json")
        payloads = {
            "project": {
                "version": 1,
                "name": "Fixture",
                "paths": {
                    "device_model": "device-configurations/model.json",
                    "default_patch": "patch-configurations/model/default.json",
                    "patch_directory": "patch-configurations",
                },
                "device_patch_map": {
                    "device-configurations/model.json": [
                        "patch-configurations/model/default.json"
                    ]
                },
            },
            "model": model,
            "patch": patch,
            "routing_rules": {
                "version": 1,
                "labels": {
                    "source_side": "above",
                    "destination_side": "below",
                    "font_size": 7.0,
                },
                "routing": {
                    "fifo_forward_turns": True,
                    "backward_out_to_in_wrap": "below",
                    "video_early_turn": True,
                    "video_vertical_rows_threshold": 6.0,
                    "wire_clearance_px": 12.0,
                    "power_wire_clearance_px": 18.0,
                    "power_lane_spacing_px": 18.0,
                    "power_column_gap_px": 420.0,
                    "left_route_gutter_px": 108.0,
                },
            },
            "device_templates": {
                "version": 1,
                "title": "Fixture templates",
                "templates": [copy.deepcopy(model["devices"][0])],
            },
        }
        for kind, payload in payloads.items():
            with self.subTest(kind=kind):
                self.assertEqual(validate_document(kind, payload), [])

    def test_routing_clearance_rules_must_be_positive_numbers(self) -> None:
        rules = {
            "version": 1,
            "labels": {
                "source_side": "above",
                "destination_side": "below",
                "font_size": 7.0,
            },
            "routing": {
                "fifo_forward_turns": True,
                "backward_out_to_in_wrap": "below",
                "video_early_turn": True,
                "video_vertical_rows_threshold": 6.0,
                "wire_clearance_px": 0,
                "power_wire_clearance_px": -1,
                "power_lane_spacing_px": "wide",
                "power_column_gap_px": False,
                "left_route_gutter_px": None,
            },
        }

        keyed = {(issue.path, issue.code) for issue in validate_document("routing_rules", rules)}
        self.assertIn(("$.routing.wire_clearance_px", "number.positive"), keyed)
        self.assertIn(("$.routing.power_wire_clearance_px", "number.positive"), keyed)
        self.assertIn(("$.routing.power_lane_spacing_px", "number.positive"), keyed)
        self.assertIn(("$.routing.power_column_gap_px", "number.positive"), keyed)
        self.assertNotIn(("$.routing.left_route_gutter_px", "number.positive"), keyed)

    def test_issues_include_precise_field_paths_and_duplicate_details(self) -> None:
        model = load_fixture("generator/model.json")
        model["devices"][1]["name"] = model["devices"][0]["name"]
        model["devices"][0]["ports"][0]["visible"] = "yes"
        issues = validate_document("model", model)
        keyed = {(issue.path, issue.code) for issue in issues}
        self.assertIn(("$.devices[1].name", "device.duplicate"), keyed)
        self.assertIn(("$.devices[0].ports[0].visible", "type.boolean"), keyed)

    def test_device_visibility_targets_are_optional_booleans(self) -> None:
        model = load_fixture("generator/model.json")
        model["devices"][0]["visibility"] = {
            "wiring_matrix": True,
            "routing_matrix": False,
            "connection_overview": True,
            "visuals": False,
        }
        self.assertEqual(validate_document("model", model), [])

        model["devices"][0]["visibility"]["visuals"] = "no"
        keyed = {(issue.path, issue.code) for issue in validate_document("model", model)}
        self.assertIn(("$.devices[0].visibility.visuals", "type.boolean"), keyed)

    def test_visual_visibility_target_overrides_legacy_fallback(self) -> None:
        hidden_legacy = {"hidden": True, "visibility": {"visuals": True}}
        visible_legacy = {"visible": True, "visibility": {"visuals": False}}
        self.assertTrue(is_model_device_visible(hidden_legacy))
        self.assertFalse(is_model_device_visible(visible_legacy))
        self.assertFalse(is_model_device_visible(hidden_legacy, "wiring_matrix"))

    def test_patch_reports_duplicate_cable_and_endpoint_usage(self) -> None:
        patch = load_fixture("generator/connections.json")
        duplicate = copy.deepcopy(patch["connections"][0])
        patch["connections"].append(duplicate)
        codes = {issue.code for issue in validate_document("patch", patch)}
        self.assertIn("cable.duplicate", codes)
        self.assertIn("endpoint.duplicate_usage", codes)

    def test_unsupported_version_and_unsafe_path_are_reported_at_fields(self) -> None:
        project = {
            "version": 2,
            "name": "Unsafe fixture",
            "paths": {
                "device_model": "../outside.json",
                "default_patch": "patches/default.json",
                "patch_directory": "patches",
            },
            "device_patch_map": {},
        }
        keyed = {(issue.path, issue.code) for issue in validate_document("project", project)}
        self.assertIn(("$.version", "version.unsupported"), keyed)
        self.assertIn(("$.paths.device_model", "path.unsafe"), keyed)

    def test_legacy_devices_keep_implicit_desk_and_one_u_defaults_without_rewrite(self) -> None:
        model = load_fixture("generator/model.json")
        original = copy.deepcopy(model)
        self.assertEqual(validate_document("model", model), [])
        self.assertEqual(model, original)
        for device in model["devices"]:
            self.assertNotIn("rack_mountable", device)
            self.assertNotIn("location", device)
            self.assertNotIn("rack_units", device)
            self.assertNotIn("rack_position", device)

    def test_valid_desk_unplaced_rack_and_placed_rack_devices(self) -> None:
        model = {
            "version": 1,
            "title": "Rack fixture",
            "devices": [
                {"name": "Implicit Desk", "ports": []},
                {
                    "name": "Explicit Desk",
                    "location": "Desk",
                    "rack_units": 2,
                    "rack_position": None,
                    "ports": [],
                },
                {
                    "name": "Unplaced Rack",
                    "rack_mountable": True,
                    "location": "Rack",
                    "ports": [],
                },
                {
                    "name": "Placed Rack",
                    "rack_mountable": True,
                    "location": "Rack",
                    "rack_units": 3,
                    "rack_position": {"rack": 4, "start_u": 14},
                    "ports": [],
                },
                {
                    "name": "Adjacent Rack",
                    "rack_mountable": True,
                    "location": "Rack",
                    "rack_units": 2,
                    "rack_position": {"rack": 4, "start_u": 12},
                    "ports": [],
                },
                {
                    "name": "Same Units Other Rack",
                    "rack_mountable": True,
                    "location": "Rack",
                    "rack_units": 3,
                    "rack_position": {"rack": 3, "start_u": 14},
                    "ports": [],
                },
            ],
        }
        original = copy.deepcopy(model)
        self.assertEqual(validate_document("model", model), [])
        self.assertEqual(model, original)

    def test_legacy_rack_metadata_without_capability_remains_loadable_but_inactive(self) -> None:
        model = {
            "version": 1,
            "title": "Legacy rack fixture",
            "devices": [
                {
                    "name": "Legacy placement",
                    "location": "Rack",
                    "rack_units": 2,
                    "rack_position": {"rack": 1, "start_u": 4},
                    "ports": [],
                }
            ],
        }
        self.assertEqual(validate_document("model", model), [])
        self.assertNotIn("rack_mountable", model["devices"][0])

    def test_invalid_rack_fields_bounds_desk_position_and_overlap_are_precise(self) -> None:
        model = {
            "version": 1,
            "title": "Invalid rack fixture",
            "devices": [
                {
                    "name": "Bad metadata",
                    "location": "rack",
                    "rack_units": True,
                    "rack_position": {"rack": 0, "start_u": 17},
                    "ports": [],
                },
                {
                    "name": "Desk position",
                    "location": "Desk",
                    "rack_position": {"rack": 1, "start_u": 1},
                    "ports": [],
                },
                {
                    "name": "Out of bounds",
                    "rack_mountable": True,
                    "location": "Rack",
                    "rack_units": 2,
                    "rack_position": {"rack": 2, "start_u": 16},
                    "ports": [],
                },
                {
                    "name": "Rack A",
                    "rack_mountable": True,
                    "location": "Rack",
                    "rack_units": 3,
                    "rack_position": {"rack": 3, "start_u": 4},
                    "ports": [],
                },
                {
                    "name": "Rack B",
                    "rack_mountable": True,
                    "location": "Rack",
                    "rack_units": 2,
                    "rack_position": {"rack": 3, "start_u": 6},
                    "ports": [],
                },
                {
                    "name": "Speaker incorrectly assigned to rack",
                    "rack_mountable": False,
                    "location": "Rack",
                    "ports": [],
                },
            ],
        }
        keyed = {(issue.path, issue.code) for issue in validate_document("model", model)}
        expected = {
            ("$.devices[0].location", "rack.location"),
            ("$.devices[0].rack_units", "rack.units"),
            ("$.devices[0].rack_position.rack", "rack.number"),
            ("$.devices[0].rack_position.start_u", "rack.start_u"),
            ("$.devices[1].rack_position", "rack.desk_position"),
            ("$.devices[2].rack_position", "rack.out_of_bounds"),
            ("$.devices[4].rack_position", "rack.overlap"),
            ("$.devices[5].location", "rack.not_mountable"),
        }
        self.assertTrue(expected.issubset(keyed), keyed)

    def test_rack_position_requires_object_with_integer_coordinates(self) -> None:
        model = {
            "version": 1,
            "title": "Position types",
            "devices": [
                {
                    "name": "Array position",
                    "rack_mountable": True,
                    "location": "Rack",
                    "rack_position": [1, 2],
                    "ports": [],
                },
                {
                    "name": "Boolean position",
                    "rack_mountable": True,
                    "location": "Rack",
                    "rack_position": {"rack": True, "start_u": False},
                    "ports": [],
                },
            ],
        }
        keyed = {(issue.path, issue.code) for issue in validate_document("model", model)}
        self.assertIn(("$.devices[0].rack_position", "rack.position"), keyed)
        self.assertIn(("$.devices[1].rack_position.rack", "rack.number"), keyed)
        self.assertIn(("$.devices[1].rack_position.start_u", "rack.start_u"), keyed)


class MigrationTests(unittest.TestCase):
    def test_model_migration_is_non_mutating_deterministic_and_idempotent(self) -> None:
        legacy = load_fixture("schema/legacy_model.json")
        original = copy.deepcopy(legacy)
        first = migrate_document("model", legacy)
        second = migrate_document("model", legacy)

        self.assertEqual(legacy, original)
        self.assertEqual(first, second)
        self.assertEqual(first.data["version"], 1)
        self.assertEqual(first.data["title"], "Legacy Fixture Studio")
        device = first.data["devices"][0]
        self.assertEqual(device["name"], "Legacy Source")
        self.assertIs(device["visible"], True)
        self.assertEqual(device["ports"][0]["name"], "Out 1")
        self.assertIs(device["ports"][0]["hidden"], False)
        self.assertEqual(validate_document("model", first.data), [])

        repeated = migrate_document("model", first.data)
        self.assertEqual(repeated.data, first.data)
        self.assertEqual(repeated.changes, ())

    def test_patch_migration_flattens_known_endpoint_names_and_booleans(self) -> None:
        migrated = migrate_document("patch", load_fixture("schema/legacy_patch.json"))
        connection = migrated.data["connections"][0]
        self.assertEqual(connection["source_device"], "Legacy Source")
        self.assertEqual(connection["source_port"], "Out 1")
        self.assertEqual(connection["dest_device"], "Legacy Destination")
        self.assertEqual(connection["dest_port"], "In 1")
        self.assertIs(connection["override_1to1"], False)
        self.assertNotIn("source", connection)
        self.assertNotIn("destination", connection)
        self.assertEqual(validate_document("patch", migrated.data), [])

    def test_patch_migration_preserves_conflicting_legacy_endpoint_data(self) -> None:
        legacy = load_fixture("schema/legacy_patch.json")
        legacy["connections"][0]["source_device"] = "Canonical Source"
        migrated = migrate_document("patch", legacy)
        connection = migrated.data["connections"][0]
        self.assertEqual(connection["source_device"], "Canonical Source")
        self.assertIn("source", connection)


class ProjectHealthTests(unittest.TestCase):
    def make_project(self, root: Path) -> tuple[Path, Path]:
        model_path = root / "device-configurations" / "model.json"
        patch_path = root / "patch-configurations" / "model" / "default.json"
        write_json(model_path, load_fixture("generator/model.json"))
        write_json(patch_path, load_fixture("generator/connections.json"))
        write_json(
            root / "project.json",
            {
                "version": 1,
                "name": "Health Fixture",
                "paths": {
                    "device_model": "device-configurations/model.json",
                    "default_patch": "patch-configurations/model/default.json",
                    "patch_directory": "patch-configurations",
                },
                "device_patch_map": {
                    "device-configurations/model.json": [
                        "patch-configurations/model/default.json"
                    ]
                },
            },
        )
        return model_path, patch_path

    def test_healthy_project_has_no_issues_and_is_not_modified(self) -> None:
        with tempfile.TemporaryDirectory(prefix="project-health-test-") as temp_dir:
            root = Path(temp_dir)
            self.make_project(root)
            before = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(check_project(root), [])
            after = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

    def test_health_reports_missing_files_and_dangling_endpoints(self) -> None:
        with tempfile.TemporaryDirectory(prefix="project-health-test-") as temp_dir:
            root = Path(temp_dir)
            _, patch_path = self.make_project(root)
            patch = json.loads(patch_path.read_text(encoding="utf-8"))
            patch["connections"][0]["source_port"] = "Missing Out"
            write_json(patch_path, patch)
            metadata = json.loads((root / "project.json").read_text(encoding="utf-8"))
            metadata["device_patch_map"]["device-configurations/missing.json"] = []
            write_json(root / "project.json", metadata)

            issues = check_project(root)
            codes = {issue.code for issue in issues}
            self.assertIn("file.missing", codes)
            self.assertIn("endpoint.dangling", codes)
            dangling = next(issue for issue in issues if issue.code == "endpoint.dangling")
            self.assertIn("$.connections[0].source_device", dangling.path)


if __name__ == "__main__":
    unittest.main()
