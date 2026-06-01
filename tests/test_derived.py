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


if __name__ == "__main__":
    unittest.main()
