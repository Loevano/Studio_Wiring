"""Observable contracts for the separate logical audio Routing Matrix.

These tests keep logical audio routing separate from the physical Wiring Matrix:
they exercise only temporary project data and never open or write a user project.
"""

from __future__ import annotations

import json
import io
import re
import tempfile
import unittest
import zipfile
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

from routing_matrix_server import (
    RoutingMatrixHandler,
    build_svg_archive,
    canonical_json_hash,
    collect_svg_files,
    dated_svg_filename,
    routing_endpoints_from_model,
    svg_folder_name,
)
from studio_wiring_schema.validation import validate_document, validate_routing_against_model


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "web/manifests/tabs.json"
ROUTING_PAGE = ROOT / "web/routing-matrix/index.html"
ROUTING_APP = ROOT / "web/routing-matrix/app.js"
GENERATOR = ROOT / "generate_point_to_point.py"
GENERATED_APP = ROOT / "routing_matrix.html"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def routing_model() -> dict[str, object]:
    return {
        "version": 1,
        "title": "Routing fixture",
        "devices": [
            {
                "name": "Audient",
                "ports": [],
                "routing_ports": [
                    {"name": "ADAT 1", "direction": "out", "channel": 1},
                    {"name": "ADAT 2", "direction": "out", "channel": 2},
                ],
            },
            {
                "name": "SSL",
                "ports": [],
                "routing_ports": [
                    {"name": "ADAT 1", "direction": "in", "channel": 1},
                    {"name": "ADAT 2", "direction": "in", "channel": 2},
                    {"name": "MADI 25", "direction": "out", "channel": 25},
                    {"name": "MADI 26", "direction": "out", "channel": 26},
                ],
            },
            {
                "name": "UFX",
                "ports": [],
                "routing_ports": [
                    {"name": "MADI 25", "direction": "in", "channel": 25},
                    {"name": "MADI 26", "direction": "in", "channel": 26},
                ],
            },
        ],
    }


def route_document() -> dict[str, object]:
    return {
        "version": 1,
        "routes": [
            {
                "source_device": "Audient",
                "source_port": "ADAT 1",
                "dest_device": "SSL",
                "dest_port": "ADAT 1",
            },
            {
                "source_device": "SSL",
                "source_port": "ADAT 1",
                "dest_device": "SSL",
                "dest_port": "MADI 25",
            },
            {
                "source_device": "SSL",
                "source_port": "MADI 25",
                "dest_device": "UFX",
                "dest_port": "MADI 25",
            },
        ],
    }


class RoutingMatrixShellAndUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(_read(MANIFEST))
        cls.html = _read(ROUTING_PAGE)

    def test_shell_places_separate_routing_matrix_immediately_after_wiring_matrix(self) -> None:
        tabs = self.manifest["tabs"]
        wiring_index = next(index for index, tab in enumerate(tabs) if tab["key"] == "routing")
        self.assertEqual(tabs[wiring_index]["label"], "Wiring Matrix")
        routing = tabs[wiring_index + 1]
        self.assertEqual(
            routing,
            {
                "key": "audio-routing",
                "label": "Routing Matrix",
                "src": "../routing-matrix/index.html",
            },
        )
        self.assertNotEqual(routing["src"], tabs[wiring_index]["src"])

    def test_routing_page_exposes_a_labeled_keyboard_operable_matrix(self) -> None:
        self.assertIn('id="routingMatrixApp"', self.html)
        self.assertIn('data-routing-matrix-version="1"', self.html)
        self.assertIn('id="routingMatrixTable"', self.html)
        self.assertIn("Sources are rows and destinations are columns", self.html)
        self.assertRegex(self.html, r'<div id="routingStatus"[^>]*role="status"[^>]*aria-live="polite"')
        self.assertIn('id="routingEmptyState"', self.html)
        self.assertIn('id="matrixScroller"', self.html)
        self.assertRegex(self.html, r'id="matrixScroller"[^>]*tabindex="0"')
        for control in (
            "routeModeSelect",
            "routeSpanSelect",
            "undoButton",
            "redoButton",
            "reloadButton",
            "saveButton",
        ):
            self.assertEqual(self.html.count(f'id="{control}"'), 1, control)
        self.assertRegex(self.html, r'<label for="routeModeSelect">Action</label>')
        self.assertRegex(self.html, r'<label for="routeSpanSelect">Channels</label>')
        self.assertRegex(self.html, r'<option value="8"(?: selected)?>8</option>')
        self.assertRegex(self.html, r'<option value="all">Bundle</option>')

    def test_routing_page_inherits_selection_without_duplicate_config_selectors(self) -> None:
        app = _read(ROUTING_APP)
        for duplicate_control in ("projectSelect", "modelSelect", "routingConfigSelect"):
            self.assertNotIn(f'id="{duplicate_control}"', self.html)
            self.assertNotIn(f'getElementById("{duplicate_control}")', app)

        self.assertIn('id="routingContext"', self.html)
        self.assertRegex(
            self.html,
            r'id="routingContext"[^>]*(?:role="status"|aria-live="polite")',
            "the inherited project/model/routing context must be announced",
        )
        self.assertRegex(
            self.html,
            r'id="routingContext"[^>]*>[^<]+',
            "the selection context needs visible fallback text while loading",
        )

        self.assertIn('studioWiringProjectSelectionV1', app)
        self.assertIn("window.localStorage.getItem", app)
        for shared_field in ("project_key", "model_path", "connections_path"):
            self.assertIn(shared_field, app)
        self.assertIn("device_routing_map", app)
        self.assertIn("default_routing_config", app)
        self.assertIn("routing_configs", app)

    def test_routing_app_contract_has_no_physical_patch_save_or_regeneration_path(self) -> None:
        app = _read(ROUTING_APP)
        self.assertIn('"/api/routing"', app)
        self.assertIn('"/api/save-routing"', app)
        self.assertNotIn("/api/save-transaction", app)
        self.assertNotIn("/api/regenerate", app)
        self.assertIn("aria-pressed", app)
        self.assertIn("routeSpanSelect", app)
        self.assertIn("routingEmptyState", app)
        self.assertIn("routingStatus", app)

    def test_direction_only_endpoints_are_available_to_the_matrix_axes(self) -> None:
        """The API deliberately supplies direction, not separate source/destination roles."""
        app = _read(ROUTING_APP)
        self.assertIn("const explicitRoles = normalizeRoles", app)
        self.assertIn('explicitRoles.length || !direction', app)
        self.assertIn('["source", "destination"]', app)
        self.assertIn("source.direction === \"in\" || source.direction === \"io\"", app)
        self.assertIn("destination.direction === \"out\" || destination.direction === \"io\"", app)

    def test_routing_app_supports_shell_theme_and_autosave_flush_protocol(self) -> None:
        app = _read(ROUTING_APP)
        for message in (
            "studio-theme-set",
            "studio-theme-request",
            "studio-shell-autosave-set",
            "studio-shell-autosave-request",
            "studio-shell-autosave-flush",
            "studio-shell-autosave-state",
            "studio-shell-autosave-changed",
            "studio-shell-autosave-flushed",
        ):
            self.assertIn(message, app)
        self.assertRegex(
            app,
            r"saveRoutes\(\{ quiet: true \}\)\.then\(\(ok\) =>",
            "flush must wait for the save promise before it reports completion",
        )
        self.assertRegex(
            app,
            r"ok\s*:\s*Boolean\(ok\)",
            "failed/conflicted saves must propagate false to the shell flush response",
        )


