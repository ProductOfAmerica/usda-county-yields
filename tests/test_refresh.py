"""Unit + integration tests for scripts/refresh.py.

No network, no fixture files: synthetic fixture is built inline.
"""
from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
from unittest import mock
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
        # 11. DROP: STOCKS is not an allowed statistic
        make_row(STATISTICCAT_DESC="STOCKS", UNIT_DESC="BU", VALUE="16500000"),
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


def _row(**overrides) -> dict:
    """A single post-filter 'kept' row dict, shaped for group_by_state.

    Defaults describe Iowa/Story corn YIELD 2024. Override any field. This is
    the dict form (group_by_state consumes dicts); make_row() is the list form
    (the filter consumes TSV rows).
    """
    base = {
        "SOURCE_DESC": "SURVEY", "COMMODITY_DESC": "CORN", "CLASS_DESC": "ALL CLASSES",
        "PRODN_PRACTICE_DESC": "ALL PRODUCTION PRACTICES", "UTIL_PRACTICE_DESC": "GRAIN",
        "STATISTICCAT_DESC": "YIELD", "UNIT_DESC": "BU / ACRE",
        "SHORT_DESC": "CORN, GRAIN - YIELD, MEASURED IN BU / ACRE",
        "DOMAIN_DESC": "TOTAL", "DOMAINCAT_DESC": "NOT SPECIFIED", "AGG_LEVEL_DESC": "COUNTY",
        "STATE_FIPS_CODE": "19", "STATE_ALPHA": "IA", "STATE_NAME": "IOWA",
        "COUNTY_CODE": "169", "COUNTY_ANSI": "169", "COUNTY_NAME": "STORY",
        "YEAR": "2024", "FREQ_DESC": "ANNUAL", "REFERENCE_PERIOD_DESC": "YEAR",
        "VALUE": "215.5", "CV_%": "1.8",
    }
    base.update(overrides)
    return base


def _filter(list_rows: list[list[str]]) -> tuple[list[str], int, list[dict]]:
    """Run a list of make_row() lists through _parse_filter; returns (header, total, kept)."""
    text = io.StringIO()
    writer = csv.writer(text, delimiter="\t")
    writer.writerow(HEADER)
    for r in list_rows:
        writer.writerow(r)
    text.seek(0)
    return refresh._parse_filter(csv.reader(text, delimiter="\t"))


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


class RequiredCvColTest(unittest.TestCase):
    def test_cv_pct_is_required(self):
        self.assertIn("CV_%", refresh.REQUIRED_COLS)

    def test_missing_cv_pct_aborts(self):
        bad_header = [c for c in HEADER if c != "CV_%"]
        text = io.StringIO()
        text.write("\t".join(bad_header) + "\n")
        text.seek(0)
        with self.assertRaises(SystemExit) as ctx:
            refresh._parse_filter(csv.reader(text, delimiter="\t"))
        self.assertIn("CV_%", str(ctx.exception))


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


# ---------- emit tests ----------


def _fixture_discovery() -> dict:
    return {
        "url": "https://www.nass.usda.gov/datasets/qs.crops_20260430.txt.gz",
        "etag": '"abc123"',
        "last_modified": "Thu, 30 Apr 2026 07:13:37 GMT",
        "content_length": 0,
        "date": "2026-04-30",
        "lag_days": 0,
    }


class EmitTestBase(unittest.TestCase):
    """Build the in-memory states tree once, redirect DATA_DIR to a tempdir.

    Every emit test inherits this so the real data/ tree is never touched.
    The tempdir is cleaned up in tearDown.
    """

    def setUp(self) -> None:
        reader = csv.reader(io.StringIO(fixture_csv_text()), delimiter="\t")
        _, _, kept = refresh._parse_filter(reader)
        self.states = refresh.group_by_state(kept)
        self.discovery = _fixture_discovery()
        self.refreshed_at = "2026-04-30T07:13:37Z"
        self.header = ["SOURCE_DESC", "SECTOR_DESC", "FUTURE_NASS_COLUMN"]

        self._tmp = tempfile.TemporaryDirectory()
        self._original_data_dir = refresh.DATA_DIR
        refresh.DATA_DIR = Path(self._tmp.name) / "data"

    def tearDown(self) -> None:
        refresh.DATA_DIR = self._original_data_dir
        self._tmp.cleanup()

    def _emit_all(self) -> None:
        """Run the full emit pipeline against self.states."""
        refresh.sort_series(self.states)
        self.missing_count, self.missing_samples = refresh.mark_canonical(self.states)
        self.expected: set[Path] = set()
        idx_path, _ = refresh.emit_index(self.states, self.discovery, self.refreshed_at)
        self.expected.add(idx_path)
        meta_paths, _ = refresh.emit_state_meta(self.states)
        self.expected |= meta_paths
        leaf_paths, _ = refresh.emit_point_leaves(self.states)
        self.expected |= leaf_paths
        rollup_paths, _ = refresh.emit_crop_rollups(self.states)
        self.expected |= rollup_paths
        audit_path, _ = refresh.emit_audit(self.header, self.refreshed_at, self.discovery["date"])
        self.expected.add(audit_path)


