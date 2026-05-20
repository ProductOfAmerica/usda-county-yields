"""Unit + integration tests for scripts/planting_windows.py.

No network, no fixture files: synthetic fixtures built inline. Mirrors
tests/test_refresh.py conventions.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import planting_windows as pw  # noqa: E402

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


class ShapeAssertTest(unittest.TestCase):
    def _good(self) -> dict:
        w = {
            "begin": "04-20",
            "mostActiveStart": "04-28",
            "mostActiveEnd": "05-12",
            "end": "05-20",
        }
        return {
            "stateFips": "19",
            "stateAlpha": "IA",
            "crop": "corn",
            "plant": dict(w),
            "harvest": dict(w),
            "method": "nass-crop-progress-percentile",
            "definition": "usual-window",
            "sourceYears": {"from": 2006, "to": 2025},
        }

    def test_good_shard_passes(self):
        pw._assert_planting_window_shape(self._good())  # no raise

    def test_extra_top_key_rejected(self):
        bad = self._good()
        bad["schema_version"] = 2
        with self.assertRaises(SystemExit):
            pw._assert_planting_window_shape(bad)

    def test_missing_block_key_rejected(self):
        bad = self._good()
        del bad["plant"]["end"]
        with self.assertRaises(SystemExit):
            pw._assert_planting_window_shape(bad)

    def test_bad_mmdd_rejected(self):
        bad = self._good()
        bad["plant"]["begin"] = "2024-04-20"
        with self.assertRaises(SystemExit):
            pw._assert_planting_window_shape(bad)


if __name__ == "__main__":
    unittest.main()
