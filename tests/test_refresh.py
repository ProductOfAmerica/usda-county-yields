"""Unit + integration tests for scripts/refresh.py.

No network, no fixture files: synthetic fixture is built inline.
"""
from __future__ import annotations

import csv
import io
import sys
import unittest
from datetime import date
from pathlib import Path

# Make scripts/ importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import refresh  # noqa: E402


# ---------- inline fixture ----------

# Real NASS bulk file has 39 columns. We synthesize the same shape so the
# tolerant header reader can pick out the columns we depend on.
HEADER = [
    "SOURCE_DESC", "SECTOR_DESC", "GROUP_DESC", "COMMODITY_DESC", "CLASS_DESC",
    "PRODN_PRACTICE_DESC", "UTIL_PRACTICE_DESC", "STATISTICCAT_DESC", "UNIT_DESC",
    "SHORT_DESC", "DOMAIN_DESC", "DOMAINCAT_DESC", "AGG_LEVEL_DESC", "STATE_ANSI",
    "STATE_FIPS_CODE", "STATE_ALPHA", "STATE_NAME", "ASD_CODE", "ASD_DESC",
    "COUNTY_ANSI", "COUNTY_CODE", "COUNTY_NAME", "REGION_DESC", "ZIP_5",
    "WATERSHED_CODE", "WATERSHED_DESC", "CONGR_DISTRICT_CODE", "COUNTRY_CODE",
    "COUNTRY_NAME", "LOCATION_DESC", "YEAR", "FREQ_DESC", "BEGIN_CODE", "END_CODE",
    "REFERENCE_PERIOD_DESC", "WEEK_ENDING", "LOAD_TIME", "VALUE", "CV_%",
]

IDX = {n: i for i, n in enumerate(HEADER)}


