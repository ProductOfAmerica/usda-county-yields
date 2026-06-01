"""Tests for scripts/derived.py (SP-C derived families)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import derived  # noqa: E402
import refresh  # noqa: E402


# ---- in-memory fixture builders (mirror refresh.group_by_state output) ----

def _series(statistic, unit, values, canonical=False, suppressed=None):
    s = {"statistic": statistic, "class": "ALL CLASSES",
         "prodn_practice": "ALL PRODUCTION PRACTICES",
         "util_practice": "GRAIN", "unit": unit, "short_desc": statistic,
         "values": dict(values), "cv": {}, "suppressed": dict(suppressed or {}), "raw": {}}
    if canonical:
        s["canonical"] = True
    return s


def _county(name, commodities):
    return {"name": name, "ansi": "", "commodities": commodities}


def _com(desc, slug, series):
    return {"commodity_desc": desc, "series": series}


def _states_one_county():
    # Iowa (19) / Story (169) / corn: yield, production, area harvested, area planted
    corn = _com("CORN", "corn", [
        _series("YIELD", "BU / ACRE", {"2023": 200.0, "2024": 210.0}, canonical=True),
        _series("PRODUCTION", "BU", {"2024": 2_100_000.0}, canonical=True),
        _series("AREA HARVESTED", "ACRES", {"2024": 10_000.0}, canonical=True),
        _series("AREA PLANTED", "ACRES", {"2024": 10_500.0}, canonical=True),
    ])
    return {"19": {"state": {"fips": "19", "alpha": "IA", "name": "IOWA"},
                   "counties": {"169": _county("STORY", {"corn": corn})}}}


def _price_states():
    return {"19": {"state": {"fips": "19", "alpha": "IA", "name": "IOWA"},
                   "crops": {"corn": {"commodity_desc": "CORN", "series": [
                       {"class": "ALL CLASSES", "period": "MARKETING YEAR", "unit": "$ / BU",
                        "canonical": True, "values": {"2024": 4.80}, "suppressed": {}}]}}}}


class CanonicalHelperTest(unittest.TestCase):
    def test_picks_canonical_per_statistic(self):
        com = _states_one_county()["19"]["counties"]["169"]["commodities"]["corn"]
        self.assertEqual(derived._canonical(com, "YIELD")["unit"], "BU / ACRE")
        self.assertEqual(derived._canonical(com, "PRODUCTION")["unit"], "BU")
        self.assertIsNone(derived._canonical(com, "AREA PLANTED, NET"))


class RevenueTest(unittest.TestCase):
    def test_harvested_and_planted(self):
        rev = derived.compute_revenue(_states_one_county(), _price_states())
        r = rev[("19", "169", "corn")]["2024"]
        self.assertEqual(r["marketing_year"], "2024")
        self.assertAlmostEqual(r["revenue_per_harvested_acre"], 210.0 * 4.80, places=4)
        # production 2_100_000 * 4.80 / area_planted 10_500 = 960.0
        self.assertAlmostEqual(r["revenue_per_planted_acre"], 960.0, places=4)

    def test_harvested_without_area_has_no_planted(self):
        states = _states_one_county()
        # drop area planted -> no per-planted, but per-harvested still present for 2024
        com = states["19"]["counties"]["169"]["commodities"]["corn"]
        com["series"] = [s for s in com["series"] if s["statistic"] != "AREA PLANTED"]
        rev = derived.compute_revenue(states, _price_states())
        r = rev[("19", "169", "corn")]["2024"]
        self.assertIn("revenue_per_harvested_acre", r)
        self.assertNotIn("revenue_per_planted_acre", r)

    def test_no_price_year_skipped(self):
        # yield 2023 has no 2023 price -> no revenue record for 2023
        rev = derived.compute_revenue(_states_one_county(), _price_states())
        self.assertNotIn("2023", rev[("19", "169", "corn")])


class RankTest(unittest.TestCase):
    def _multi(self):
        # one state (19) with 4 counties, distinct + tied yields for 2024
        def cy(name, y):
            return _county(name, {"corn": _com("CORN", "corn",
                          [_series("YIELD", "BU / ACRE", {"2024": y}, canonical=True)])})
        return {"19": {"state": {"fips": "19", "alpha": "IA", "name": "IOWA"},
                       "counties": {"001": cy("A", 100.0), "002": cy("B", 90.0),
                                    "003": cy("C", 90.0), "004": cy("D", 80.0)}}}

    def test_competition_rank_and_percentile(self):
        ranks = derived.compute_ranks(self._multi())
        a = ranks[("19", "001", "corn")]["2024"]
        b = ranks[("19", "002", "corn")]["2024"]
        c = ranks[("19", "003", "corn")]["2024"]
        d = ranks[("19", "004", "corn")]["2024"]
        self.assertEqual((a["rank_in_state"], a["count_in_state"]), (1, 4))
        self.assertEqual(b["rank_in_state"], 2)
        self.assertEqual(c["rank_in_state"], 2)   # tie shares rank
        self.assertEqual(d["rank_in_state"], 4)   # competition ranking skips 3
        self.assertAlmostEqual(a["percentile_in_state"], 1.0, places=4)
        self.assertAlmostEqual(d["percentile_in_state"], 0.0, places=4)

    def test_nation_spans_all_states(self):
        states = self._multi()
        states["20"] = {"state": {"fips": "20", "alpha": "KS", "name": "KANSAS"},
                        "counties": {"010": _county("E", {"corn": _com("CORN", "corn",
                            [_series("YIELD", "BU / ACRE", {"2024": 300.0}, canonical=True)])})}}
        ranks = derived.compute_ranks(states)
        e = ranks[("20", "010", "corn")]["2024"]
        self.assertEqual(e["rank_in_state"], 1)        # alone in its state
        self.assertEqual(e["rank_in_nation"], 1)       # highest nationally
        self.assertEqual(e["count_in_nation"], 5)
        a = ranks[("19", "001", "corn")]["2024"]
        self.assertEqual(a["rank_in_nation"], 2)

    def test_single_county_percentile_is_one(self):
        states = {"19": {"state": {"fips": "19", "alpha": "IA", "name": "IOWA"},
                  "counties": {"001": _county("A", {"corn": _com("CORN", "corn",
                      [_series("YIELD", "BU / ACRE", {"2024": 100.0}, canonical=True)])})}}}
        r = derived.compute_ranks(states)[("19", "001", "corn")]["2024"]
        self.assertAlmostEqual(r["percentile_in_state"], 1.0, places=4)
        self.assertEqual(r["count_in_state"], 1)


class WeightedYieldTest(unittest.TestCase):
    def _two_counties(self):
        def cy(name, prod, area):
            return _county(name, {"corn": _com("CORN", "corn", [
                _series("PRODUCTION", "BU", {"2024": prod}, canonical=True),
                _series("AREA HARVESTED", "ACRES", {"2024": area}, canonical=True)])})
        return {"19": {"state": {"fips": "19", "alpha": "IA", "name": "IOWA"},
                       "counties": {"001": cy("A", 1000.0, 10.0),    # 100 bu/ac
                                    "002": cy("B", 1000.0, 5.0)}}}    # 200 bu/ac

    def test_state_weighted_yield(self):
        w = derived.compute_weighted_yield(self._two_counties())
        # (1000+1000)/(10+5) = 133.333...
        self.assertAlmostEqual(w[("19", "corn")]["state"]["2024"], 2000.0 / 15.0, places=4)
        self.assertAlmostEqual(w[("19", "corn")]["national"]["2024"], 2000.0 / 15.0, places=4)

    def test_county_missing_one_side_excluded(self):
        states = self._two_counties()
        # county 002 loses area harvested -> excluded from both sums
        com = states["19"]["counties"]["002"]["commodities"]["corn"]
        com["series"] = [s for s in com["series"] if s["statistic"] != "AREA HARVESTED"]
        w = derived.compute_weighted_yield(states)
        self.assertAlmostEqual(w[("19", "corn")]["state"]["2024"], 100.0, places=4)


class YieldStatsTest(unittest.TestCase):
    def _series_states(self, values):
        return {"19": {"state": {"fips": "19", "alpha": "IA", "name": "IOWA"},
                "counties": {"001": _county("A", {"corn": _com("CORN", "corn",
                    [_series("YIELD", "BU / ACRE", values, canonical=True)])})}}}

    def test_yoy_trailing_and_slope(self):
        vals = {str(y): float(100 + (y - 2015) * 10) for y in range(2015, 2025)}  # 100..190
        stats = derived.compute_yield_stats(self._series_states(vals))[("19", "001", "corn")]
        self.assertAlmostEqual(stats["yoy_pct"]["2016"], 10.0, places=2)       # 110 vs 100
        self.assertAlmostEqual(stats["slope_bu_per_year"], 10.0, places=4)     # perfect line
        # trailing 5yr at 2024 = mean(150,160,170,180,190) = 170
        self.assertAlmostEqual(stats["trailing_5yr_avg"]["2024"], 170.0, places=2)
        # trailing 10yr at 2024 = mean(100..190) = 145
        self.assertAlmostEqual(stats["trailing_10yr_avg"]["2024"], 145.0, places=2)

    def test_thresholds_and_gaps(self):
        # only 2 present in any 5yr window -> no 5yr avg; YoY skips the gap year;
        # 2 distinct years still define a slope.
        vals = {"2020": 100.0, "2024": 120.0}
        stats = derived.compute_yield_stats(self._series_states(vals))[("19", "001", "corn")]
        self.assertEqual(stats["trailing_5yr_avg"], {})
        self.assertNotIn("2021", stats["yoy_pct"])
        self.assertIn("slope_bu_per_year", stats)  # 2 distinct years -> slope defined


if __name__ == "__main__":
    unittest.main()
