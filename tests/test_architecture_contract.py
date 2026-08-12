from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ArchitectureContractTests(unittest.TestCase):
    def test_shell_routes_matrix_directly_to_canonical_app(self) -> None:
        manifest = json.loads((ROOT / "web/manifests/tabs.json").read_text(encoding="utf-8"))
        routing = next(tab for tab in manifest["tabs"] if tab["key"] == "routing")
        source = str(routing["src"])
        self.assertIn("routing_matrix_prototype_compact.html", source)
        self.assertNotIn("routing_matrix.html", source.replace("routing_matrix_prototype_compact.html", ""))

    def test_canonical_matrix_contains_no_iframe(self) -> None:
        canonical = (ROOT / "prototypes/routing_matrix_prototype_compact.html").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("<iframe", canonical.lower())
        self.assertIn('id="matrixContainer"', canonical)
        self.assertIn('id="projectSelect"', canonical)

    def test_only_routing_manifest_entry_hosts_a_matrix(self) -> None:
        manifest = json.loads((ROOT / "web/manifests/tabs.json").read_text(encoding="utf-8"))
        canonical_entries = [
            tab for tab in manifest["tabs"]
            if "routing_matrix_prototype_compact.html" in str(tab.get("src", ""))
        ]
        self.assertEqual([tab["key"] for tab in canonical_entries], ["routing"])


if __name__ == "__main__":
    unittest.main()