class IndexTest(EmitTestBase):
    def test_index_lists_all_states_with_crops_and_county_count(self) -> None:
        self._emit_all()
        idx = json.loads(refresh._index_path().read_text(encoding="utf-8"))
        self.assertEqual(set(idx["states"]), {"19", "20"})
        ia = idx["states"]["19"]
        self.assertEqual(ia["alpha"], "IA")
        self.assertEqual(ia["county_count"], 1)
        self.assertEqual(ia["crops"], ["corn", "soybeans"])
        ks = idx["states"]["20"]
        self.assertEqual(ks["alpha"], "KS")
        self.assertEqual(ks["crops"], ["wheat"])

    def test_index_carries_refreshed_at_and_source(self) -> None:
        self._emit_all()
        idx = json.loads(refresh._index_path().read_text(encoding="utf-8"))
        self.assertEqual(idx["refreshed_at"], self.refreshed_at)
        self.assertEqual(idx["source"]["publication_date"], "2026-04-30")
        self.assertEqual(idx["source"]["etag"], '"abc123"')
        self.assertEqual(idx["schema_version"], 3)


class StateMetaTest(EmitTestBase):
    def test_state_meta_lists_counties_and_crops(self) -> None:
        self._emit_all()
        meta = json.loads(refresh._state_meta_path("19").read_text(encoding="utf-8"))
        self.assertEqual(meta["state"]["fips"], "19")
        story = meta["counties"]["169"]
        self.assertEqual(story["name"], "STORY")
        self.assertEqual(story["crops"], ["corn", "soybeans"])

    def test_state_meta_has_no_timestamps(self) -> None:
        self._emit_all()
        meta = json.loads(refresh._state_meta_path("19").read_text(encoding="utf-8"))
        self.assertNotIn("refreshed_at", meta)
        self.assertNotIn("source_publication_date", meta)


class PointLeafTest(EmitTestBase):
    def test_point_leaf_shape_minimal_and_complete(self) -> None:
        self._emit_all()
        leaf_path = refresh._point_leaf_path("19", "169", "corn")
        leaf = json.loads(leaf_path.read_text(encoding="utf-8"))
        self.assertEqual(leaf["schema_version"], 3)
        self.assertEqual(leaf["state"]["fips"], "19")
        self.assertEqual(leaf["county"], {"code": "169", "name": "STORY"})
        self.assertEqual(leaf["commodity"], {"slug": "corn", "desc": "CORN"})
        self.assertEqual(len(leaf["series"]), 2)  # grain + silage
        grain = next(s for s in leaf["series"] if s["util_practice"] == "GRAIN")
        self.assertEqual(grain["values"]["2024"], 218.9)
        self.assertEqual(grain["suppressed"]["1980"], "D")

    def test_point_leaf_has_no_timestamps(self) -> None:
        self._emit_all()
        leaf = json.loads(refresh._point_leaf_path("19", "169", "corn").read_text(encoding="utf-8"))
        self.assertNotIn("refreshed_at", leaf)
        self.assertNotIn("source_publication_date", leaf)
        # Leaves must not carry the legacy NASS `ansi` field at the leaf level
        # (county.code is the canonical identifier; ansi was sometimes blank).
        self.assertNotIn("ansi", leaf["county"])

    def test_point_leaf_path_uses_county_code(self) -> None:
        self._emit_all()
        # Story IA: code == ansi == "169". The path segment must equal the
        # outer dict key from group_by_state, never the (sometimes blank) ansi.
        leaf_path = refresh._point_leaf_path("19", "169", "corn")
        self.assertTrue(leaf_path.exists())
        # Defensive: a hypothetical blank-ansi case would write to "/corn.json"
        # with an empty segment; verify our path builder never emits one.
        bad_path = refresh._point_leaf_path("19", "", "corn")
        self.assertNotIn("//", str(bad_path).replace("\\", "/").replace(":/", ""))


