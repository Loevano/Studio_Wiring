from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

from routing_matrix_server import (
    RoutingMatrixHandler,
    SaveConflictError,
    canonical_json_hash,
    execute_json_save_transaction,
    validate_save_transaction_changes,
    write_json_atomic,
    write_json_transaction,
)


class SaveTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        (self.root / "routing-rules.json").write_text("{}\n", encoding="utf-8")
        self.initial_model = {"version": 1, "devices": []}
        self.initial_connections = {"version": 1, "connections": []}
        for key in ("alpha", "beta"):
            project = self.root / "projects" / key
            (project / "device-configurations").mkdir(parents=True)
            (project / "patch-configurations").mkdir(parents=True)
            (project / "outputs" / "html").mkdir(parents=True)
            (project / "outputs" / "svgs").mkdir(parents=True)
            (project / "outputs" / "debug").mkdir(parents=True)
            write_json_atomic(project / "project.json", {"version": 1, "name": key})
            write_json_atomic(project / "device-configurations" / "model.json", self.initial_model)
            write_json_atomic(project / "patch-configurations" / "patch.json", self.initial_connections)
        self.lock = threading.Lock()
        self.regenerate_calls: list[tuple[Path, Path]] = []

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def targets(self, project_key: str) -> dict[str, Path]:
        base = self.root / "projects" / project_key
        return {
            "model_path": base / "device-configurations/model.json",
            "connections_path": base / "patch-configurations/patch.json",
        }

    def execute(
        self,
        project_key: str,
        changes: dict[str, object],
        expected: dict[str, object] | None = None,
    ) -> tuple[dict[str, str], bool, str]:
        targets = self.targets(project_key)
        requested = [(label, targets[f"{label}_path"], value) for label, value in changes.items()]
        if expected is None:
            expected = {}
            if "model" in changes:
                expected["model"] = canonical_json_hash(self.initial_model)
            if "connections" in changes:
                expected["connections"] = canonical_json_hash(self.initial_connections)

        def regenerate() -> tuple[bool, str]:
            self.regenerate_calls.append((targets["model_path"], targets["connections_path"]))
            return True, "generated"

        return execute_json_save_transaction(
            requested=requested,
            expected_hashes=expected,
            lock=self.lock,
            regenerate=True,
            regenerate_callback=regenerate,
        )

    def test_model_and_connections_commit_before_one_regeneration(self) -> None:
        model = {"version": 2, "devices": [{"name": "new"}]}
        connections = {"version": 2, "connections": [{"cable_id": "A-001"}]}
        hashes, regenerate_ok, _message = self.execute(
            "alpha", {"model": model, "connections": connections}
        )
        self.assertEqual(set(hashes), {"model", "connections"})
        self.assertTrue(regenerate_ok)
        self.assertEqual(len(self.regenerate_calls), 1)
        targets = self.targets("alpha")
        self.assertEqual(json.loads(targets["model_path"].read_text()), model)
        self.assertEqual(json.loads(targets["connections_path"].read_text()), connections)

    def test_concurrent_requests_keep_targets_request_scoped(self) -> None:
        alpha = {"version": 2, "devices": [{"name": "alpha"}]}
        beta = {"version": 2, "devices": [{"name": "beta"}]}
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda item: self.execute(item[0], {"model": item[1]}),
                    (("alpha", alpha), ("beta", beta)),
                )
            )
        self.assertEqual(len(results), 2)
        for key, expected in (("alpha", alpha), ("beta", beta)):
            self.assertEqual(json.loads(self.targets(key)["model_path"].read_text()), expected)
        self.assertEqual(
            {model for model, _connections in self.regenerate_calls},
            {self.targets("alpha")["model_path"], self.targets("beta")["model_path"]},
        )

    def test_hash_conflict_writes_nothing_and_does_not_regenerate(self) -> None:
        with self.assertRaises(SaveConflictError) as raised:
            self.execute("alpha", {"model": {"version": 9}}, {"model": "stale"})
        self.assertEqual(set(raised.exception.conflicts), {"model"})
        self.assertEqual(json.loads(self.targets("alpha")["model_path"].read_text()), self.initial_model)
        self.assertEqual(self.regenerate_calls, [])

    def test_invalid_transaction_payload_is_rejected_before_any_write(self) -> None:
        targets = self.targets("alpha")
        invalid_changes = {
            "model": {
                "version": 1,
                "title": "Invalid duplicate model",
                "devices": [
                    {"name": "Duplicate", "ports": []},
                    {"name": "Duplicate", "ports": []},
                ],
            },
            "connections": {
                "version": 1,
                "connections": [
                    {
                        "cable_id": "",
                        "source_device": "Duplicate",
                        "source_port": "Missing Out",
                        "dest_device": "Duplicate",
                        "dest_port": "Missing In",
                    }
                ],
            },
        }
        before_model = targets["model_path"].read_bytes()
        before_connections = targets["connections_path"].read_bytes()

        with self.assertRaisesRegex(ValueError, "validation failed") as raised:
            validate_save_transaction_changes(invalid_changes)
        keyed = {(issue.path, issue.code) for issue in raised.exception.issues}
        self.assertIn(("$.changes.model.devices[1].name", "device.duplicate"), keyed)
        self.assertIn(
            ("$.changes.connections.connections[0].cable_id", "value.non_empty_string"),
            keyed,
        )
        self.assertEqual(targets["model_path"].read_bytes(), before_model)
        self.assertEqual(targets["connections_path"].read_bytes(), before_connections)
        self.assertEqual(self.regenerate_calls, [])

        responses: list[tuple[dict[str, object], HTTPStatus]] = []
        handler = object.__new__(RoutingMatrixHandler)
        handler.path = "/api/save-transaction"
        handler._read_json_body = lambda: {
            "changes": invalid_changes,
            "expected_hashes": {
                "model": canonical_json_hash(self.initial_model),
                "connections": canonical_json_hash(self.initial_connections),
            },
        }
        handler._transaction_targets = lambda _payload: {
            "model_path": targets["model_path"],
            "connections_path": targets["connections_path"],
        }
        handler._send_json = lambda payload, status=HTTPStatus.OK: responses.append(
            (payload, status)
        )
        RoutingMatrixHandler.do_POST(handler)

        self.assertEqual(len(responses), 1)
        response, status = responses[0]
        self.assertEqual(status, HTTPStatus.UNPROCESSABLE_ENTITY)
        self.assertFalse(response["ok"])
        response_paths = {issue["path"] for issue in response["validation_issues"]}
        self.assertIn("$.changes.model.devices[1].name", response_paths)
        self.assertEqual(targets["model_path"].read_bytes(), before_model)
        self.assertEqual(targets["connections_path"].read_bytes(), before_connections)

    def test_rack_overlap_transaction_returns_422_without_writes_or_regeneration(self) -> None:
        class Handler(RoutingMatrixHandler):
            regenerate_calls = 0

            @classmethod
            def trigger_regenerate_for_targets(cls, **_kwargs: object) -> tuple[bool, str]:
                cls.regenerate_calls += 1
                return True, "generated"

        targets = self.targets("alpha")
        invalid_model = {
            "version": 1,
            "title": "Overlapping rack",
            "devices": [
                {
                    "name": "Rack Device A",
                    "rack_mountable": True,
                    "location": "Rack",
                    "rack_units": 4,
                    "rack_position": {"rack": 1, "start_u": 3},
                    "ports": [],
                },
                {
                    "name": "Rack Device B",
                    "rack_mountable": True,
                    "location": "Rack",
                    "rack_units": 2,
                    "rack_position": {"rack": 1, "start_u": 6},
                    "ports": [],
                },
            ],
        }
        before_model = targets["model_path"].read_bytes()
        before_connections = targets["connections_path"].read_bytes()

        with self.assertRaisesRegex(ValueError, "validation failed") as raised:
            validate_save_transaction_changes({"model": invalid_model})
        keyed = {(issue.path, issue.code) for issue in raised.exception.issues}
        self.assertIn(
            ("$.changes.model.devices[1].rack_position", "rack.overlap"),
            keyed,
        )

        responses: list[tuple[dict[str, object], HTTPStatus]] = []
        handler = object.__new__(Handler)
        handler.path = "/api/save-transaction"
        handler._read_json_body = lambda: {
            "changes": {"model": invalid_model},
            "expected_hashes": {"model": canonical_json_hash(self.initial_model)},
            "regenerate": True,
        }
        handler._transaction_targets = lambda _payload: {
            "model_path": targets["model_path"],
            "connections_path": targets["connections_path"],
        }
        handler._send_json = lambda payload, status=HTTPStatus.OK: responses.append(
            (payload, status)
        )

        Handler.do_POST(handler)

        self.assertEqual(len(responses), 1)
        response, status = responses[0]
        self.assertEqual(status, HTTPStatus.UNPROCESSABLE_ENTITY)
        self.assertFalse(response["ok"])
        self.assertIn(
            "$.changes.model.devices[1].rack_position",
            {issue["path"] for issue in response["validation_issues"]},
        )
        self.assertEqual(targets["model_path"].read_bytes(), before_model)
        self.assertEqual(targets["connections_path"].read_bytes(), before_connections)
        self.assertEqual(Handler.regenerate_calls, 0)
        self.assertEqual(self.regenerate_calls, [])

    def test_save_device_template_preserves_optional_rack_metadata(self) -> None:
        template_path = self.root / "templates" / "user.json"
        responses: list[tuple[dict[str, object], HTTPStatus]] = []
        handler = object.__new__(RoutingMatrixHandler)
        handler.path = "/api/save-device-template"
        handler._read_json_body = lambda: {
            "template_path": "templates/user.json",
            "template": {
                "name": "Rack Processor",
                "manufacturer": "Example",
                "device_type": "Processor",
                "layout_group": "Outboard",
                "rack_mountable": True,
                "location": "Rack",
                "rack_units": 3,
                "rack_position": {"rack": 2, "start_u": 8},
                "ports": [],
            },
        }
        handler._resolve_target_path = lambda _path: template_path
        handler._relative_path = lambda path: path.relative_to(self.root).as_posix()
        handler._config_payload = lambda: {}
        handler._send_json = lambda payload, status=HTTPStatus.OK: responses.append(
            (payload, status)
        )

        RoutingMatrixHandler.do_POST(handler)

        self.assertEqual(responses[0][1], HTTPStatus.OK)
        saved = json.loads(template_path.read_text(encoding="utf-8"))["templates"][0]
        self.assertIs(saved["rack_mountable"], True)
        self.assertEqual(saved["location"], "Rack")
        self.assertEqual(saved["rack_units"], 3)
        self.assertEqual(saved["rack_position"], {"rack": 2, "start_u": 8})

    def test_save_device_template_does_not_materialize_implicit_rack_defaults(self) -> None:
        template_path = self.root / "templates" / "legacy.json"
        responses: list[tuple[dict[str, object], HTTPStatus]] = []
        handler = object.__new__(RoutingMatrixHandler)
        handler.path = "/api/save-device-template"
        handler._read_json_body = lambda: {
            "template_path": "templates/legacy.json",
            "template": {"name": "Desk Legacy", "ports": []},
        }
        handler._resolve_target_path = lambda _path: template_path
        handler._relative_path = lambda path: path.relative_to(self.root).as_posix()
        handler._config_payload = lambda: {}
        handler._send_json = lambda payload, status=HTTPStatus.OK: responses.append(
            (payload, status)
        )

        RoutingMatrixHandler.do_POST(handler)

        self.assertEqual(responses[0][1], HTTPStatus.OK)
        saved = json.loads(template_path.read_text(encoding="utf-8"))["templates"][0]
        self.assertNotIn("rack_mountable", saved)
        self.assertNotIn("location", saved)
        self.assertNotIn("rack_units", saved)
        self.assertNotIn("rack_position", saved)

    def test_staged_multi_file_write_rolls_back_on_replace_failure(self) -> None:
        targets = self.targets("alpha")
        real_replace = os.replace
        replace_count = 0

        def fail_second_replace(source: object, destination: object) -> None:
            nonlocal replace_count
            replace_count += 1
            if replace_count == 2:
                raise OSError("simulated replace failure")
            real_replace(source, destination)

        with patch("routing_matrix_server.os.replace", side_effect=fail_second_replace):
            with self.assertRaisesRegex(OSError, "simulated"):
                write_json_transaction(
                    [
                        (targets["model_path"], {"version": 2}),
                        (targets["connections_path"], {"version": 2}),
                    ]
                )
        self.assertEqual(json.loads(targets["model_path"].read_text()), self.initial_model)
        self.assertEqual(
            json.loads(targets["connections_path"].read_text()), self.initial_connections
        )

    def test_rejects_target_outside_selected_project(self) -> None:
        handler = object.__new__(RoutingMatrixHandler)
        handler.root_path = self.root
        raw = {
            "project_key": "alpha",
            "targets": {
                "model_path": "projects/beta/device-configurations/model.json",
                "connections_path": "projects/alpha/patch-configurations/patch.json",
                "routing_rules_path": "routing-rules.json",
                "route_debug_path": "projects/alpha/outputs/debug/route-debug.json",
                "preview_html": "projects/alpha/outputs/html/wiring.html",
                "preview_svg_dir": "projects/alpha/outputs/svgs",
            },
        }
        with self.assertRaisesRegex(ValueError, "inside selected project"):
            handler._transaction_targets(raw)

    def test_matching_active_targets_refresh_watcher_baseline(self) -> None:
        class Handler(RoutingMatrixHandler):
            pass

        targets = self.targets("alpha")
        Handler.model_path = targets["model_path"]
        Handler.connections_path = targets["connections_path"]
        Handler.routing_rules_path = self.root / "routing-rules.json"
        Handler._watch_baseline_signature = None
        write_json_atomic(targets["model_path"], {"version": 3})
        Handler.refresh_watch_baseline_for_targets(
            model_path=targets["model_path"],
            connections_path=targets["connections_path"],
        )
        self.assertEqual(Handler._watch_baseline_signature, Handler._watch_signature())


if __name__ == "__main__":
    unittest.main()
