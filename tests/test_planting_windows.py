"""Unit + integration tests for scripts/planting_windows.py.

No network, no fixture files: synthetic fixtures built inline. Mirrors
tests/test_refresh.py conventions.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "data" / "_schema" / "planting-window.json"


class SchemaFileTest(unittest.TestCase):
    def test_schema_is_valid_json_with_expected_shape(self):
        d = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(d["type"], "object")
        self.assertEqual(d["additionalProperties"], False)
        self.assertEqual(
            set(d["required"]),
            {
                "stateFips", "stateAlpha", "crop", "plant", "harvest",
                "method", "definition", "sourceYears",
            },
        )
        # The shard must NOT carry schema_version (frozen contract).
        self.assertNotIn("schema_version", d["properties"])
        block = d["$defs"]["window"]
        self.assertEqual(
            set(block["required"]),
            {"begin", "mostActiveStart", "mostActiveEnd", "end"},
        )


if __name__ == "__main__":
    unittest.main()
