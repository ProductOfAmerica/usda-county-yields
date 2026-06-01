"""Unit + integration tests for scripts/prices.py (SP-B state prices).

No network; synthetic fixtures built inline. Mirrors tests/test_refresh.py
and tests/test_planting_windows.py conventions.
"""
from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import prices  # noqa: E402
import refresh  # noqa: E402


def _row(**over) -> dict:
    base = {
        "SOURCE_DESC": "SURVEY", "COMMODITY_DESC": "CORN", "CLASS_DESC": "ALL CLASSES",
        "STATISTICCAT_DESC": "PRICE RECEIVED", "UNIT_DESC": "$ / BU",
        "AGG_LEVEL_DESC": "STATE", "STATE_FIPS_CODE": "19", "STATE_ALPHA": "IA",
        "STATE_NAME": "IOWA", "YEAR": "2024", "FREQ_DESC": "ANNUAL",
        "REFERENCE_PERIOD_DESC": "MARKETING YEAR", "VALUE": "4.80",
    }
    base.update(over)
    return base


def _filter(rows: list[dict]):
    text = io.StringIO()
    w = csv.writer(text, delimiter="\t")
    w.writerow(prices.REQUIRED_PRICE_COLS)
    for r in rows:
        w.writerow([r[c] for c in prices.REQUIRED_PRICE_COLS])
    text.seek(0)
    return prices.filter_prices(csv.reader(text, delimiter="\t"))


class FilterPricesTest(unittest.TestCase):
    def test_keeps_marketing_year_and_monthly(self):
        rows = [
            _row(),  # ANNUAL / MARKETING YEAR
            _row(FREQ_DESC="MONTHLY", REFERENCE_PERIOD_DESC="AUG", VALUE="5.20"),
        ]
        total, kept = _filter(rows)
        self.assertEqual(total, 2)
        self.assertEqual(len(kept), 2)

    def test_excludes_parity_after_report_prior(self):
        for sc in ("PRICE RECEIVED, PARITY", "PRICE RECEIVED AFTER REPORT",
                   "PRICE RECEIVED PRIOR TO CLOSING", "PRICE RECEIVED, 10 YEAR AVG"):
            _, kept = _filter([_row(STATISTICCAT_DESC=sc)])
            self.assertEqual(kept, [], sc)

    def test_excludes_non_dollar_unit_and_non_state_and_census(self):
        _, k1 = _filter([_row(UNIT_DESC="PCT OF PARITY")])
        _, k2 = _filter([_row(AGG_LEVEL_DESC="NATIONAL")])
        _, k3 = _filter([_row(SOURCE_DESC="CENSUS")])
        self.assertEqual((k1, k2, k3), ([], [], []))

    def test_excludes_non_allowlisted_commodity(self):
        _, kept = _filter([_row(COMMODITY_DESC="OATS")])
        self.assertEqual(kept, [])

    def test_excludes_annual_year_and_monthly_marketing_year(self):
        _, k1 = _filter([_row(FREQ_DESC="ANNUAL", REFERENCE_PERIOD_DESC="YEAR")])
        _, k2 = _filter([_row(FREQ_DESC="MONTHLY", REFERENCE_PERIOD_DESC="MARKETING YEAR")])
        self.assertEqual((k1, k2), ([], []))

    def test_missing_required_col_aborts(self):
        bad = [c for c in prices.REQUIRED_PRICE_COLS if c != "VALUE"]
        text = io.StringIO()
        text.write("\t".join(bad) + "\n")
        text.seek(0)
        with self.assertRaises(SystemExit):
            prices.filter_prices(csv.reader(text, delimiter="\t"))


if __name__ == "__main__":
    unittest.main()
