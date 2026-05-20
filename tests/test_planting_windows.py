"""Unit + integration tests for scripts/planting_windows.py.

No network, no fixture files: synthetic fixtures built inline. Mirrors
tests/test_refresh.py conventions.
"""
from __future__ import annotations

import csv
import io
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


def _pw_header() -> list[str]:
    # Same 39-col NASS shape as tests/test_refresh.py.
    return [
        "SOURCE_DESC", "SECTOR_DESC", "GROUP_DESC", "COMMODITY_DESC", "CLASS_DESC",
        "PRODN_PRACTICE_DESC", "UTIL_PRACTICE_DESC", "STATISTICCAT_DESC", "UNIT_DESC",
        "SHORT_DESC", "DOMAIN_DESC", "DOMAINCAT_DESC", "AGG_LEVEL_DESC", "STATE_ANSI",
        "STATE_FIPS_CODE", "STATE_ALPHA", "STATE_NAME", "ASD_CODE", "ASD_DESC",
        "COUNTY_ANSI", "COUNTY_CODE", "COUNTY_NAME", "REGION_DESC", "ZIP_5",
        "WATERSHED_CODE", "WATERSHED_DESC", "CONGR_DISTRICT_CODE", "COUNTRY_CODE",
        "COUNTRY_NAME", "LOCATION_DESC", "YEAR", "FREQ_DESC", "BEGIN_CODE", "END_CODE",
        "REFERENCE_PERIOD_DESC", "WEEK_ENDING", "LOAD_TIME", "VALUE", "CV_%",
    ]


_PWIDX = {n: i for i, n in enumerate(_pw_header())}


def _pw_row(**ov) -> list[str]:
    row = [""] * len(_pw_header())
    d = {
        "SOURCE_DESC": "SURVEY",
        "SECTOR_DESC": "CROPS",
        "GROUP_DESC": "FIELD CROPS",
        "COMMODITY_DESC": "CORN",
        "CLASS_DESC": "ALL CLASSES",
        "STATISTICCAT_DESC": "PROGRESS",
        "UNIT_DESC": "PCT PLANTED",
        "AGG_LEVEL_DESC": "STATE",
        "STATE_ANSI": "19",
        "STATE_FIPS_CODE": "19",
        "STATE_ALPHA": "IA",
        "STATE_NAME": "IOWA",
        "YEAR": "2025",
        "FREQ_DESC": "WEEKLY",
        "REFERENCE_PERIOD_DESC": "WEEK #18",
        "WEEK_ENDING": "2025-05-04",
        "VALUE": "50",
    }
    d.update(ov)
    for k, v in d.items():
        row[_PWIDX[k]] = v
    return row


class FilterProgressTest(unittest.TestCase):
    def _read(self, rows):
        buf = io.StringIO()
        w = csv.writer(buf, delimiter="\t")
        w.writerow(_pw_header())
        for r in rows:
            w.writerow(r)
        buf.seek(0)
        return pw.filter_progress(csv.reader(buf, delimiter="\t"))

    def test_missing_required_column_aborts(self):
        bad = [c for c in _pw_header() if c != "WEEK_ENDING"]
        buf = io.StringIO()
        buf.write("\t".join(bad) + "\n")
        buf.seek(0)
        with self.assertRaises(SystemExit) as ctx:
            pw.filter_progress(csv.reader(buf, delimiter="\t"))
        self.assertIn("WEEK_ENDING", str(ctx.exception))

    def test_keeps_only_state_survey_progress_allowlisted(self):
        rows = [
            _pw_row(),  # keep: IA corn pct planted
            _pw_row(UNIT_DESC="PCT HARVESTED"),  # keep: harvest
            _pw_row(COMMODITY_DESC="WHEAT", CLASS_DESC="WINTER"),  # keep
            _pw_row(COMMODITY_DESC="WHEAT", CLASS_DESC="SPRING"),  # keep
            _pw_row(SOURCE_DESC="CENSUS"),  # drop
            _pw_row(AGG_LEVEL_DESC="COUNTY"),  # drop
            _pw_row(STATISTICCAT_DESC="YIELD"),  # drop
            _pw_row(UNIT_DESC="PCT EMERGED"),  # drop
            _pw_row(COMMODITY_DESC="COTTON"),  # drop
            _pw_row(COMMODITY_DESC="WHEAT", CLASS_DESC="DURUM"),  # drop
        ]
        total, kept = self._read(rows)
        self.assertEqual(total, 10)
        slugs = sorted({k["crop_slug"] for k in kept})
        self.assertEqual(slugs, ["corn", "spring-wheat", "winter-wheat"])
        ops = sorted({k["op"] for k in kept})
        self.assertEqual(ops, ["harvest", "plant"])

    def test_parse_pct(self):
        self.assertEqual(pw.parse_pct("0"), 0.0)
        self.assertEqual(pw.parse_pct(" 88 "), 88.0)
        self.assertEqual(pw.parse_pct("1,000"), 1000.0)
        self.assertIsNone(pw.parse_pct("(D)"))
        self.assertIsNone(pw.parse_pct(""))
        self.assertIsNone(pw.parse_pct("NA"))


