#!/usr/bin/env python3
"""Exercise the HTML generator against small, immutable test fixtures.

The check deliberately writes every generated artifact to a temporary directory. It
also applies browser-compatibility guardrails to the committed matrix page, but does
not expect that user-facing page to contain the fixture studio snapshot.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "generate_point_to_point.py"
MATRIX_FILE = ROOT / "routing_matrix.html"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "generator"
FIXTURE_MODEL = FIXTURE_DIR / "model.json"
FIXTURE_CONNECTIONS = FIXTURE_DIR / "connections.json"
ROUTING_RULES = ROOT / "json" / "routing_rules.json"

FORBIDDEN_SNIPPETS = {
    "replaceAll(": "Use regex or split/join for Safari compatibility.",
    "prefix.replace(/[.*+?^${}()|[\\]\\]/g": "Use escapeRegExp helper for regex escaping.",
    "window.prompt(": "Use the accessible configuration dialog for naming workflows.",
}

REQUIRED_SNIPPETS = {
    "function escapeRegExp(value)": "Missing escapeRegExp helper.",
    "id=\"copyDebugReportBtn\"": "Missing debug report copy button.",
    "window.__matrixDebug = debugRuntime;": "Missing runtime debug state export.",
    "id=\"configDialog\"": "Missing configuration manager dialog.",
    "aria-labelledby=\"configDialogTitle\"": "Configuration dialog has no accessible name.",
    "function requestConfiguration(options)": "Missing shared configuration dialog controller.",
    "function configDialogFocusableElements()": "Missing configuration dialog focus trap.",
    "configDialog.addEventListener(\"cancel\"": "Missing Escape/cancel handling for configuration dialog.",
    "id=\"configDialogOverwriteCheck\"": "Missing explicit overwrite confirmation control.",
}


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Fixture not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON fixture {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Fixture must contain a JSON object: {path}")
    return payload


def validate_fixtures() -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate the fixture contract before invoking the larger generator."""
    model = load_json_object(FIXTURE_MODEL)
    patch = load_json_object(FIXTURE_CONNECTIONS)

    devices = model.get("devices")
    if not isinstance(devices, list) or not devices:
        raise ValueError("Generator model fixture must contain a non-empty 'devices' array.")
    connections = patch.get("connections")
    if not isinstance(connections, list) or not connections:
        raise ValueError("Generator connection fixture must contain a non-empty 'connections' array.")

    endpoints: dict[tuple[str, str], str] = {}
    for device_index, device in enumerate(devices):
        if not isinstance(device, dict):
            raise ValueError(f"devices[{device_index}] must be an object.")
        device_name = str(device.get("name") or "").strip()
        if not device_name:
            raise ValueError(f"devices[{device_index}].name must be non-empty.")
        ports = device.get("ports")
        if not isinstance(ports, list) or not ports:
            raise ValueError(f"Device {device_name!r} must contain a non-empty 'ports' array.")
        for port_index, port in enumerate(ports):
            if not isinstance(port, dict):
                raise ValueError(f"ports[{port_index}] on {device_name!r} must be an object.")
            port_name = str(port.get("name") or "").strip()
            direction = str(port.get("direction") or "").strip().lower()
            if not port_name:
                raise ValueError(f"ports[{port_index}].name on {device_name!r} must be non-empty.")
            if direction not in {"in", "out", "io"}:
                raise ValueError(
                    f"Port {device_name!r}/{port_name!r} has invalid direction {direction!r}."
                )
            endpoint = (device_name, port_name)
            if endpoint in endpoints:
                raise ValueError(f"Duplicate fixture endpoint: {device_name}/{port_name}")
            endpoints[endpoint] = direction

    cable_ids: set[str] = set()
    for index, connection in enumerate(connections):
        if not isinstance(connection, dict):
            raise ValueError(f"connections[{index}] must be an object.")
        cable_id = str(connection.get("cable_id") or "").strip()
        if not cable_id:
            raise ValueError(f"connections[{index}].cable_id must be non-empty.")
        if cable_id in cable_ids:
            raise ValueError(f"Duplicate fixture cable_id: {cable_id}")
        cable_ids.add(cable_id)

        source = (
            str(connection.get("source_device") or "").strip(),
            str(connection.get("source_port") or "").strip(),
        )
        destination = (
            str(connection.get("dest_device") or "").strip(),
            str(connection.get("dest_port") or "").strip(),
        )
        if source not in endpoints:
            raise ValueError(f"Unknown source endpoint in {cable_id}: {'/'.join(source)}")
        if destination not in endpoints:
            raise ValueError(f"Unknown destination endpoint in {cable_id}: {'/'.join(destination)}")
        if endpoints[source] not in {"out", "io"}:
            raise ValueError(f"Source endpoint is not output-capable in {cable_id}: {'/'.join(source)}")
        if endpoints[destination] not in {"in", "io"}:
            raise ValueError(
                f"Destination endpoint is not input-capable in {cable_id}: {'/'.join(destination)}"
            )

    return model, patch


