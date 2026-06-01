# NASS Phase 2: State Price-Received family (SP-B)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or executing-plans. Steps use checkbox (`- [ ]`). This plan is executed INLINE, sequentially (no parallel subagents: they share one worktree and commit over each other).

**Goal:** Publish a new state-level price family `data/prices/states/{fips}/{crop}.json` from NASS PRICE RECEIVED rows, as a second pass over the same download (the SP-A `planting_windows.py` pattern), with its own schema + audit, a bootstrap sentinel, and a per-family Gate 2 baseline term, all added in this phase.

**Architecture:** New stdlib-only module `scripts/prices.py` mirroring `planting_windows.py`: `filter_prices` -> `group_prices` -> `mark_price_canonical` -> `emit_all` (writes shards + audit UNCONDITIONALLY) -> `run_prices` returning `PriceRunResult(paths, shard_count, kept_count)`. Wired into `refresh.main()` after SP-A and before `prune_stale`. A small generalization in `refresh.py` (`family_baseline`) supports the per-family Gate 2 term.

**Tech Stack:** Python 3.11 stdlib, `unittest`. Tests: `python -m unittest discover -s tests`.

**Spec:** `docs/superpowers/specs/2026-05-31-nass-prices-stats-derived-design.md` section 4.4 (and 4.7 for the bootstrap/baseline phasing rule).

**Base:** Foundation `3a7a60f` on main. Baseline: 121 tests pass.