class GroupProgressTest(unittest.TestCase):
    def test_groups_by_state_slug_op_year_last_write_wins(self):
        kept = [
            {
                "crop_slug": "corn", "op": "plant", "state_fips": "19",
                "state_alpha": "IA", "state_name": "IOWA", "year": 2025,
                "week_ending": "2025-04-06", "value": "0",
            },
            {
                "crop_slug": "corn", "op": "plant", "state_fips": "19",
                "state_alpha": "IA", "state_name": "IOWA", "year": 2025,
                "week_ending": "2025-04-13", "value": "5",
            },
            # duplicate WEEK_ENDING -> last row wins (revised value 7)
            {
                "crop_slug": "corn", "op": "plant", "state_fips": "19",
                "state_alpha": "IA", "state_name": "IOWA", "year": 2025,
                "week_ending": "2025-04-13", "value": "7",
            },
            # suppressed value is dropped
            {
                "crop_slug": "corn", "op": "plant", "state_fips": "19",
                "state_alpha": "IA", "state_name": "IOWA", "year": 2025,
                "week_ending": "2025-04-20", "value": "(D)",
            },
        ]
        g = pw.group_progress(kept)
        series = g[("19", "corn")]["plant"][2025]["readings"]
        self.assertEqual(series, [("2025-04-06", 0.0), ("2025-04-13", 7.0)])
        self.assertEqual(g[("19", "corn")]["state_alpha"], "IA")


class DayOrdinalTest(unittest.TestCase):
    def test_plain_crops_use_day_of_year_for_plant_and_harvest(self):
        self.assertEqual(pw.day_ordinal("corn", "plant", 2025, "2025-05-04"), 124)
        self.assertEqual(pw.day_ordinal("soybeans", "harvest", 2024, "2024-10-06"), 280)
        self.assertEqual(pw.day_ordinal("corn", "harvest", 2025, "2025-11-15"), 319)
        self.assertEqual(pw.day_ordinal("spring-wheat", "harvest", 2024, "2024-09-30"), 274)

    def test_winter_wheat_plant_anchored_aug1_prev_year_no_wrap(self):
        self.assertEqual(pw.day_ordinal("winter-wheat", "plant", 2024, "2023-09-03"), 33)
        self.assertEqual(pw.day_ordinal("winter-wheat", "plant", 2024, "2024-01-01"), 153)

    def test_winter_wheat_harvest_plain_in_year(self):
        self.assertEqual(pw.day_ordinal("winter-wheat", "harvest", 2024, "2024-06-16"), 168)

    def test_plain_crop_wrong_calendar_year_returns_none(self):
        self.assertIsNone(pw.day_ordinal("corn", "plant", 2025, "2024-12-31"))


class CrossingsTest(unittest.TestCase):
    def test_real_zero_leading_reading_interpolates_all_four(self):
        readings = [
            ("2025-04-06", 0.0),
            ("2025-04-13", 10.0),
            ("2025-04-20", 50.0),
            ("2025-04-27", 80.0),
            ("2025-05-04", 90.0),
            ("2025-05-11", 99.0),
        ]
        cr = pw.year_crossings("corn", "plant", 2025, readings)
        self.assertIsNotNone(cr)
        self.assertEqual(
            sorted(cr),
            ["begin", "end", "mostActiveEnd", "mostActiveStart"],
        )
        # begin (5%) between 0% (doy 96) and 10% (doy 103) -> 99.5
        self.assertAlmostEqual(cr["begin"], 99.5, places=3)

    def test_exact_threshold_uses_that_date(self):
        readings = [
            ("2025-04-06", 5.0),
            ("2025-04-13", 15.0),
            ("2025-04-20", 85.0),
            ("2025-04-27", 95.0),
        ]
        cr = pw.year_crossings("corn", "plant", 2025, readings)
        self.assertEqual(
            cr["begin"],
            float(pw.day_ordinal("corn", "plant", 2025, "2025-04-06")),
        )

    def test_non_monotone_dropped(self):
        readings = [
            ("2025-04-06", 0.0),
            ("2025-04-13", 40.0),
            ("2025-04-20", 30.0),
            ("2025-04-27", 96.0),
        ]
        self.assertIsNone(pw.year_crossings("corn", "plant", 2025, readings))

    def test_left_censored_first_reading_above_5_dropped(self):
        readings = [
            ("2025-04-06", 12.0),
            ("2025-04-13", 60.0),
            ("2025-04-20", 96.0),
        ]
        self.assertIsNone(pw.year_crossings("corn", "plant", 2025, readings))

    def test_never_reaches_95_dropped(self):
        readings = [
            ("2025-04-06", 0.0),
            ("2025-04-13", 50.0),
            ("2025-04-20", 88.0),
        ]
        self.assertIsNone(pw.year_crossings("corn", "plant", 2025, readings))


class DeriveWindowTest(unittest.TestCase):
    def _years(self):
        out = {}
        for yr in range(2003, 2025):
            out[yr] = {"readings": [
                (f"{yr}-03-30", 0.0),
                (f"{yr}-04-20", 50.0),
                (f"{yr}-05-15", 96.0),
            ]}
        return out

    def test_fewer_than_20_usable_returns_none(self):
        plant = {2024: {"readings": [
            ("2024-03-30", 0.0),
            ("2024-04-20", 50.0),
            ("2024-05-15", 96.0),
        ]}}
        self.assertIsNone(pw.derive_block("corn", "plant", plant))

    def test_block_has_mmdd_and_uses_recent_20(self):
        blk, used = pw.derive_block("corn", "plant", self._years())
        self.assertEqual(set(blk), set(pw.THRESHOLD_KEYS))
        for v in blk.values():
            self.assertRegex(v, r"^[0-9]{2}-[0-9]{2}$")
        self.assertEqual(len(used), 20)
        self.assertEqual(used, list(range(2005, 2025)))

    def test_ordinal_to_mmdd_plain_and_winter_wheat(self):
        self.assertEqual(pw.ordinal_to_mmdd("corn", "plant", 124.0), "05-04")
        self.assertEqual(pw.ordinal_to_mmdd("winter-wheat", "plant", 153.0), "01-01")

    def test_round_half_up(self):
        self.assertEqual(pw.ordinal_to_mmdd("corn", "plant", 123.5), "05-04")


if __name__ == "__main__":
    unittest.main()