class RoutingSchemaTests(unittest.TestCase):
    def test_hidden_devices_are_excluded_from_logical_matrix_endpoints(self) -> None:
        model = routing_model()
        devices = model["devices"]
        assert isinstance(devices, list)
        devices[0]["hidden"] = True
        devices[1]["visible"] = False

        endpoints = routing_endpoints_from_model(model)

        self.assertEqual({endpoint["device"] for endpoint in endpoints}, {"UFX"})

    def test_routing_matrix_visibility_overrides_legacy_device_visibility(self) -> None:
        model = routing_model()
        devices = model["devices"]
        assert isinstance(devices, list)
        devices[0]["hidden"] = True
        devices[0]["visibility"] = {"routing_matrix": True, "wiring_matrix": False}
        devices[1]["visibility"] = {"routing_matrix": False}

        endpoints = routing_endpoints_from_model(model)

        self.assertEqual({endpoint["device"] for endpoint in endpoints}, {"Audient", "UFX"})

    def test_logical_matrix_preserves_named_channel_labels(self) -> None:
        app = _read(ROUTING_APP)
        self.assertIn("Number.isFinite(Number(rawChannel)) ? Number(rawChannel) : text(rawChannel)", app)
        endpoint = routing_endpoints_from_model(
            {
                "devices": [
                    {
                        "name": "UFX",
                        "routing_ports": [
                            {"name": "Main L", "direction": "in", "channel": "USB 1"}
                        ],
                    }
                ]
            }
        )[0]
        self.assertEqual(endpoint["channel"], "USB 1")

    def test_logical_matrix_exposes_roles_and_input_metadata(self) -> None:
        app = _read(ROUTING_APP)
        self.assertIn("hardware: text(raw?.hardware)", app)
        self.assertIn("connection2: text(raw?.connection_2)", app)
        endpoint = routing_endpoints_from_model(
            {
                "devices": [
                    {
                        "name": "UFX",
                        "routing_ports": [
                            {
                                "name": "Input: ASP880 1",
                                "direction": "in",
                                "routing_roles": ["source"],
                                "channel": "UFX ADAT 1",
                                "hardware": "ASP 1",
                                "connection_2": "ASP ADAT 1",
                            }
                        ],
                    }
                ]
            }
        )[0]
        self.assertEqual(endpoint["routing_roles"], ["source"])
        self.assertEqual(endpoint["hardware"], "ASP 1")
        self.assertEqual(endpoint["connection_2"], "ASP ADAT 1")

    def test_three_hop_route_and_same_device_input_to_output_are_valid(self) -> None:
        model = routing_model()
        routes = route_document()
        self.assertEqual(validate_document("routing", routes), [])
        self.assertEqual(validate_routing_against_model(routes, model), [])

    def test_destination_is_unique_but_source_can_fan_out(self) -> None:
        model = routing_model()
        routes = route_document()
        rows = routes["routes"]
        assert isinstance(rows, list)
        rows.append(
            {
                "source_device": "Audient",
                "source_port": "ADAT 1",
                "dest_device": "SSL",
                "dest_port": "ADAT 2",
            }
        )
        self.assertEqual(validate_routing_against_model(routes, model), [])
        rows.append(
            {
                "source_device": "Audient",
                "source_port": "ADAT 2",
                "dest_device": "SSL",
                "dest_port": "ADAT 2",
            }
        )
        codes = {issue.code for issue in validate_routing_against_model(routes, model)}
        self.assertIn("routing.destination_duplicate", codes)

    def test_invalid_endpoint_and_direction_are_rejected_without_rewriting_model(self) -> None:
        model = routing_model()
        before = json.dumps(model, sort_keys=True)
        invalid = {
            "version": 1,
            "routes": [
                {
                    "source_device": "Audient",
                    "source_port": "Missing",
                    "dest_device": "SSL",
                    "dest_port": "ADAT 1",
                },
                {
                    "source_device": "SSL",
                    "source_port": "MADI 25",
                    "dest_device": "SSL",
                    "dest_port": "ADAT 1",
                },
            ],
        }
        codes = {issue.code for issue in validate_routing_against_model(invalid, model)}
        self.assertIn("routing.endpoint_missing", codes)
        self.assertIn("routing.source_direction", codes)
        self.assertEqual(json.dumps(model, sort_keys=True), before)


class RoutingSaveEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="routing-save-contract-")
        self.root = Path(self.temp_dir.name).resolve()
        self.project = self.root / "projects" / "alpha"
        self.model_path = self.project / "device-configurations" / "model.json"
        self.patch_path = self.project / "patch-configurations" / "model" / "physical.json"
        self.routing_path = self.project / "routing-configurations" / "model" / "route-default.json"
        self.model_path.parent.mkdir(parents=True)
        self.patch_path.parent.mkdir(parents=True)
        self.routing_path.parent.mkdir(parents=True)
        self.model = routing_model()
        self.routes = route_document()
        self.physical_patch = {"version": 1, "connections": []}
        self.model_path.write_text(json.dumps(self.model), encoding="utf-8")
        self.patch_path.write_text(json.dumps(self.physical_patch), encoding="utf-8")
        self.routing_path.write_text(json.dumps({"version": 1, "routes": []}), encoding="utf-8")
        (self.project / "project.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "name": "Alpha",
                    "paths": {
                        "device_model": "device-configurations/model.json",
                        "default_patch": "patch-configurations/model/physical.json",
                        "patch_directory": "patch-configurations",
                        "default_routing": "routing-configurations/model/route-default.json",
                    },
                    "device_patch_map": {
                        "device-configurations/model.json": [
                            "patch-configurations/model/physical.json"
                        ]
                    },
                    "device_routing_map": {
                        "device-configurations/model.json": [
                            "routing-configurations/model/route-default.json"
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _handler(self, path: str, payload: dict[str, object] | None = None):
        class Handler(RoutingMatrixHandler):
            regenerate_calls = 0

            @classmethod
            def trigger_regenerate(cls, **_kwargs: object) -> tuple[bool, str]:
                cls.regenerate_calls += 1
                return True, "unexpected"

        Handler.root_path = self.root
        Handler.model_path = self.model_path
        Handler.connections_path = self.patch_path
        Handler.routing_rules_path = self.root / "json/routing_rules.json"
        Handler.route_debug_path = self.project / "outputs/debug/routes.json"
        Handler.preview_html_path = self.project / "outputs/html/preview.html"
        Handler.preview_svg_dir = self.project / "outputs/svgs"
        Handler.generator_script = ROOT / "generate_point_to_point.py"
        Handler.targets_selected = True
        responses: list[tuple[dict[str, object], HTTPStatus]] = []
        handler = object.__new__(Handler)
        handler.path = path
        handler._read_json_body = lambda: payload
        handler._send_json = lambda body, status=HTTPStatus.OK: responses.append((body, status))
        return Handler, handler, responses

    def _payload(self, **overrides: object) -> dict[str, object]:
        empty = {"version": 1, "routes": []}
        payload: dict[str, object] = {
            "project_key": "alpha",
            "model_path": "projects/alpha/device-configurations/model.json",
            "routing_path": "projects/alpha/routing-configurations/model/route-default.json",
            "routes": self.routes,
            "expected_hash": canonical_json_hash(empty),
        }
        payload.update(overrides)
        return payload

    def test_save_and_read_routing_are_project_scoped_and_preserve_physical_patch(self) -> None:
        before_patch = self.patch_path.read_bytes()
        Handler, handler, responses = self._handler("/api/save-routing", self._payload())
        Handler.do_POST(handler)
        self.assertEqual(responses[0][1], HTTPStatus.OK)
        saved = json.loads(self.routing_path.read_text(encoding="utf-8"))
        self.assertEqual(saved, self.routes)
        self.assertEqual(self.patch_path.read_bytes(), before_patch)
        self.assertEqual(Handler.regenerate_calls, 0)

        _Handler, handler, responses = self._handler(
            "/api/routing?project_key=alpha&model_path=projects/alpha/device-configurations/model.json&routing_path=projects/alpha/routing-configurations/model/route-default.json"
        )
        _Handler.do_GET(handler)
        self.assertEqual(responses[0][1], HTTPStatus.OK)
        self.assertEqual(responses[0][0]["routes"], self.routes)
        self.assertIn("endpoints", responses[0][0])

    def test_svg_files_endpoint_returns_a_named_folder_and_current_svg_contents(self) -> None:
        svg_dir = self.project / "outputs" / "svgs"
        svg_dir.mkdir(parents=True)
        (svg_dir / "audio.svg").write_text("<svg id='audio'/>", encoding="utf-8")
        (svg_dir / "notes.txt").write_text("not exported", encoding="utf-8")

        _Handler, handler, responses = self._handler("/api/svg-files?project_key=alpha")
        _Handler.do_GET(handler)

        payload, status = responses[0]
        self.assertEqual(status, HTTPStatus.OK)
        self.assertTrue(payload["ok"])
        self.assertRegex(payload["folder_name"], r"^Alpha-\d{4}-\d{2}-\d{2}-svgs$")
        self.assertEqual(payload["files"], [{"name": "audio.svg", "content": "<svg id='audio'/>"}])

    def test_svg_archive_endpoint_downloads_a_zip_for_the_selected_project(self) -> None:
        svg_dir = self.project / "outputs" / "svgs"
        svg_dir.mkdir(parents=True)
        (svg_dir / "audio.svg").write_text("<svg id='audio'/>", encoding="utf-8")

        _Handler, handler, _responses = self._handler("/api/svg-archive?project_key=alpha")
        downloads: list[tuple[bytes, str, str]] = []
        handler._send_bytes = lambda body, *, content_type, filename, status=HTTPStatus.OK: downloads.append(
            (body, content_type, filename)
        )
        with patch("routing_matrix_server.time.strftime", return_value="2026-08-22"):
            _Handler.do_GET(handler)

        body, content_type, filename = downloads[0]
        self.assertEqual(content_type, "application/zip")
        self.assertEqual(filename, "Alpha-2026-08-22-svgs.zip")
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertEqual(
                archive.namelist(),
                ["Alpha-2026-08-22-svgs/audio-2026-08-22.svg"],
            )
            self.assertEqual(
                archive.read(archive.namelist()[0]).decode("utf-8"),
                "<svg id='audio'/>",
            )

    def test_save_rejects_invalid_topology_stale_writes_and_paths_without_touching_files(self) -> None:
        original_routes = self.routing_path.read_bytes()
        original_patch = self.patch_path.read_bytes()
        invalid = route_document()
        rows = invalid["routes"]
        assert isinstance(rows, list)
        rows[1]["dest_port"] = "ADAT 1"  # same-device output -> input is invalid
        for payload, status in (
            (self._payload(routes=invalid), HTTPStatus.UNPROCESSABLE_ENTITY),
            (self._payload(expected_hash="stale"), HTTPStatus.CONFLICT),
            (
                self._payload(
                    routing_path="projects/elsewhere/routing-configurations/model/route.json"
                ),
                HTTPStatus.BAD_REQUEST,
            ),
        ):
            Handler, handler, responses = self._handler("/api/save-routing", payload)
            Handler.do_POST(handler)
            self.assertEqual(responses[0][1], status)
            self.assertFalse(responses[0][0]["ok"])
            self.assertEqual(self.routing_path.read_bytes(), original_routes)
            self.assertEqual(self.patch_path.read_bytes(), original_patch)
            self.assertEqual(Handler.regenerate_calls, 0)


class PowerVisualPreviewContractTests(unittest.TestCase):
    def test_power_is_a_first_class_visual_preview_in_source_and_generated_app(self) -> None:
        for path in (GENERATOR, GENERATED_APP):
            source = _read(path)
            with self.subTest(path=path.name):
                self.assertRegex(
                    source,
                    r'\{ key: "power", label: "Power", file: "power\.svg" \}',
                )
                self.assertIn('id="previewPower"', source)
                self.assertIn('id="previewLinkPower"', source)
                self.assertIn('aria-label="Power preview"', source)

    def test_server_config_and_transaction_responses_expose_power_svg_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="power-preview-contract-") as temp_dir:
            root = Path(temp_dir).resolve()
            handler = object.__new__(RoutingMatrixHandler)
            handler.root_path = root
            handler.preview_svg_dir = root / "projects/fixture/outputs/svgs"

            expected = "projects/fixture/outputs/svgs/power.svg"
            self.assertEqual(handler._preview_paths_payload()["power"], expected)
            transaction = handler._transaction_preview_payload(
                {
                    "preview_svg_dir": handler.preview_svg_dir,
                    "preview_html_path": root / "projects/fixture/outputs/html/preview.html",
                    "route_debug_path": root / "projects/fixture/outputs/debug/routes.json",
                }
            )
            self.assertEqual(transaction["preview_paths"]["power"], expected)

    def test_server_regeneration_requests_the_power_layer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="power-regenerate-contract-") as temp_dir:
            root = Path(temp_dir).resolve()
            model = root / "projects/fixture/device-configurations/model.json"
            patch_path = root / "projects/fixture/patch-configurations/model/patch.json"
            rules = root / "json/routing_rules.json"
            debug = root / "projects/fixture/outputs/debug/routes.json"
            html = root / "projects/fixture/outputs/html/preview.html"
            svgs = root / "projects/fixture/outputs/svgs"
            generator = root / "generate_point_to_point.py"
            for path in (model, patch_path, rules, generator):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")

            class Handler(RoutingMatrixHandler):
                pass

            Handler.root_path = root
            Handler.generator_script = generator
            with patch("routing_matrix_server.subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = "ok"
                run.return_value.stderr = ""
                ok, _message = Handler._run_regenerate_command_for_targets(
                    model_path=model,
                    connections_path=patch_path,
                    routing_rules_path=rules,
                    route_debug_path=debug,
                    preview_html_path=html,
                    preview_svg_dir=svgs,
                )

            self.assertTrue(ok)
            command = run.call_args.args[0]
            self.assertIn("--show-power", command)


class AllAudioVisualPreviewContractTests(unittest.TestCase):
    def test_all_audio_is_a_first_class_visual_preview_in_source_and_generated_app(self) -> None:
        for path in (GENERATOR, GENERATED_APP):
            source = _read(path)
            with self.subTest(path=path.name):
                self.assertRegex(
                    source,
                    r'\{ key: "allAudio", label: "All Audio", file: "all-audio\.svg" \}',
                )
                self.assertIn('id="previewAllAudio"', source)
                self.assertIn('id="previewLinkAllAudio"', source)
                self.assertIn('aria-label="All Audio preview"', source)

    def test_server_responses_expose_all_audio_svg_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="all-audio-preview-contract-") as temp_dir:
            root = Path(temp_dir).resolve()
            handler = object.__new__(RoutingMatrixHandler)
            handler.root_path = root
            handler.preview_svg_dir = root / "projects/fixture/outputs/svgs"

            expected = "projects/fixture/outputs/svgs/all-audio.svg"
            self.assertEqual(handler._preview_paths_payload()["allAudio"], expected)
            transaction = handler._transaction_preview_payload(
                {
                    "preview_svg_dir": handler.preview_svg_dir,
                    "preview_html_path": root / "projects/fixture/outputs/html/preview.html",
                    "route_debug_path": root / "projects/fixture/outputs/debug/routes.json",
                }
            )
            self.assertEqual(transaction["preview_paths"]["allAudio"], expected)


class SvgFolderContractTests(unittest.TestCase):
    def test_payload_contains_every_svg_and_excludes_other_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="svg-folder-contract-") as temp_dir:
            svg_dir = Path(temp_dir)
            (svg_dir / "audio.svg").write_text("<svg id='audio'/>", encoding="utf-8")
            (svg_dir / "power.svg").write_text("<svg id='power'/>", encoding="utf-8")
            (svg_dir / "notes.txt").write_text("not an svg", encoding="utf-8")

            files = collect_svg_files(svg_dir)

            self.assertEqual([file["name"] for file in files], ["audio.svg", "power.svg"])
            self.assertIn("id='power'", files[1]["content"])

    def test_folder_name_uses_project_name_and_date(self) -> None:
        with patch("routing_matrix_server.time.strftime", return_value="2026-08-22"):
            self.assertEqual(
                svg_folder_name("Studio Sidecar", "studio-sidecar"),
                "Studio-Sidecar-2026-08-22-svgs",
            )

    def test_archive_dates_folder_and_every_svg_filename(self) -> None:
        files = [
            {"name": "audio-analog.svg", "content": "<svg>2026-08-22</svg>"},
            {"name": "power.svg", "content": "<svg>2026-08-22</svg>"},
        ]
        archive_bytes = build_svg_archive(
            files,
            "Studio-Sidecar-2026-08-22-svgs",
            "2026-08-22",
        )

        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            self.assertEqual(
                archive.namelist(),
                [
                    "Studio-Sidecar-2026-08-22-svgs/audio-analog-2026-08-22.svg",
                    "Studio-Sidecar-2026-08-22-svgs/power-2026-08-22.svg",
                ],
            )

    def test_dated_svg_filename_does_not_duplicate_an_existing_date(self) -> None:
        self.assertEqual(
            dated_svg_filename("power-2026-08-22.svg", "2026-08-22"),
            "power-2026-08-22.svg",
        )


if __name__ == "__main__":
    unittest.main()