**Verified price facts (live qs.crops_20260530, full scan):**
- `STATISTICCAT_DESC == "PRICE RECEIVED"` (exact) cleanly excludes the 5 PRICE variants (AFTER REPORT, PRIOR TO CLOSING, PARITY, 10 YEAR AVG, 10 YEAR AVG FOR PARITY).
- Keep = `PRICE RECEIVED` + `UNIT_DESC == "$ / BU"` + `AGG_LEVEL_DESC == "STATE"` + `SOURCE_DESC == "SURVEY"` + commodity in {CORN, SOYBEANS, WHEAT}.
- Two period shapes kept: `FREQ_DESC == "ANNUAL" and REFERENCE_PERIOD_DESC == "MARKETING YEAR"` (the recap-grade price) and `FREQ_DESC == "MONTHLY" and REFERENCE_PERIOD_DESC in {JAN..DEC}`. EXCLUDE `(ANNUAL, YEAR)` calendar-year prices and the ~337 `(MONTHLY, MARKETING YEAR)` oddities (out of the spec's two-series intent; recorded in audit).
- Wheat classes present: `ALL CLASSES` (the aggregate, canonical), `WINTER`, `SPRING, (EXCL DURUM)`, `SPRING, DURUM`. The token `"WHEAT"` has 0 rows. Corn/soy carry `ALL CLASSES` only.
- Canonical price series per (state, crop) = `class == "ALL CLASSES"` + period marketing-year. Wheat class variants are retained as additional, non-canonical series.

**Shard shape** (`data/prices/states/{fips}/{crop}.json`):
```json
{
  "schema_version": 3,
  "state": {"fips": "19", "alpha": "IA", "name": "IOWA"},
  "commodity": {"slug": "corn", "desc": "CORN"},
  "series": [
    {"class": "ALL CLASSES", "period": "MARKETING YEAR", "unit": "$ / BU",
     "canonical": true, "values": {"2024": 4.80}, "suppressed": {}},
    {"class": "ALL CLASSES", "period": "MONTHLY", "unit": "$ / BU",
     "values": {"2024-08": 5.20}, "suppressed": {}}
  ]
}
```
Monthly value keys are `YYYY-MM` (month token mapped JAN=01..DEC=12). Marketing-year keys are `YYYY`. `canonical` appears only on the ALL CLASSES marketing-year series.

---

### Task 1: `prices.py` constants + `filter_prices` (+ tests)

**Files:** Create `scripts/prices.py`; create `tests/test_prices.py`.

- [ ] **Step 1: failing test.** Create `tests/test_prices.py`:

```python
"""Tests for scripts/prices.py (SP-B state price-received family)."""
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
                   "PRICE RECEIVED PRIOR TO CLOSING"):
            _, kept = _filter([_row(STATISTICCAT_DESC=sc)])
            self.assertEqual(kept, [], sc)

    def test_excludes_non_dollar_unit_and_non_state(self):
        _, k1 = _filter([_row(UNIT_DESC="PCT OF PARITY")])
        _, k2 = _filter([_row(AGG_LEVEL_DESC="NATIONAL")])
        _, k3 = _filter([_row(SOURCE_DESC="CENSUS")])
        self.assertEqual((k1, k2, k3), ([], [], []))

    def test_excludes_annual_year_and_monthly_marketing_year(self):
        _, k1 = _filter([_row(FREQ_DESC="ANNUAL", REFERENCE_PERIOD_DESC="YEAR")])
        _, k2 = _filter([_row(FREQ_DESC="MONTHLY", REFERENCE_PERIOD_DESC="MARKETING YEAR")])
        self.assertEqual((k1, k2), ([], []))

    def test_missing_required_col_aborts(self):
        bad = [c for c in prices.REQUIRED_PRICE_COLS if c != "VALUE"]
        text = io.StringIO(); text.write("\t".join(bad) + "\n"); text.seek(0)
        with self.assertRaises(SystemExit):
            prices.filter_prices(csv.reader(text, delimiter="\t"))
```

Run: `python -m unittest tests.test_prices.FilterPricesTest -v` -> FAIL (no module `prices`).

- [ ] **Step 2: implement `scripts/prices.py` header + `filter_prices`:**

```python
#!/usr/bin/env python3
"""SP-B: NASS state price-received producer.

Second pass over the same qs.crops_*.txt.gz the yields refresh downloads.
Filters STATE-level SURVEY PRICE RECEIVED rows in $ / BU for corn/soybeans/
wheat, groups per (state, crop) into a marketing-year series and a monthly
series (per class), marks the ALL CLASSES marketing-year series canonical,
and emits sharded JSON + schema + audit. Stdlib only. See spec 4.4.
"""
from __future__ import annotations

import csv
import gzip
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import refresh  # lazy-safe: refresh imports this only inside main()

PRICE_UNIT = "$ / BU"
MONTH_NUM = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05", "JUN": "06",
    "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
}
# Canonical price series per (state, crop): the classless aggregate marketing
# year. Verified live: wheat aggregate is class "ALL CLASSES" (not "WHEAT");
# corn/soy carry only "ALL CLASSES".
CANONICAL_PRICE_CLASS = "ALL CLASSES"
CANONICAL_PRICE_PERIOD = "MARKETING YEAR"

REQUIRED_PRICE_COLS = [
    "SOURCE_DESC", "COMMODITY_DESC", "CLASS_DESC", "STATISTICCAT_DESC",
    "UNIT_DESC", "AGG_LEVEL_DESC", "STATE_FIPS_CODE", "STATE_ALPHA",
    "STATE_NAME", "YEAR", "FREQ_DESC", "REFERENCE_PERIOD_DESC", "VALUE",
]


@dataclass(frozen=True)
class PriceRunResult:
    paths: set
    shard_count: int
    kept_count: int


def _kept_period(freq: str, ref: str) -> Optional[str]:
    """Return the canonical period label for a kept row, or None to drop.

    Two shapes kept: ANNUAL/MARKETING YEAR and MONTHLY/<month token>.
    """
    if freq == "ANNUAL" and ref == "MARKETING YEAR":
        return "MARKETING YEAR"
    if freq == "MONTHLY" and ref in MONTH_NUM:
        return "MONTHLY"
    return None


def filter_prices(reader: Iterable[list[str]]) -> tuple[int, list[dict]]:
    """Second-pass filter over the bulk gz csv reader. Returns (total, kept).

    Raises SystemExit on missing required columns (Gate 1).
    """
    header = next(iter(reader))
    col = {name: i for i, name in enumerate(header)}
    missing = [c for c in REQUIRED_PRICE_COLS if c not in col]
    if missing:
        raise SystemExit(f"Required columns missing from NASS bulk file (PRICES): {missing}")
    total = 0
    kept: list[dict] = []
    for row in reader:
        total += 1
        try:
            if (row[col["SOURCE_DESC"]] != "SURVEY"
                    or row[col["STATISTICCAT_DESC"]] != "PRICE RECEIVED"
                    or row[col["UNIT_DESC"]] != PRICE_UNIT
                    or row[col["AGG_LEVEL_DESC"]] != "STATE"
                    or row[col["COMMODITY_DESC"]] not in refresh.COMMODITY_ALLOWLIST):
                continue
            period = _kept_period(row[col["FREQ_DESC"]], row[col["REFERENCE_PERIOD_DESC"]])
            if period is None:
                continue
            kept.append({
                "period": period,
                "commodity": row[col["COMMODITY_DESC"]],
                "class": row[col["CLASS_DESC"]],
                "state_fips": row[col["STATE_FIPS_CODE"]].zfill(2),
                "state_alpha": row[col["STATE_ALPHA"]],
                "state_name": row[col["STATE_NAME"]],
                "year": row[col["YEAR"]],
                "ref": row[col["REFERENCE_PERIOD_DESC"]],
                "value": row[col["VALUE"]],
            })
        except IndexError:
            continue
    return total, kept
```

Run the test class -> PASS.

- [ ] **Step 3: commit** `feat(prices): SP-B filter_prices (strict PRICE RECEIVED, $/bu, state)`.

---

### Task 2: `group_prices` (marketing-year + monthly series per class) (+ tests)

**Files:** `scripts/prices.py`, `tests/test_prices.py`.

- [ ] **Step 1: failing test:**

```python
class GroupPricesTest(unittest.TestCase):
    def test_marketing_year_and_monthly_series(self):
        _, kept = _filter([
            _row(YEAR="2024", VALUE="4.80"),
            _row(YEAR="2023", VALUE="5.45"),
            _row(FREQ_DESC="MONTHLY", REFERENCE_PERIOD_DESC="AUG", YEAR="2024", VALUE="5.20"),
        ])
        states = prices.group_prices(kept)
        com = states["19"]["corn"]
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
        classes = {s["class"] for s in states["19"]["wheat"]["series"]}
        self.assertEqual(classes, {"ALL CLASSES", "WINTER"})

    def test_suppressed_price_routed(self):
        _, kept = _filter([_row(VALUE="(D)")])
        s = prices.group_prices(kept)["19"]["corn"]["series"][0]
        self.assertEqual(s["values"], {})
        self.assertEqual(s["suppressed"], {"2024": "D"})
```

Run -> FAIL (`group_prices` missing).

- [ ] **Step 2: implement `group_prices` and the slug helper:**

```python
def _slug(commodity: str) -> str:
    return refresh.slugify(commodity)


def group_prices(kept: list[dict]) -> dict:
    """Nest kept rows: state_fips -> crop_slug -> {state, commodity_desc,
    series[]}. One series per (class, period); values keyed by YYYY
    (marketing year) or YYYY-MM (monthly). Suppressed values land in a
    parallel suppressed map (last-write-wins on duplicate keys).
    """
    states: dict = {}
    for r in kept:
        fips = r["state_fips"]
        slug = _slug(r["commodity"])
        st = states.setdefault(fips, {
            "state": {"fips": fips, "alpha": r["state_alpha"], "name": r["state_name"]},
            "crops": {},
        })
        com = st["crops"].setdefault(slug, {"commodity_desc": r["commodity"], "series": []})
        key = (r["class"], r["period"])
        series = next(
            (s for s in com["series"] if (s["class"], s["period"]) == key), None)
        if series is None:
            series = {"class": r["class"], "period": r["period"], "unit": PRICE_UNIT,
                      "values": {}, "suppressed": {}}
            com["series"].append(series)
        if r["period"] == "MONTHLY":
            vkey = f"{r['year']}-{MONTH_NUM[r['ref']]}"
        else:
            vkey = r["year"]
        value, code, _raw = refresh.parse_value(r["value"])
        if value is not None:
            series["values"][vkey] = value
        elif code is not None:
            series["suppressed"][vkey] = code
    return states
```

Run -> PASS.

- [ ] **Step 3: commit** `feat(prices): group_prices into per-(state,crop) marketing-year + monthly series`.

---

### Task 3: canonical marking, shape assert, sorting (+ tests)

**Files:** `scripts/prices.py`, `tests/test_prices.py`.

- [ ] **Step 1: failing test:**

```python
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
        com = states["19"]["corn"]
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

    def test_shape_assert_rejects_bad(self):
        with self.assertRaises(SystemExit):
            prices._assert_price_shape({"schema_version": 2})
```

Run -> FAIL.

- [ ] **Step 2: implement sort, mark, assert:**

```python
def _series_sort_key(s: dict) -> tuple:
    return (s["class"], s["period"])


def sort_price_series(states: dict) -> None:
    for st in states.values():
        for com in st["crops"].values():
            com["series"].sort(key=_series_sort_key)


def mark_price_canonical(states: dict) -> tuple[int, list[tuple[str, str]]]:
    """Mark the ALL CLASSES marketing-year series canonical per (state, crop).

    Returns (missing_count, samples): (state, crop) pairs that have price
    series but no ALL CLASSES marketing-year series. (class, period) is a
    unique grouping key, so at most one series matches; no ambiguity abort.
    """
    missing = 0
    samples: list[tuple[str, str]] = []
    for fips, st in states.items():
        for slug, com in st["crops"].items():
            match = next(
                (s for s in com["series"]
                 if s["class"] == CANONICAL_PRICE_CLASS and s["period"] == CANONICAL_PRICE_PERIOD),
                None)
            if match is not None:
                match["canonical"] = True
            elif com["series"]:
                missing += 1
                if len(samples) < 10:
                    samples.append((fips, slug))
    return missing, samples


def _assert_price_shape(shard: dict) -> None:
    top = {"schema_version", "state", "commodity", "series"}
    if set(shard) != top:
        raise SystemExit(f"Price shard top-level keys mismatch: {sorted(set(shard))}")
    if shard["schema_version"] != 3:
        raise SystemExit(f"Price shard schema_version not 3: {shard['schema_version']!r}")
    required = {"class", "period", "unit", "values", "suppressed"}
    optional = {"canonical"}
    for s in shard["series"]:
        keys = set(s)
        if required - keys:
            raise SystemExit(f"Price series missing keys: {sorted(required - keys)}")
        if keys - required - optional:
            raise SystemExit(f"Price series unexpected keys: {sorted(keys - required - optional)}")
```

Run -> PASS.

- [ ] **Step 3: commit** `feat(prices): canonical (ALL CLASSES marketing-year) + shape assert + sort`.

---

### Task 4: paths, `emit_all`, `run_prices`, `price.json` schema (+ tests)

**Files:** `scripts/prices.py`, `data/_schema/price.json`, `tests/test_prices.py`.

- [ ] **Step 1: failing test:**

```python
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
        self.assertEqual(audit["product_name"], "NASS state prices received")

    def test_emit_writes_audit_even_with_zero_shards(self):
        disc = {"url": "u", "etag": '"e"', "date": "2026-05-30"}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(refresh, "DATA_DIR", Path(td)):
                prices.emit_all({}, disc, "2026-05-30T00:00:00Z")
                self.assertTrue((Path(td) / "_audit" / "prices.json").exists())

    def test_schema_file_is_v3(self):
        p = Path(refresh.DATA_DIR) / "_schema" / "price.json"
        self.assertTrue(p.exists())
        sch = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(sch["properties"]["schema_version"].get("const"), 3)
```

Run -> FAIL.

- [ ] **Step 2: implement paths + emit_all + run_prices:**

```python
def _data_dir() -> Path:
    return refresh.DATA_DIR


def _shard_path(fips: str, slug: str) -> Path:
    return _data_dir() / "prices" / "states" / fips / f"{slug}.json"


def _audit_path() -> Path:
    return _data_dir() / "_audit" / "prices.json"


def _schema_path() -> Path:
    return _data_dir() / "_schema" / "price.json"


def prices_bootstrap_needed() -> bool:
    """True when the price audit sentinel is absent (same-publication re-emit)."""
    return not _audit_path().exists()


def emit_all(states: dict, discovery: dict, refreshed_at: str) -> set[Path]:
    """Write per-(state, crop) shards + audit. Audit is written UNCONDITIONALLY
    (even zero shards) so the bootstrap sentinel always clears. Returns the
    protected path set (schema + shards + audit) for prune_stale.
    """
    paths: set[Path] = {_schema_path()}
    shard_count = 0
    for fips in sorted(states):
        st = states[fips]
        for slug in sorted(st["crops"]):
            com = st["crops"][slug]
            shard = {
                "schema_version": 3,
                "state": st["state"],
                "commodity": {"slug": slug, "desc": com["commodity_desc"]},
                "series": com["series"],
            }
            _assert_price_shape(shard)
            p = _shard_path(fips, slug)
            refresh.write_if_changed(p, refresh._dump_json(shard))
            paths.add(p)
            shard_count += 1
    audit = {
        "product_name": "NASS state prices received",
        "refreshed_at": refreshed_at,
        "source": {"url": discovery["url"], "etag": discovery["etag"],
                   "publication_date": discovery["date"]},
        "unit": PRICE_UNIT,
        "periods": ["MARKETING YEAR", "MONTHLY"],
        "shard_count": shard_count,
    }
    ap = _audit_path()
    refresh.write_if_changed(ap, refresh._dump_json(audit))
    paths.add(ap)
    return paths


def run_prices(download_path: Path, discovery: dict, refreshed_at: str,
               baseline: Optional[int]) -> PriceRunResult:
    """SP-B entrypoint, called from refresh.main() after the shared download."""
    with gzip.open(download_path, "rt", encoding="utf-8", newline="") as f:
        total, kept = filter_prices(csv.reader(f, delimiter="\t"))
    if total == 0:
        raise SystemExit("SP-B: bulk file produced 0 rows. Aborting.")
    _validate_band(len(kept), baseline)
    states = group_prices(kept)
    sort_price_series(states)
    missing, samples = mark_price_canonical(states)
    if missing:
        s = ", ".join(f"{f}/{c}" for f, c in samples)
        print(f"SP-B prices: {missing} (state, crop) pairs lack a canonical "
              f"marketing-year price. First {len(samples)}: {s}", file=sys.stderr)
    paths = emit_all(states, discovery, refreshed_at)
    print(f"SP-B prices: kept={len(kept)} shards={sum(len(st['crops']) for st in states.values())}",
          file=sys.stderr)
    shard_count = sum(len(st["crops"]) for st in states.values())
    return PriceRunResult(paths=paths, shard_count=shard_count, kept_count=len(kept))


def _validate_band(kept: int, baseline: Optional[int]) -> None:
    """Per-family Gate 2: +/-10% band vs prior price-row count. Bootstrap-
    tolerant (baseline None) and zero-tolerant (a legitimately empty price
    family is a valid published state, per spec 4.7's zero-shard invariant).
    """
    if baseline is None or baseline == 0:
        return
    delta = abs(kept - baseline) / baseline
    if delta > refresh.ROW_COUNT_TOLERANCE:
        raise SystemExit(
            f"SP-B price row count {kept} differs from baseline {baseline} by "
            f"{delta:.1%} (>{refresh.ROW_COUNT_TOLERANCE:.0%}). Aborting.")
```

- [ ] **Step 3: create `data/_schema/price.json`** (JSON Schema 2020-12 matching `_assert_price_shape`):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/_schema/price.json",
  "title": "NASS state price-received shard (v3)",
  "description": "One shard per (state, crop). State-level PRICE RECEIVED in $ / BU, as a marketing-year series and a monthly series per class. The ALL CLASSES marketing-year series is canonical.",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "state", "commodity", "series"],
  "properties": {
    "schema_version": {"const": 3},
    "state": {
      "type": "object", "additionalProperties": false,
      "required": ["fips", "alpha", "name"],
      "properties": {
        "fips": {"type": "string", "pattern": "^[0-9]{2}$"},
        "alpha": {"type": "string", "pattern": "^[A-Z]{2}$"},
        "name": {"type": "string"}
      }
    },
    "commodity": {
      "type": "object", "additionalProperties": false,
      "required": ["slug", "desc"],
      "properties": {"slug": {"type": "string"}, "desc": {"type": "string"}}
    },
    "series": {
      "type": "array",
      "items": {
        "type": "object", "additionalProperties": false,
        "required": ["class", "period", "unit", "values", "suppressed"],
        "properties": {
          "class": {"type": "string"},
          "period": {"type": "string", "enum": ["MARKETING YEAR", "MONTHLY"]},
          "unit": {"const": "$ / BU"},
          "canonical": {"type": "boolean"},
          "values": {"type": "object", "additionalProperties": {"type": "number"}},
          "suppressed": {"type": "object", "additionalProperties": {"type": "string"}}
        }
      }
    }
  }
}
```

Run the test class -> PASS.

- [ ] **Step 4: commit** `feat(prices): emit_all + run_prices + price.json schema (zero-shard sentinel)`.

---

### Task 5: wire `run_prices` into `refresh.main()` (+ tests)

**Files:** `scripts/refresh.py`, `tests/test_refresh.py`.

Context: in `refresh.main()` today, SP-A is wired at ~lines 752 (`bootstrap_needed`), 814-833 (`expected` set + `run_planting_windows`), 845-850 (`save_state`). Prices wires in the same three places. Re-read those exact lines before editing; line numbers drift.

- [ ] **Step 1: failing test** in `tests/test_refresh.py`:

```python
class FamilyBaselineTest(unittest.TestCase):
    def test_family_baseline_leaf_and_prices(self):
        st = {"last_filtered_row_count": {"leaf": 100, "prices": 50}}
        self.assertEqual(refresh.family_baseline(st, "leaf"), 100)
        self.assertEqual(refresh.family_baseline(st, "prices"), 50)

    def test_family_baseline_legacy_scalar_is_none(self):
        self.assertIsNone(refresh.family_baseline({"last_filtered_row_count": 9}, "prices"))

    def test_family_baseline_absent_is_none(self):
        self.assertIsNone(refresh.family_baseline({}, "prices"))