class CanonicalTest(EmitTestBase):
    def test_canonical_flag_corn_grain_not_silage(self) -> None:
        self._emit_all()
        leaf = json.loads(refresh._point_leaf_path("19", "169", "corn").read_text(encoding="utf-8"))
        grain = next(s for s in leaf["series"] if s["util_practice"] == "GRAIN")
        silage = next(s for s in leaf["series"] if s["util_practice"] == "SILAGE")
        self.assertTrue(grain.get("canonical"))
        self.assertNotIn("canonical", silage)

    def test_canonical_flag_absent_when_no_match(self) -> None:
        # Kansas wheat fixture row uses class="WINTER", so the canonical rule
        # (class="ALL CLASSES") finds no match. mark_canonical should count
        # the gap and the leaf must not carry canonical:true on any series.
        self._emit_all()
        leaf = json.loads(refresh._point_leaf_path("20", "181", "wheat").read_text(encoding="utf-8"))
        for s in leaf["series"]:
            self.assertNotIn("canonical", s)
        self.assertGreaterEqual(self.missing_count, 1)

    def test_canonical_rules_cover_all_commodities(self) -> None:
        crops = {c.lower() for c in refresh.COMMODITY_ALLOWLIST}
        rule_crops = {crop for (crop, _stat) in refresh.CANONICAL_RULES}
        self.assertEqual(crops - rule_crops, set())

    def test_missing_canonical_warns_and_continues(self) -> None:
        # mark_canonical must return a non-zero count for the Kansas wheat
        # fixture (class=WINTER) without raising, and the emit pipeline
        # must complete normally afterwards.
        self._emit_all()
        self.assertGreaterEqual(self.missing_count, 1)
        # Pipeline still produced index, meta, leaf, audit.
        self.assertTrue(refresh._index_path().exists())
        self.assertTrue(refresh._audit_path().exists())
        self.assertTrue(refresh._point_leaf_path("20", "181", "wheat").exists())


class SeriesOrderTest(EmitTestBase):
    def test_series_order_is_canonical(self) -> None:
        # Reverse the in-memory series order to simulate NASS row reorder,
        # then run sort_series and verify the resulting series order is
        # deterministic by canonical key tuple, not by source-row order.
        story_corn = self.states["19"]["counties"]["169"]["commodities"]["corn"]
        story_corn["series"].reverse()
        refresh.sort_series(self.states)
        keys = [refresh._series_sort_key(s) for s in story_corn["series"]]
        self.assertEqual(keys, sorted(keys))


class BootstrapTest(EmitTestBase):
    def test_bootstrap_when_index_missing(self) -> None:
        # Empty tempdir -> bootstrap_needed is True.
        self.assertFalse(refresh._index_path().exists())
        # After emit, the index exists -> bootstrap_needed is False.
        self._emit_all()
        self.assertTrue(refresh._index_path().exists())


class CropRollupTest(EmitTestBase):
    def test_crop_rollup_includes_all_counties_for_that_crop(self) -> None:
        self._emit_all()
        rollup = json.loads(refresh._crop_rollup_path("19", "corn").read_text(encoding="utf-8"))
        self.assertEqual(rollup["schema_version"], 3)
        self.assertEqual(rollup["state"]["fips"], "19")
        self.assertEqual(rollup["commodity"], {"slug": "corn", "desc": "CORN"})
        self.assertIn("169", rollup["counties"])
        self.assertEqual(rollup["counties"]["169"]["name"], "STORY")
        self.assertEqual(len(rollup["counties"]["169"]["series"]), 2)

    def test_crop_rollup_has_no_timestamps(self) -> None:
        self._emit_all()
        rollup = json.loads(refresh._crop_rollup_path("19", "corn").read_text(encoding="utf-8"))
        self.assertNotIn("refreshed_at", rollup)
        self.assertNotIn("source_publication_date", rollup)


