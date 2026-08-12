#!/usr/bin/env python3
"""Run the repository's fast, dependency-free validation suite."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {".git", "__pycache__", "backups"}


def python_sources() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*.py")
        if not any(part in EXCLUDED_PARTS for part in path.relative_to(ROOT).parts)
    )


def check_python_syntax() -> list[str]:
    issues: list[str] = []
    sources = python_sources()
    for path in sources:
        try:
            source = path.read_text(encoding="utf-8")
            compile(source, str(path), "exec")
        except (OSError, SyntaxError, UnicodeError) as exc:
            issues.append(f"{path.relative_to(ROOT)}: {exc}")
    if not issues:
        print(f"PASS Python syntax ({len(sources)} files; no bytecode written)")
    return issues


def run_check(label: str, command: list[str]) -> int:
    completed = subprocess.run(command, cwd=ROOT, text=True)
    if completed.returncode == 0:
        print(f"PASS {label}")
    else:
        print(f"FAIL {label} (exit {completed.returncode})", file=sys.stderr)
    return completed.returncode


def main() -> int:
    syntax_issues = check_python_syntax()
    if syntax_issues:
        print("FAIL Python syntax", file=sys.stderr)
        for issue in syntax_issues:
            print(f"- {issue}", file=sys.stderr)

    generation_result = run_check(
        "fixture-based generator consistency",
        [sys.executable, str(ROOT / "tools" / "check_routing_matrix_generation.py")],
    )
    schema_result = run_check(
        "fixture-based schema, migration, and project-health tests",
        [
            sys.executable,
            "-m",
            "unittest",
            "tests.test_schema_validation",
        ],
    )
    save_transaction_result = run_check(
        "transactional save tests",
        [sys.executable, "-m", "unittest", "tests.test_save_transaction"],
    )
    generated_ui_result = run_check(
        "generated configuration UI checks",
        [sys.executable, "-m", "unittest", "tests.test_generated_ui"],
    )
    architecture_result = run_check(
        "shell and canonical matrix architecture checks",
        [sys.executable, "-m", "unittest", "tests.test_architecture_contract"],
    )
    shell_tab_result = run_check(
        "shell tab routing and embedded panel contracts",
        [
            sys.executable,
            "-m",
            "unittest",
            "tests.test_shell_tab_contract",
            "tests.test_shell_prepaint_contract",
        ],
    )
    prototype_result = run_check(
        "canonical matrix accessibility, history, and save contracts",
        [sys.executable, "-m", "unittest", "tests.test_prototype_contract"],
    )
    rack_ui_result = run_check(
        "rack editor UI and data contracts",
        [sys.executable, "-m", "unittest", "tests.test_rack_ui_contract"],
    )

    if (
        syntax_issues
        or generation_result
        or schema_result
        or save_transaction_result
        or generated_ui_result
        or architecture_result
        or shell_tab_result
        or prototype_result
        or rack_ui_result
    ):
        return 1
    print("All project checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
