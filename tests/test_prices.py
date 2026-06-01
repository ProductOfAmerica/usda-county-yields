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


class GroupPricesTest(unittest.TestCase):
    def test_marketing_year_and_monthly_series(self):
        _, kept = _filter([
            _row(YEAR="2024", VALUE="4.80"),
            _row(YEAR="2023", VALUE="5.45"),
            _row(FREQ_DESC="MONTHLY", REFERENCE_PERIOD_DESC="AUG", YEAR="2024", VALUE="5.20"),
        ])
        states = prices.group_prices(kept)
        com = states["19"]["crops"]["corn"]
        my = next(s for s in com["series"] if s["period"] == "MARKETING YEAR")
        mo = next(s for s in com["series"] if s["period"] == "MONTHLY")
        self.assertEqual(my["values"], {"2024": 4.80, "2023": 5.45})
        self.assertEqual(mo["values"], {"2024-08": 5.20})

    def test_wheat_classes_separate_series(self):
        _, kept = _filter([
            _row(COMMODITY_DESC="WHEAT", CLASS_DESC="ALL CLASSES", VALUE="6.10"),
            _row(COMMODITY_DESC="WHEAT", CLASS_DESC="WINTER", VALUE="6.25"),
        ])
        states = prices.group_prices(kept)
        classes = {s["class"] for s in states["19"]["crops"]["wheat"]["series"]}
        self.assertEqual(classes, {"ALL CLASSES", "WINTER"})

    def test_suppressed_price_routed(self):
        _, kept = _filter([_row(VALUE="(D)")])
        s = prices.group_prices(kept)["19"]["crops"]["corn"]["series"][0]
        self.assertEqual(s["values"], {})
        self.assertEqual(s["suppressed"], {"2024": "D"})


class CanonicalPriceTest(unittest.TestCase):
    def test_marks_all_classes_marketing_year(self):
        _, kept = _filter([
            _row(VALUE="4.80"),  # ALL CLASSES / MARKETING YEAR
            _row(FREQ_DESC="MONTHLY", REFERENCE_PERIOD_DESC="AUG", VALUE="5.20"),
        ])
        states = prices.group_prices(kept)
        prices.sort_price_series(states)
        missing, _ = prices.mark_price_canonical(states)
        self.assertEqual(missing, 0)
        com = states["19"]["crops"]["corn"]
        canon = [s for s in com["series"] if s.get("canonical")]
        self.assertEqual(len(canon), 1)
        self.assertEqual((canon[0]["class"], canon[0]["period"]),
                         ("ALL CLASSES", "MARKETING YEAR"))

    def test_missing_canonical_counted_when_no_marketing_year(self):
        _, kept = _filter([_row(FREQ_DESC="MONTHLY", REFERENCE_PERIOD_DESC="AUG", VALUE="5.2")])
        states = prices.group_prices(kept)
        prices.sort_price_series(states)
        missing, _ = prices.mark_price_canonical(states)
        self.assertEqual(missing, 1)

    def test_wheat_winter_not_canonical(self):
        _, kept = _filter([
            _row(COMMODITY_DESC="WHEAT", CLASS_DESC="ALL CLASSES", VALUE="6.10"),
            _row(COMMODITY_DESC="WHEAT", CLASS_DESC="WINTER", VALUE="6.25"),
        ])
        states = prices.group_prices(kept)
        prices.sort_price_series(states)
        prices.mark_price_canonical(states)
        wheat = states["19"]["crops"]["wheat"]
        canon = [s for s in wheat["series"] if s.get("canonical")]
        self.assertEqual([s["class"] for s in canon], ["ALL CLASSES"])

    def test_shape_assert_rejects_bad_version(self):
        with self.assertRaises(SystemExit):
            prices._assert_price_shape({"schema_version": 2})


class EmitPricesTest(unittest.TestCase):
    def _states(self):
        _, kept = _filter([
            _row(VALUE="4.80"),
            _row(FREQ_DESC="MONTHLY", REFERENCE_PERIOD_DESC="AUG", VALUE="5.20"),
        ])
        states = prices.group_prices(kept)
        prices.sort_price_series(states)
        prices.mark_price_canonical(states)
        return states

    def test_emit_writes_shard_and_audit_at_v3(self):
        disc = {"url": "u", "etag": '"e"', "date": "2026-05-30"}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(refresh, "DATA_DIR", Path(td)):
                paths = prices.emit_all(self._states(), disc, "2026-05-30T00:00:00Z")
                shard = json.loads((Path(td) / "prices" / "states" / "19" / "corn.json").read_text())
                audit = json.loads((Path(td) / "_audit" / "prices.json").read_text())
        self.assertEqual(shard["schema_version"], 3)
        self.assertEqual(shard["commodity"]["slug"], "corn")
        self.assertIn(Path(td) / "_audit" / "prices.json", paths)
        self.assertIn(Path(td) / "_schema" / "price.json", paths)
        self.assertEqual(audit["product_name"], "NASS state prices received")

    def test_emit_writes_audit_even_with_zero_shards(self):
        disc = {"url": "u", "etag": '"e"', "date": "2026-05-30"}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(refresh, "DATA_DIR", Path(td)):
                prices.emit_all({}, disc, "2026-05-30T00:00:00Z")
                self.assertTrue((Path(td) / "_audit" / "prices.json").exists())

    def test_schema_file_is_v3(self):
        p = Path(refresh.DATA_DIR) / "_schema" / "price.json"
        self.assertTrue(p.exists(), "data/_schema/price.json must exist")
        sch = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(sch["properties"]["schema_version"].get("const"), 3)

    def test_run_prices_returns_counts(self):
        # Build a tiny gz the same way the real download would land, run end to
        # end against a tempdir DATA_DIR.
        import gzip as _gz
        rows = [
            _row(VALUE="4.80"),
            _row(FREQ_DESC="MONTHLY", REFERENCE_PERIOD_DESC="AUG", VALUE="5.20"),
            _row(COMMODITY_DESC="WHEAT", CLASS_DESC="WINTER", VALUE="6.25"),
        ]
        with tempfile.TemporaryDirectory() as td:
            gz = Path(td) / "qs.crops.txt.gz"
            with _gz.open(gz, "wt", encoding="utf-8", newline="") as f:
                w = csv.writer(f, delimiter="\t")
                w.writerow(prices.REQUIRED_PRICE_COLS)
                for r in rows:
                    w.writerow([r[c] for c in prices.REQUIRED_PRICE_COLS])
            with mock.patch.object(refresh, "DATA_DIR", Path(td) / "data"):
                res = prices.run_prices(gz, {"url": "u", "etag": '"e"', "date": "2026-05-30"},
                                        "2026-05-30T00:00:00Z", baseline=None)
        self.assertEqual(res.kept_count, 3)
        self.assertEqual(res.shard_count, 2)  # corn + wheat

    def test_run_prices_band_abort(self):
        import gzip as _gz
        with tempfile.TemporaryDirectory() as td:
            gz = Path(td) / "q.gz"
            with _gz.open(gz, "wt", encoding="utf-8", newline="") as f:
                w = csv.writer(f, delimiter="\t")
                w.writerow(prices.REQUIRED_PRICE_COLS)
                w.writerow([_row()[c] for c in prices.REQUIRED_PRICE_COLS])  # 1 kept
            with mock.patch.object(refresh, "DATA_DIR", Path(td) / "data"):
                with self.assertRaises(SystemExit):
                    prices.run_prices(gz, {"url": "u", "etag": '"e"', "date": "2026-05-30"},
                                      "2026-05-30T00:00:00Z", baseline=100)


if __name__ == "__main__":
    unittest.main()