class PruneTest(EmitTestBase):
    def test_prune_removes_stale_leaf(self) -> None:
        self._emit_all()
        # Plant a stale leaf that the current run did not produce.
        stale = refresh._point_leaf_path("99", "001", "corn")
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("{}", encoding="utf-8")
        self.assertTrue(stale.exists())
        deleted = refresh.prune_stale(self.expected)
        self.assertGreaterEqual(deleted, 1)
        self.assertFalse(stale.exists())
        # Active leaves must remain.
        self.assertTrue(refresh._point_leaf_path("19", "169", "corn").exists())


class AuditTest(EmitTestBase):
    def test_audit_carries_header_observed(self) -> None:
        self._emit_all()
        audit = json.loads(refresh._audit_path().read_text(encoding="utf-8"))
        self.assertEqual(audit["schema_version"], 3)
        self.assertEqual(audit["refreshed_at"], self.refreshed_at)
        self.assertEqual(audit["source_publication_date"], "2026-04-30")
        self.assertIn("FUTURE_NASS_COLUMN", audit["header_observed"])


class ParityTest(EmitTestBase):
    """Cross-emit value parity. Three separate views of the same data must
    agree byte-for-byte: the in-memory states tree (source of truth), the
    point leaves on disk, and the per-(state, crop) rollups on disk.
    """

    def _memory_tuples(self) -> set[tuple]:
        out: set[tuple] = set()
        for fips, st in self.states.items():
            for code, cty in st["counties"].items():
                for slug, com in cty["commodities"].items():
                    for s in com["series"]:
                        skey = refresh._series_sort_key(s)
                        for y, v in s["values"].items():
                            out.add((fips, code, slug, skey, y, "v", v))
                        for y, c in s["suppressed"].items():
                            out.add((fips, code, slug, skey, y, "s", c))
                        for y, r in s["raw"].items():
                            out.add((fips, code, slug, skey, y, "r", r))
        return out

    def _leaf_tuples(self) -> set[tuple]:
        out: set[tuple] = set()
        for leaf_path in (refresh.DATA_DIR / "states").rglob("*/counties/*/*.json"):
            d = json.loads(leaf_path.read_text(encoding="utf-8"))
            fips = d["state"]["fips"]
            code = d["county"]["code"]
            slug = d["commodity"]["slug"]
            for s in d["series"]:
                skey = refresh._series_sort_key(s)
                for y, v in s["values"].items():
                    out.add((fips, code, slug, skey, y, "v", v))
                for y, c in s["suppressed"].items():
                    out.add((fips, code, slug, skey, y, "s", c))
                for y, r in s["raw"].items():
                    out.add((fips, code, slug, skey, y, "r", r))
        return out

    def _rollup_tuples(self) -> set[tuple]:
        out: set[tuple] = set()
        for rollup_path in (refresh.DATA_DIR / "states").rglob("*/crops/*.json"):
            d = json.loads(rollup_path.read_text(encoding="utf-8"))
            fips = d["state"]["fips"]
            slug = d["commodity"]["slug"]
            for code, county_payload in d["counties"].items():
                for s in county_payload["series"]:
                    skey = refresh._series_sort_key(s)
                    for y, v in s["values"].items():
                        out.add((fips, code, slug, skey, y, "v", v))
                    for y, c in s["suppressed"].items():
                        out.add((fips, code, slug, skey, y, "s", c))
                    for y, r in s["raw"].items():
                        out.add((fips, code, slug, skey, y, "r", r))
        return out

    def test_memory_leaf_parity(self) -> None:
        self._emit_all()
        self.assertEqual(self._memory_tuples(), self._leaf_tuples())

    def test_point_rollup_parity(self) -> None:
        self._emit_all()
        self.assertEqual(self._leaf_tuples(), self._rollup_tuples())


class WriteIfChangedTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_if_changed_writes_when_absent(self) -> None:
        path = self.tmpdir / "sub" / "file.json"
        wrote = refresh.write_if_changed(path, "hello\n")
        self.assertTrue(wrote)
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(encoding="utf-8"), "hello\n")

    def test_write_if_changed_skips_when_identical(self) -> None:
        path = self.tmpdir / "file.json"
        path.write_text("hello\n", encoding="utf-8")
        mtime_before = path.stat().st_mtime_ns
        wrote = refresh.write_if_changed(path, "hello\n")
        self.assertFalse(wrote)
        self.assertEqual(path.stat().st_mtime_ns, mtime_before)

    def test_write_if_changed_writes_when_different(self) -> None:
        path = self.tmpdir / "file.json"
        path.write_text("old\n", encoding="utf-8")
        wrote = refresh.write_if_changed(path, "new\n")
        self.assertTrue(wrote)
        self.assertEqual(path.read_text(encoding="utf-8"), "new\n")