```

Run -> FAIL (`family_baseline` missing).

- [ ] **Step 2: add `family_baseline` and route `leaf_baseline` through it.** In `refresh.py`, replace the `leaf_baseline` function with:

```python
def family_baseline(state: dict, family: str) -> Optional[int]:
    """Per-family Gate 2 baseline. Returns None (bootstrap) when absent or in
    the legacy scalar shape; the int for {family: N}."""
    counts = state.get("last_filtered_row_count")
    if isinstance(counts, dict):
        return counts.get(family)
    return None


def leaf_baseline(state: dict) -> Optional[int]:
    return family_baseline(state, "leaf")
```

- [ ] **Step 3: wire prices into `main()`.** Re-read main() first. Three edits:

(a) Bootstrap sentinel, change the `bootstrap_needed` line to OR in prices:
```python
    bootstrap_needed = (
        not _index_path().exists()
        or sp_a_bootstrap_needed()
        or _prices_bootstrap_needed()
    )
```
and add near the SP-A lazy import area a module-level helper used before the heavy import is available. Since `prices` is imported lazily later, add a tiny local indirection: define at top of `refresh.py` (after `_sp_a_audit_path`):
```python
def _prices_bootstrap_needed() -> bool:
    return not (DATA_DIR / "_audit" / "prices.json").exists()
