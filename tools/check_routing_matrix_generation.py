#!/usr/bin/env python3
"""Check that routing_matrix.html is up to date with generate_point_to_point.py.

Also enforces a few regression guards for known browser-compat pitfalls.
"""

from __future__ import annotations

import argparse
import difflib
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "generate_point_to_point.py"
MATRIX_FILE = ROOT / "routing_matrix.html"

FORBIDDEN_SNIPPETS = {
    "replaceAll(": "Use regex or split/join for Safari compatibility.",
    "prefix.replace(/[.*+?^${}()|[\\]\\]/g": "Use escapeRegExp helper for regex escaping.",
}

REQUIRED_SNIPPETS = {
    "function escapeRegExp(value)": "Missing escapeRegExp helper.",
    "id=\"copyDebugReportBtn\"": "Missing debug report copy button.",
    "window.__matrixDebug = debugRuntime;": "Missing runtime debug state export.",
}


def run_generator(tmp_matrix: Path, tmp_html: Path, tmp_debug: Path) -> None:
    cmd = [
        sys.executable,
        str(GENERATOR),
        "--output",
        str(tmp_html),
        "--debug-routes-json",
        str(tmp_debug),
        "--matrix-model-url",
        "projects/studio-sidecar/device-configurations/studio-model-001.json",
        "--matrix-connections-url",
        "projects/studio-sidecar/patch-configurations/studio-model-001/patch-default.json",
        "--matrix-output",
        str(tmp_matrix),
    ]
    completed = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(
            "Generator failed\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check generated routing_matrix consistency")
    parser.add_argument(
        "--matrix-file",
        type=Path,
        default=MATRIX_FILE,
        help="Path to committed/generated routing_matrix.html",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    matrix_path = args.matrix_file.resolve()

    if not matrix_path.exists():
        print(f"Matrix file not found: {matrix_path}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="matrix-gen-check-") as td:
        tmp_dir = Path(td)
        tmp_matrix = tmp_dir / "routing_matrix.generated.html"
        tmp_html = tmp_dir / "studio_wiring_point_to_point.generated.html"
        tmp_debug = tmp_dir / "route-debug.generated.json"

        try:
            run_generator(tmp_matrix=tmp_matrix, tmp_html=tmp_html, tmp_debug=tmp_debug)
        except Exception as exc:  # noqa: BLE001
            print(str(exc), file=sys.stderr)
            return 2

        current_text = matrix_path.read_text(encoding="utf-8")
        generated_text = tmp_matrix.read_text(encoding="utf-8")

        issues: list[str] = []
        issues.extend(check_snippets(current_text))
        issues.extend(check_snippets(generated_text))

        diff_text = compare_files(
            expected=current_text,
            actual=generated_text,
            label_expected=str(matrix_path),
            label_actual=str(tmp_matrix),
        )
        if diff_text:
            issues.append("routing_matrix.html is out of date with generator output.")

        if issues:
            print("Check failed:", file=sys.stderr)
            for issue in issues:
                print(f"- {issue}", file=sys.stderr)
            if diff_text:
                print("\nDiff preview:\n", file=sys.stderr)
                print(diff_text, file=sys.stderr)
            return 1

    print("Check passed: routing_matrix.html matches generator output and guardrails.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