def run_generator(output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "matrix": output_dir / "routing_matrix.generated.html",
        "html": output_dir / "studio_wiring_point_to_point.generated.html",
        "debug": output_dir / "route-debug.generated.json",
    }
    cmd = [
        sys.executable,
        str(GENERATOR),
        "--model",
        str(FIXTURE_MODEL.relative_to(ROOT)),
        "--connections-json",
        str(FIXTURE_CONNECTIONS.relative_to(ROOT)),
        "--routing-rules",
        str(ROUTING_RULES.relative_to(ROOT)),
        "--output",
        str(artifacts["html"]),
        "--debug-routes-json",
        str(artifacts["debug"]),
        "--matrix-model-url",
        "tests/fixtures/generator/model.json",
        "--matrix-connections-url",
        "tests/fixtures/generator/connections.json",
        "--matrix-output",
        str(artifacts["matrix"]),
    ]
    completed = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(
            "Generator failed\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    for label, path in artifacts.items():
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"Generator did not create a non-empty {label} artifact: {path}")
    return artifacts


def check_snippets(text: str) -> list[str]:
    issues: list[str] = []
    for snippet, message in FORBIDDEN_SNIPPETS.items():
        if snippet in text:
            issues.append(f"Forbidden snippet found: {snippet} ({message})")
    for snippet, message in REQUIRED_SNIPPETS.items():
        if snippet not in text:
            issues.append(f"Required snippet missing: {snippet} ({message})")
    return issues


def compare_files(expected: str, actual: str, label_expected: str, label_actual: str) -> str:
    if expected == actual:
        return ""
    diff_lines = difflib.unified_diff(
        expected.splitlines(),
        actual.splitlines(),
        fromfile=label_expected,
        tofile=label_actual,
        lineterm="",
        n=3,
    )
    preview = []
    for idx, line in enumerate(diff_lines):
        preview.append(line)
        if idx >= 159:
            preview.append("... diff truncated ...")
            break
    return "\n".join(preview)