# ---------- hardening pass tests ----------


class SlugCollisionTest(unittest.TestCase):
    """Pin the inline guard at refresh.group_by_state's slug-collision check."""

    def test_slug_collision_aborts(self) -> None:
        # "FOO BAR" and "FOO   BAR" both slugify to "foo-bar" because
        # SLUG_DASHES collapses repeated dashes.
        rows = [
            {
                "STATE_FIPS_CODE": "19", "STATE_ALPHA": "IA", "STATE_NAME": "IOWA",
                "COUNTY_CODE": "169", "COUNTY_ANSI": "169", "COUNTY_NAME": "STORY",
                "COMMODITY_DESC": "FOO BAR",
                "CLASS_DESC": "ALL CLASSES",
                "PRODN_PRACTICE_DESC": "ALL PRODUCTION PRACTICES",
                "UTIL_PRACTICE_DESC": "GRAIN", "STATISTICCAT_DESC": "YIELD",
                "UNIT_DESC": "BU / ACRE",
                "SHORT_DESC": "FOO BAR - YIELD", "YEAR": "2024", "VALUE": "1.0", "CV_%": "",
            },
            {
                "STATE_FIPS_CODE": "19", "STATE_ALPHA": "IA", "STATE_NAME": "IOWA",
                "COUNTY_CODE": "169", "COUNTY_ANSI": "169", "COUNTY_NAME": "STORY",
                "COMMODITY_DESC": "FOO   BAR",   # different desc, same slug
                "CLASS_DESC": "ALL CLASSES",
                "PRODN_PRACTICE_DESC": "ALL PRODUCTION PRACTICES",
                "UTIL_PRACTICE_DESC": "GRAIN", "STATISTICCAT_DESC": "YIELD",
                "UNIT_DESC": "BU / ACRE",
                "SHORT_DESC": "FOO BAR - YIELD", "YEAR": "2024", "VALUE": "2.0", "CV_%": "",
            },
        ]
        self.assertEqual(refresh.slugify("FOO BAR"), refresh.slugify("FOO   BAR"))
        with self.assertRaises(SystemExit) as ctx:
            refresh.group_by_state(rows)
        self.assertIn("collision", str(ctx.exception).lower())


