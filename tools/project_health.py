#!/usr/bin/env python3
"""Report schema and referential-integrity issues for one Studio Wiring project."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from studio_wiring_schema.health import check_project  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a non-mutating Studio Wiring project health scan")
    parser.add_argument("project_directory", type=Path, help="Directory containing project.json")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    issues = check_project(args.project_directory)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "severity": issue.severity,
                        "path": issue.path,
                        "code": issue.code,
                        "message": issue.message,
                    }
                    for issue in issues
                ],
                indent=2,
                ensure_ascii=False,
            )
        )
    elif issues:
        for issue in issues:
            print(issue.format())
        print(f"Project health found {len(issues)} issue(s).")
    else:
        print("Project health passed: no issues found.")
    return 1 if any(issue.severity == "error" for issue in issues) else 0


if __name__ == "__main__":
    raise SystemExit(main())