def normalize_matrix_structure(text: str) -> str:
    """Remove only generator inputs while retaining all application markup/code."""
    normalized = text.replace("\r\n", "\n")
    substitutions = (
        (
            r"(?m)^(\s*<title>).*?( \| Wiring Matrix</title>)$",
            r"\1__NORMALIZED_TITLE__\2",
            "document title",
        ),
        (
            r"(?m)^(\s*<h1>).*?( \| Wiring Matrix</h1>)$",
            r"\1__NORMALIZED_TITLE__\2",
            "page heading",
        ),
        (
            r"(?m)^(\s*<div class=\"meta\">Generated: ).*?( \| Click a cell to connect/disconnect\.</div>)$",
            r"\1__NORMALIZED_DATE__\2",
            "generated date",
        ),
        (
            r"(?m)^\s*const EMBEDDED_MODEL = .*;$",
            "    const EMBEDDED_MODEL = __NORMALIZED_MODEL__;",
            "embedded model",
        ),
        (
            r"(?m)^\s*const EMPTY_MODEL_TEMPLATE = .*;$",
            "    const EMPTY_MODEL_TEMPLATE = __NORMALIZED_EMPTY_MODEL__;",
            "empty model template",
        ),
        (
            r"(?m)^\s*const EMBEDDED_MATRIX = .*;$",
            "    const EMBEDDED_MATRIX = __NORMALIZED_MATRIX__;",
            "embedded matrix",
        ),
    )
    for pattern, replacement, label in substitutions:
        normalized, count = re.subn(pattern, replacement, normalized)
        if count != 1:
            raise ValueError(
                f"Could not normalize {label}: expected exactly one match, found {count}."
            )
    return normalized.rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check fixture-based HTML generation consistency")
    parser.add_argument(
        "--matrix-file",
        type=Path,
        default=MATRIX_FILE,
        help="Committed matrix page to scan for browser guardrails (not overwritten)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    matrix_path = args.matrix_file.resolve()

    if not matrix_path.exists():
        print(f"Matrix file not found: {matrix_path}", file=sys.stderr)
        return 2

    try:
        fixture_model, fixture_patch = validate_fixtures()
    except ValueError as exc:
        print(f"Fixture validation failed: {exc}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="matrix-gen-check-") as td:
        tmp_dir = Path(td)

        try:
            first = run_generator(tmp_dir / "first")
            second = run_generator(tmp_dir / "second")
        except Exception as exc:  # noqa: BLE001
            print(str(exc), file=sys.stderr)
            return 2

        current_text = matrix_path.read_text(encoding="utf-8")
        generated_text = first["matrix"].read_text(encoding="utf-8")

        issues: list[str] = []
        issues.extend(check_snippets(current_text))
        issues.extend(check_snippets(generated_text))
        diff_text = ""
        for artifact_name in ("matrix", "html", "debug"):
            first_text = first[artifact_name].read_text(encoding="utf-8")
            second_text = second[artifact_name].read_text(encoding="utf-8")
            artifact_diff = compare_files(
                expected=first_text,
                actual=second_text,
                label_expected=f"first/{first[artifact_name].name}",
                label_actual=f"second/{second[artifact_name].name}",
            )
            if artifact_diff:
                issues.append(f"Generator output is not deterministic for {artifact_name}.")
                if not diff_text:
                    diff_text = artifact_diff

        try:
            committed_structure = normalize_matrix_structure(current_text)
            generated_structure = normalize_matrix_structure(generated_text)
            structure_diff = compare_files(
                expected=generated_structure,
                actual=committed_structure,
                label_expected="fixture-generated/routing_matrix.html (normalized)",
                label_actual=f"{matrix_path} (normalized)",
            )
            if structure_diff:
                issues.append(
                    "Committed routing_matrix.html application structure is stale relative to "
                    "generate_point_to_point.py. Regenerate it; embedded studio data may differ."
                )
                if not diff_text:
                    diff_text = structure_diff
        except ValueError as exc:
            issues.append(str(exc))

        expected_strings = [
            str(fixture_model["title"]),
            str(fixture_model["devices"][0]["name"]),
            str(fixture_patch["connections"][0]["cable_id"]),
        ]
        for expected in expected_strings:
            if expected not in generated_text:
                issues.append(f"Generated matrix is missing fixture value: {expected!r}")

        try:
            debug_payload = load_json_object(first["debug"])
            debug_layers = debug_payload.get("layers")
            if not isinstance(debug_layers, dict) or not debug_layers:
                issues.append("Generated route debug JSON has no layers.")
        except ValueError as exc:
            issues.append(str(exc))

        if issues:
            print("Check failed:", file=sys.stderr)
            for issue in issues:
                print(f"- {issue}", file=sys.stderr)
            if diff_text:
                print("\nDiff preview:\n", file=sys.stderr)
                print(diff_text, file=sys.stderr)
            return 1

    print(
        "Check passed: fixtures are valid, generator output is deterministic, "
        "committed matrix structure matches the generator, and guardrails are present."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