class MalformedInputTest(unittest.TestCase):
    """Tripwire asserts in group_by_state for FIPS / county_code shape."""

    def _row(self, **overrides) -> dict:
        base = {
            "STATE_FIPS_CODE": "19", "STATE_ALPHA": "IA", "STATE_NAME": "IOWA",
            "COUNTY_CODE": "169", "COUNTY_ANSI": "169", "COUNTY_NAME": "STORY",
            "COMMODITY_DESC": "CORN",
            "CLASS_DESC": "ALL CLASSES",
            "PRODN_PRACTICE_DESC": "ALL PRODUCTION PRACTICES",
            "UTIL_PRACTICE_DESC": "GRAIN", "UNIT_DESC": "BU / ACRE",
            "SHORT_DESC": "CORN, GRAIN - YIELD, MEASURED IN BU / ACRE",
            "YEAR": "2024", "VALUE": "215.5",
        }
        base.update(overrides)
        return base

    def test_group_aborts_on_malformed_fips(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            refresh.group_by_state([self._row(STATE_FIPS_CODE="ABCD")])
        self.assertIn("STATE_FIPS_CODE", str(ctx.exception))

    def test_group_aborts_on_long_county_code(self) -> None:
        # zfill(3) does not truncate; "9999" stays length 4.
        with self.assertRaises(SystemExit) as ctx:
            refresh.group_by_state([self._row(COUNTY_CODE="9999")])
        self.assertIn("COUNTY_CODE", str(ctx.exception))


class CanonicalBandGateTest(unittest.TestCase):
    """Gate 3: validate_canonical_coverage thresholds."""

    def test_canonical_band_aborts_on_excess(self) -> None:
        # 10/100 = 10% > 5% tolerance
        with self.assertRaises(SystemExit) as ctx:
            refresh.validate_canonical_coverage(missing_count=10, total_pairs=100)
        self.assertIn("Missing-canonical", str(ctx.exception))

    def test_canonical_band_passes_on_few_missing(self) -> None:
        # 3/100 = 3% < 5% tolerance -> no raise
        refresh.validate_canonical_coverage(missing_count=3, total_pairs=100)

    def test_canonical_band_passes_on_zero_pairs(self) -> None:
        # Edge case: zero pairs returns early without raising.
        refresh.validate_canonical_coverage(missing_count=0, total_pairs=0)


class LeafShapeAssertTest(EmitTestBase):
    """_assert_leaf_shape catches drift in the published leaf contract."""

    def test_leaf_shape_assert_passes_on_emit(self) -> None:
        # All leaves the emit pipeline produces must pass the shape assert.
        # If this fails, the leaf shape diverged from data/_schema/leaf.json.
        self._emit_all()
        leaf_dir = refresh.DATA_DIR / "states"
        leaves = list(leaf_dir.rglob("counties/*/*.json"))
        self.assertGreater(len(leaves), 0)
        for leaf_path in leaves:
            d = json.loads(leaf_path.read_text(encoding="utf-8"))
            refresh._assert_leaf_shape(d)  # raises on drift

    def test_leaf_shape_assert_rejects_extra_top_key(self) -> None:
        bad = {
            "schema_version": 3,
            "state": {"fips": "19", "alpha": "IA", "name": "IOWA"},
            "county": {"code": "169", "name": "STORY"},
            "commodity": {"slug": "corn", "desc": "CORN"},
            "series": [],
            "unexpected_field": "drift",
        }
        with self.assertRaises(SystemExit):
            refresh._assert_leaf_shape(bad)

    def test_leaf_shape_assert_rejects_missing_series_keys(self) -> None:
        bad = {
            "schema_version": 3,
            "state": {"fips": "19", "alpha": "IA", "name": "IOWA"},
            "county": {"code": "169", "name": "STORY"},
            "commodity": {"slug": "corn", "desc": "CORN"},
            "series": [
                {
                    "class": "ALL CLASSES",
                    "prodn_practice": "ALL PRODUCTION PRACTICES",
                    "util_practice": "GRAIN",
                    "unit": "BU / ACRE",
                    "short_desc": "CORN, GRAIN - YIELD, MEASURED IN BU / ACRE",
                    # statistic, values, cv, suppressed, raw all missing -- producer regression
                },
            ],
        }
        with self.assertRaises(SystemExit):
            refresh._assert_leaf_shape(bad)


class IsCaughtUpTest(unittest.TestCase):
    """is_caught_up predicate distinguishes 'nothing to do' from 'NASS missing'."""

    def test_today_equals_last_known(self) -> None:
        self.assertTrue(refresh.is_caught_up(date(2026, 5, 1), date(2026, 5, 1)))

    def test_last_known_in_future(self) -> None:
        # Clock-skew defensive: still treat as caught-up.
        self.assertTrue(refresh.is_caught_up(date(2026, 5, 2), date(2026, 5, 1)))

    def test_last_known_yesterday(self) -> None:
        self.assertFalse(refresh.is_caught_up(date(2026, 4, 30), date(2026, 5, 1)))

    def test_bootstrap(self) -> None:
        self.assertFalse(refresh.is_caught_up(None, date(2026, 5, 1)))


class MainCaughtUpTest(unittest.TestCase):
    """main() returns exit 0 (not 1) on a same-day rerun."""

    def setUp(self) -> None:
        self._original_load = refresh.load_state
        self._original_ping = refresh.ping_healthchecks
        self._ping_calls = 0

        def _fake_ping() -> None:
            self._ping_calls += 1

        refresh.load_state = lambda: {
            "last_successful_date": "2026-05-01",
            "last_etag": "etag-from-prior-run",
        }
        refresh.ping_healthchecks = _fake_ping

    def tearDown(self) -> None:
        refresh.load_state = self._original_load
        refresh.ping_healthchecks = self._original_ping

    def test_main_returns_zero_when_already_caught_up_today(self) -> None:
        # Before this fix, this same-day rerun returned 1 because discover()
        # produced an empty window and main() hit the "no fresh file" path.
        result = refresh.main(today=date(2026, 5, 1))
        self.assertEqual(result, 0)
        self.assertEqual(self._ping_calls, 1)


class FilterStatisticsTest(unittest.TestCase):
    STATS = ["YIELD", "PRODUCTION", "AREA HARVESTED", "AREA PLANTED", "AREA PLANTED, NET"]

    def test_keeps_five_statistics(self):
        rows = [make_row(STATISTICCAT_DESC=s, UNIT_DESC="ACRES") for s in self.STATS]
        _, total, kept = _filter(rows)
        self.assertEqual(total, 5)
        self.assertEqual(len(kept), 5)

    def test_excludes_other_statistics(self):
        rows = [make_row(STATISTICCAT_DESC="STOCKS"),
                make_row(STATISTICCAT_DESC="PRICE RECEIVED")]
        _, _, kept = _filter(rows)
        self.assertEqual(len(kept), 0)


class GroupStatisticCvTest(unittest.TestCase):
    def test_statistic_on_series(self):
        states = refresh.group_by_state([_row(
            STATISTICCAT_DESC="PRODUCTION", UNIT_DESC="BU", VALUE="1000",
            SHORT_DESC="CORN, GRAIN - PRODUCTION, MEASURED IN BU")])
        series = states["19"]["counties"]["169"]["commodities"]["corn"]["series"][0]
        self.assertEqual(series["statistic"], "PRODUCTION")

    def test_cv_parallel_to_values(self):
        states = refresh.group_by_state([_row(VALUE="215.5", **{"CV_%": "1.8"})])
        series = states["19"]["counties"]["169"]["commodities"]["corn"]["series"][0]
        self.assertEqual(series["values"], {"2024": 215.5})
        self.assertEqual(series["cv"], {"2024": 1.8})

    def test_blank_cv_absent(self):
        states = refresh.group_by_state([_row(VALUE="215.5", **{"CV_%": ""})])
        series = states["19"]["counties"]["169"]["commodities"]["corn"]["series"][0]
        self.assertEqual(series["cv"], {})

    def test_same_statistic_different_unit_separate_series(self):
        states = refresh.group_by_state([
            _row(),
            _row(UNIT_DESC="BU / NET PLANTED ACRE",
                 SHORT_DESC="CORN, GRAIN - YIELD, MEASURED IN BU / NET PLANTED ACRE",
                 VALUE="97.8"),
        ])
        com = states["19"]["counties"]["169"]["commodities"]["corn"]
        self.assertEqual(len(com["series"]), 2)


class LeafV3ShapeTest(unittest.TestCase):
    def _leaf(self):
        states = refresh.group_by_state([_row()])
        refresh.sort_series(states)
        refresh.mark_canonical(states)
        com = states["19"]["counties"]["169"]["commodities"]["corn"]
        return {
            "schema_version": 3,
            "state": {"fips": "19", "alpha": "IA", "name": "IOWA"},
            "county": {"code": "169", "name": "STORY"},
            "commodity": {"slug": "corn", "desc": "CORN"},
            "series": com["series"],
        }

    def test_v3_leaf_passes(self):
        refresh._assert_leaf_shape(self._leaf())

    def test_v2_leaf_rejected(self):
        leaf = self._leaf()
        leaf["schema_version"] = 2
        with self.assertRaises(SystemExit):
            refresh._assert_leaf_shape(leaf)

    def test_series_missing_statistic_rejected(self):
        leaf = self._leaf()
        del leaf["series"][0]["statistic"]
        with self.assertRaises(SystemExit):
            refresh._assert_leaf_shape(leaf)


class AllArtifactsV3Test(unittest.TestCase):
    def _states(self):
        states = refresh.group_by_state([_row()])
        refresh.sort_series(states)
        refresh.mark_canonical(states)
        return states

    def test_index_meta_audit_are_v3(self):
        states = self._states()
        discovery = {"url": "u", "last_modified": "m", "etag": '"e"',
                     "date": "2026-05-30", "lag_days": 0}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(refresh, "DATA_DIR", Path(td)):
                refresh.emit_index(states, discovery, "2026-05-30T00:00:00Z")
                refresh.emit_state_meta(states)
                refresh.emit_audit(["H"], "2026-05-30T00:00:00Z", "2026-05-30")
                idx = json.loads((Path(td) / "index.json").read_text())
                meta = json.loads((Path(td) / "states" / "19" / "meta.json").read_text())
                audit = json.loads((Path(td) / "_audit" / "latest.json").read_text())
        self.assertEqual(idx["schema_version"], 3)
        self.assertEqual(meta["schema_version"], 3)
        self.assertEqual(audit["schema_version"], 3)


class CanonicalRulesTableTest(unittest.TestCase):
    def test_every_crop_statistic_has_a_rule(self):
        crops = {c.lower() for c in refresh.COMMODITY_ALLOWLIST}
        for crop in crops:
            for stat in refresh.STATISTIC_ALLOWLIST:
                self.assertIn((crop, stat), refresh.CANONICAL_RULES,
                              f"missing rule for {(crop, stat)}")

    def test_corn_area_planted_is_all_utilization(self):
        self.assertEqual(
            refresh.CANONICAL_RULES[("corn", "AREA PLANTED")]["util_practice"],
            "ALL UTILIZATION PRACTICES")

    def test_corn_area_harvested_is_grain(self):
        self.assertEqual(
            refresh.CANONICAL_RULES[("corn", "AREA HARVESTED")]["util_practice"],
            "GRAIN")


class MarkCanonicalV3Test(unittest.TestCase):
    def test_marks_one_per_statistic(self):
        states = refresh.group_by_state([
            _row(),
            _row(STATISTICCAT_DESC="PRODUCTION", UNIT_DESC="BU", VALUE="1000",
                 SHORT_DESC="CORN, GRAIN - PRODUCTION, MEASURED IN BU"),
        ])
        refresh.sort_series(states)
        refresh.mark_canonical(states)
        series = states["19"]["counties"]["169"]["commodities"]["corn"]["series"]
        canon = {s["statistic"] for s in series if s.get("canonical")}
        self.assertEqual(canon, {"YIELD", "PRODUCTION"})

    def test_duplicate_candidate_aborts(self):
        states = refresh.group_by_state([
            _row(),
            _row(SHORT_DESC="CORN, GRAIN - YIELD, MEASURED IN BU / ACRE (DUP)"),
        ])
        refresh.sort_series(states)
        with self.assertRaises(SystemExit):
            refresh.mark_canonical(states)

    def test_missing_yield_counted(self):
        states = refresh.group_by_state([
            _row(STATISTICCAT_DESC="PRODUCTION", UNIT_DESC="BU", VALUE="1000",
                 SHORT_DESC="CORN, GRAIN - PRODUCTION, MEASURED IN BU"),
        ])
        refresh.sort_series(states)
        missing, _ = refresh.mark_canonical(states)
        self.assertEqual(missing, 1)


class BaselineMapTest(unittest.TestCase):
    def test_legacy_int_baseline_treated_as_absent(self):
        self.assertIsNone(refresh.leaf_baseline({"last_filtered_row_count": 1318932}))

    def test_map_baseline_read(self):
        self.assertEqual(
            refresh.leaf_baseline({"last_filtered_row_count": {"leaf": 4300000}}),
            4300000)

    def test_absent_baseline_is_none(self):
        self.assertIsNone(refresh.leaf_baseline({}))


class RollupYieldOnlyTest(unittest.TestCase):
    def _states(self):
        states = refresh.group_by_state([
            _row(),
            _row(STATISTICCAT_DESC="PRODUCTION", UNIT_DESC="BU", VALUE="1000",
                 SHORT_DESC="CORN, GRAIN - PRODUCTION, MEASURED IN BU"),
        ])
        refresh.sort_series(states)
        refresh.mark_canonical(states)
        return states

    def test_rollup_excludes_non_yield_series(self):
        states = self._states()
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(refresh, "DATA_DIR", Path(td)):
                refresh.emit_crop_rollups(states)
                rollup = json.loads((Path(td) / "states" / "19" / "crops" / "corn.json").read_text())
        stats = {s["statistic"] for s in rollup["counties"]["169"]["series"]}
        self.assertEqual(stats, {"YIELD"})

    def test_rollup_does_not_mutate_leaf_series(self):
        states = self._states()
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(refresh, "DATA_DIR", Path(td)):
                refresh.emit_crop_rollups(states)
        leaf_series = states["19"]["counties"]["169"]["commodities"]["corn"]["series"]
        self.assertEqual(len({s["statistic"] for s in leaf_series}), 2)


if __name__ == "__main__":
    unittest.main()