```
(This mirrors `sp_a_bootstrap_needed()` and avoids importing prices just to check a path.)

(b) After the SP-A block (`expected |= sp_a.paths`), add the prices second pass:
```python
    import prices  # lazy: avoids circular import at module load
    price_result = prices.run_prices(
        download_path, discovery, refreshed_at, family_baseline(state, "prices")
    )
    expected |= price_result.paths
```

(c) In the `save_state({...})` payload, change the baseline map and add the price count:
```python
        "last_filtered_row_count": {"leaf": len(kept_rows), "prices": price_result.kept_count},
        ...
        "last_price_shard_count": price_result.shard_count,
```

- [ ] **Step 4: full suite** `python -m unittest discover -s tests` -> all green. The SP-A `MainIntegrationTest` stubs `discover/download_with_retry/stream_filter`; it will now also reach `prices.run_prices`, which opens `download_path`. That test stubs `stream_filter` but `run_prices` calls `gzip.open(download_path)` directly. Re-read `MainIntegrationTest`: it writes a real gz to `download_path` (via the stubbed download). If it does NOT, stub `prices.run_prices` in that test to return a `PriceRunResult(set(), 0, 0)`, matching how it already handles SP-A. Apply whichever keeps it hermetic; prefer stubbing `prices.run_prices` like SP-A is stubbed.

- [ ] **Step 5: commit** `feat(refresh): wire SP-B prices into main (bootstrap sentinel, baseline, prune set)`.

---

### Task 6: integration test + prune protection (+ commit)

**Files:** `tests/test_prices.py`.

- [ ] **Step 1: integration test** driving filter -> group -> sort -> mark -> emit on a multi-crop, multi-class fixture, asserting the wheat ALL CLASSES marketing-year is canonical and WINTER is not, monthly keys are `YYYY-MM`, and a suppressed price lands in `suppressed`:

```python
class PricesIntegrationTest(unittest.TestCase):
    def test_end_to_end_corn_and_wheat(self):
        rows = [
            _row(VALUE="4.80"),  # corn ALL CLASSES MY
            _row(FREQ_DESC="MONTHLY", REFERENCE_PERIOD_DESC="AUG", VALUE="5.20"),
            _row(COMMODITY_DESC="WHEAT", CLASS_DESC="ALL CLASSES", VALUE="6.10"),
            _row(COMMODITY_DESC="WHEAT", CLASS_DESC="WINTER", VALUE="6.25"),
            _row(COMMODITY_DESC="WHEAT", CLASS_DESC="ALL CLASSES",
                 FREQ_DESC="MONTHLY", REFERENCE_PERIOD_DESC="SEP", VALUE="(D)"),
        ]
        _, kept = _filter(rows)
        states = prices.group_prices(kept)
        prices.sort_price_series(states)
        missing, _ = prices.mark_price_canonical(states)
        self.assertEqual(missing, 0)
        wheat = states["19"]["wheat"]
        canon = [s for s in wheat["series"] if s.get("canonical")]
        self.assertEqual(len(canon), 1)
        self.assertEqual(canon[0]["class"], "ALL CLASSES")
        winter = [s for s in wheat["series"] if s["class"] == "WINTER"]
        self.assertFalse(winter[0].get("canonical"))
        sep = next(s for s in wheat["series"]
                   if s["class"] == "ALL CLASSES" and s["period"] == "MONTHLY")
        self.assertEqual(sep["suppressed"], {"2024-09": "D"})
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(refresh, "DATA_DIR", Path(td)):
                prices.emit_all(states, {"url": "u", "etag": '"e"', "date": "2026-05-30"},
                                "2026-05-30T00:00:00Z")
                corn = json.loads((Path(td) / "prices" / "states" / "19" / "corn.json").read_text())
        mo = next(s for s in corn["series"] if s["period"] == "MONTHLY")
        self.assertEqual(mo["values"], {"2024-08": 5.20})
```

Run -> PASS, then full suite green.

- [ ] **Step 2: commit** `test(prices): SP-B end-to-end integration (wheat classes, monthly keys, suppressed)`.

---

## Self-review notes
- `family_baseline` generalizes `leaf_baseline` (kept as a thin wrapper) so Foundation tests stay green; prices uses `family_baseline(state, "prices")`.
- `run_prices` is zero-tolerant (the spec's zero-shard sentinel invariant): `_validate_band` skips on baseline None/0, and `emit_all` writes the audit even with zero shards.
- The prices bootstrap term is added in THIS phase (spec 4.7 phasing rule), via `_prices_bootstrap_needed()` checking `_audit/prices.json`.
- Out of scope: `(ANNUAL, YEAR)` calendar-year prices, `(MONTHLY, MARKETING YEAR)` oddities, national prices, derived revenue (phase 3).
- `prune_stale` already deletes any `data/**/*.json` not in `expected`; the prices paths join `expected` via Task 5(b), so they survive prune. A test in `test_refresh.py` is not strictly needed (covered by the existing prune test honoring `expected`), but the integration of price paths into `expected` is the load-bearing line.