def make_row(**overrides) -> list[str]:
    """Build a 39-col row with sensible NASS-style defaults; overrides win."""
    row = [""] * len(HEADER)
    defaults = {
        "SOURCE_DESC": "SURVEY", "SECTOR_DESC": "CROPS", "GROUP_DESC": "FIELD CROPS",
        "COMMODITY_DESC": "CORN", "CLASS_DESC": "ALL CLASSES",
        "PRODN_PRACTICE_DESC": "ALL PRODUCTION PRACTICES",
        "UTIL_PRACTICE_DESC": "GRAIN", "STATISTICCAT_DESC": "YIELD",
        "UNIT_DESC": "BU / ACRE",
        "SHORT_DESC": "CORN, GRAIN - YIELD, MEASURED IN BU / ACRE",
        "DOMAIN_DESC": "TOTAL", "DOMAINCAT_DESC": "NOT SPECIFIED",
        "AGG_LEVEL_DESC": "COUNTY", "STATE_ANSI": "19", "STATE_FIPS_CODE": "19",
        "STATE_ALPHA": "IA", "STATE_NAME": "IOWA",
        "COUNTY_ANSI": "169", "COUNTY_CODE": "169", "COUNTY_NAME": "STORY",
        "YEAR": "2024", "FREQ_DESC": "ANNUAL", "REFERENCE_PERIOD_DESC": "YEAR",
        "VALUE": "218.9",
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        row[IDX[k]] = v
    return row


def fixture_rows() -> list[list[str]]:
    return [
        # 1. KEEP: Iowa Story corn 2024
        make_row(YEAR="2024", VALUE="218.9"),
        # 2. KEEP: Iowa Story corn 2023
        make_row(YEAR="2023", VALUE="201.5"),
        # 3. KEEP: Iowa Story corn 1980 -- suppressed
        make_row(YEAR="1980", VALUE="(D)"),
        # 4. KEEP: Iowa Story corn 1990 with comma in VALUE -> parses to 1234.5
        make_row(YEAR="1990", VALUE="1,234.5"),
        # 5. KEEP: Iowa Story corn silage (different util/unit -> separate series)
        make_row(
            UTIL_PRACTICE_DESC="SILAGE", UNIT_DESC="TONS / ACRE",
            SHORT_DESC="CORN, SILAGE - YIELD, MEASURED IN TONS / ACRE",
            YEAR="2024", VALUE="21.5",
        ),
        # 6. KEEP: Iowa Story soybeans 2024
        make_row(
            COMMODITY_DESC="SOYBEANS",
            UTIL_PRACTICE_DESC="ALL UTILIZATION PRACTICES",
            SHORT_DESC="SOYBEANS - YIELD, MEASURED IN BU / ACRE",
            YEAR="2024", VALUE="60.5",
        ),
        # 7. KEEP: Kansas Sherman wheat 2024
        make_row(
            COMMODITY_DESC="WHEAT", CLASS_DESC="WINTER",
            SHORT_DESC="WHEAT, WINTER - YIELD, MEASURED IN BU / ACRE",
            STATE_ANSI="20", STATE_FIPS_CODE="20", STATE_ALPHA="KS", STATE_NAME="KANSAS",
            COUNTY_ANSI="181", COUNTY_CODE="181", COUNTY_NAME="SHERMAN",
            YEAR="2024", VALUE="50.0",
        ),
        # 8. DROP: CENSUS source
        make_row(SOURCE_DESC="CENSUS", YEAR="2017", VALUE="215.4"),
        # 9. DROP: STATE aggregation
        make_row(AGG_LEVEL_DESC="STATE", VALUE="198.0"),
        # 10. DROP: commodity not in allowlist
        make_row(COMMODITY_DESC="COTTON", VALUE="800"),
        # 11. DROP: not YIELD statistic
        make_row(STATISTICCAT_DESC="PRODUCTION", UNIT_DESC="BU", VALUE="16500000"),
        # 12. DROP: DOMAIN_DESC != TOTAL
        make_row(DOMAIN_DESC="ECONOMIC CLASS", DOMAINCAT_DESC="ECONOMIC CLASS: (X)"),
    ]


def fixture_csv_text() -> str:
    """Return the full TSV text including header + rows."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter="\t")
    writer.writerow(HEADER)
    for row in fixture_rows():
        writer.writerow(row)
    return buf.getvalue()


# ---------- unit tests ----------

class SlugifyTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(refresh.slugify("CORN"), "corn")
        self.assertEqual(refresh.slugify("SOYBEANS"), "soybeans")

    def test_multiword_collapses_dashes(self):
        self.assertEqual(refresh.slugify("SUGAR  BEETS"), "sugar-beets")
        self.assertEqual(refresh.slugify("SWEET / CORN"), "sweet-corn")

    def test_punctuation_stripped(self):
        self.assertEqual(refresh.slugify("BEANS, DRY EDIBLE"), "beans-dry-edible")
        self.assertEqual(refresh.slugify("--corn--"), "corn")


class ParseValueTest(unittest.TestCase):
    def test_numeric(self):
        self.assertEqual(refresh.parse_value("218.9"), (218.9, None, None))

    def test_numeric_with_thousands_comma(self):
        self.assertEqual(refresh.parse_value("1,234.5"), (1234.5, None, None))

    def test_numeric_with_whitespace(self):
        self.assertEqual(refresh.parse_value("  42  "), (42.0, None, None))

    def test_suppression_codes(self):
        for code in ("D", "NA", "S", "X", "Z"):
            self.assertEqual(refresh.parse_value(f"({code})"), (None, code, None))

    def test_empty(self):
        self.assertEqual(refresh.parse_value(""), (None, None, None))
        self.assertEqual(refresh.parse_value("   "), (None, None, None))

    def test_unparseable_fallback(self):
        self.assertEqual(refresh.parse_value("WEIRD"), (None, None, "WEIRD"))


class DiscoverTest(unittest.TestCase):
    def test_returns_none_when_already_caught_up_today(self):
        # last_known is today -> earliest = today + 1 > today -> nothing to do
        result = refresh.discover(date(2026, 4, 30), date(2026, 4, 30))
        self.assertIsNone(result)


# ---------- integration tests ----------

class FilterAndGroupTest(unittest.TestCase):
    def setUp(self):
        reader = csv.reader(io.StringIO(fixture_csv_text()), delimiter="\t")
        self.header, self.total, self.kept = refresh._parse_filter(reader)

    def test_total_count(self):
        self.assertEqual(self.total, 12)

    def test_filter_keeps_seven(self):
        self.assertEqual(len(self.kept), 7)

    def test_dropped_census(self):
        for row in self.kept:
            self.assertEqual(row["SOURCE_DESC"], "SURVEY")

    def test_dropped_state_level(self):
        for row in self.kept:
            self.assertEqual(row["AGG_LEVEL_DESC"], "COUNTY")

    def test_dropped_non_allowlisted_commodity(self):
        commodities = {row["COMMODITY_DESC"] for row in self.kept}
        self.assertTrue(commodities.issubset({"CORN", "SOYBEANS", "WHEAT"}))


class GroupByStateTest(unittest.TestCase):
    def setUp(self):
        reader = csv.reader(io.StringIO(fixture_csv_text()), delimiter="\t")
        _, _, kept = refresh._parse_filter(reader)
        self.states = refresh.group_by_state(kept)

    def test_two_states_present(self):
        self.assertEqual(set(self.states.keys()), {"19", "20"})

    def test_iowa_story_corn_has_two_series(self):
        story = self.states["19"]["counties"]["169"]
        corn_series = story["commodities"]["corn"]["series"]
        self.assertEqual(len(corn_series), 2)
        utils = {s["util_practice"] for s in corn_series}
        self.assertEqual(utils, {"GRAIN", "SILAGE"})

    def test_grain_series_carries_all_yields_and_suppressions(self):
        story = self.states["19"]["counties"]["169"]
        grain = next(
            s for s in story["commodities"]["corn"]["series"]
            if s["util_practice"] == "GRAIN"
        )
        self.assertEqual(grain["values"]["2024"], 218.9)
        self.assertEqual(grain["values"]["2023"], 201.5)
        self.assertEqual(grain["values"]["1990"], 1234.5)
        self.assertEqual(grain["suppressed"]["1980"], "D")

    def test_soybeans_present_under_iowa_story(self):
        story = self.states["19"]["counties"]["169"]
        self.assertIn("soybeans", story["commodities"])

    def test_kansas_sherman_wheat(self):
        sherman = self.states["20"]["counties"]["181"]
        self.assertEqual(sherman["name"], "SHERMAN")
        self.assertIn("wheat", sherman["commodities"])


class ValidateTest(unittest.TestCase):
    def test_bootstrap_skips_band_check(self):
        # last_filtered_count is None -> bootstrap, anything goes
        refresh.validate(total_rows=100, kept_rows=10, last_filtered_count=None)

    def test_within_band_passes(self):
        refresh.validate(100, 10, last_filtered_count=10)
        refresh.validate(100, 11, last_filtered_count=10)
        refresh.validate(100, 9, last_filtered_count=10)

    def test_outside_band_aborts(self):
        with self.assertRaises(SystemExit):
            refresh.validate(100, 12, last_filtered_count=10)
        with self.assertRaises(SystemExit):
            refresh.validate(100, 5, last_filtered_count=10)

    def test_zero_total_aborts(self):
        with self.assertRaises(SystemExit):
            refresh.validate(0, 0, None)

    def test_zero_kept_aborts(self):
        with self.assertRaises(SystemExit):
            refresh.validate(100, 0, None)


class MissingRequiredColumnTest(unittest.TestCase):
    def test_missing_required_col_aborts(self):
        # Drop SOURCE_DESC from the header; everything else stays
        bad_header = [c for c in HEADER if c != "SOURCE_DESC"]
        text = io.StringIO()
        text.write("\t".join(bad_header) + "\n")
        text.seek(0)
        reader = csv.reader(text, delimiter="\t")
        with self.assertRaises(SystemExit) as ctx:
            refresh._parse_filter(reader)
        self.assertIn("SOURCE_DESC", str(ctx.exception))


class TolerantHeaderTest(unittest.TestCase):
    def test_extra_columns_tolerated(self):
        # Append a brand-new NASS column at the end; refresh should not abort
        extended_header = HEADER + ["FUTURE_NASS_COLUMN"]
        rows = [extended_header]
        for row in fixture_rows():
            rows.append(row + ["something_new"])
        text = io.StringIO()
        writer = csv.writer(text, delimiter="\t")
        for row in rows:
            writer.writerow(row)
        text.seek(0)
        reader = csv.reader(text, delimiter="\t")
        header, total, kept = refresh._parse_filter(reader)
        self.assertEqual(total, 12)
        self.assertEqual(len(kept), 7)
        self.assertIn("FUTURE_NASS_COLUMN", header)


if __name__ == "__main__":
    unittest.main()
