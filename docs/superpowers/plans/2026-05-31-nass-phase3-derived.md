# NASS Phase 3: Derived families (SP-C)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or executing-plans. Steps use checkbox (`- [ ]`). Execute INLINE, sequentially. NEVER dispatch parallel subagents into this one worktree: they commit over each other.

**Goal:** Publish precomputed derived families from the data already grouped in memory during a refresh: per-(county, crop) imputed revenue/acre + yield trend stats + rank/percentile (`data/derived/{fips}/counties/{code}/{crop}.json`), and per-(state, crop) production-weighted yield + a county comparison scan (`data/states/{fips}/derived/state-{crop}.json`). No third parse of the bulk file.

**Architecture:** New stdlib-only module `scripts/derived.py` mirroring `prices.py`/`planting_windows.py`: pure compute functions over the in-memory `states` (county leaves, canonical already marked) and `price_states` (from the SP-B result) -> `emit_all` (writes both shard families + audit UNCONDITIONALLY) -> `run_derived(states, price_states, discovery, refreshed_at, baseline)` returning `DerivedRunResult(paths, shard_count, kept_count)`. Wired into `refresh.main()` after SP-B and before `prune_stale`. `prices.run_prices` is extended to return its grouped `price_states` (additive field) so derived consumes it without re-filtering.

**Tech Stack:** Python 3.11 stdlib, `unittest`. Tests: `python -m unittest discover -s tests`.

**Spec:** `docs/superpowers/specs/2026-05-31-nass-prices-stats-derived-design.md` sections 4.5 (the five derived families; 4.5e bundle is Phase 4), 4.6 (marketing-year join), 4.7 (per-family Gate 2 baseline, bootstrap sentinel, zero-shard invariant).

**Base:** `84a69c7` on main (Phase 2 merged). Baseline: 144 tests pass.

**What derived reads (all in memory, canonical already marked by `refresh.mark_canonical`):**
- County canonical series per statistic: helper `_canonical(com, statistic)` returns the series with `s.get("canonical") and s["statistic"] == statistic`, else None. Values are `series["values"]` (year -> float); suppressed years are absent from `values` (in `series["suppressed"]`), so "skip suppressed" == "iterate `values`".
- State canonical marketing-year price: `price_states[fips]["crops"][slug]["series"]`, the entry with `s.get("canonical")` (which `mark_price_canonical` set on ALL CLASSES / MARKETING YEAR). Values are `series["values"]` (year -> $/bu).

**Marketing-year join (spec 4.6):** county `yield[Y]` joins to the price marketing-year row labelled `Y`, for all three crops (NASS labels the marketing year by its start year, which equals the harvest year here). Encode the identity explicitly as `MARKETING_YEAR_LABEL = {"corn": 0, "soybeans": 0, "wheat": 0}` (offset added to the yield year), so a future spring/southern-hemisphere crop does not blindly reuse it. The derived record carries both `marketing_year` and the yield year (the map key) so the join is auditable.

**Math (verified definitions, each pinned by a test):**
- `revenue_per_harvested_acre[Y] = yield[Y] * price[Y]`. Requires canonical YIELD value and price value for the joined marketing year. Units: (BU/ACRE)*($/BU) = $/ACRE.
- `revenue_per_planted_acre[Y] = production[Y] * price[Y] / area_planted[Y]`. Requires canonical PRODUCTION and canonical AREA PLANTED values and `area_planted[Y] > 0`. Units: (BU)*($/BU)/(ACRES) = $/ACRE. Emitted only when those inputs exist for `Y`; per-harvested can exist for a year where per-planted does not.
- `production_weighted_yield[Y] = sum_counties(production[Y]) / sum_counties(area_harvested[Y])`, including only counties where BOTH canonical PRODUCTION and canonical AREA HARVESTED have a non-suppressed value for `Y`. Computed for the state (counties in that state) and the nation (all counties). Units BU/ACRE.
- Rank/percentile per (crop, year), on canonical YIELD: competition rank, 1 = highest, `rank = 1 + count(strictly greater yields)`; ties share a rank. `percentile = round((n - rank) / (n - 1), 4)`, with `percentile = 1.0` when `n == 1`. Computed within-state (counties in that state with a yield value for `Y`) and within-nation (all counties with a yield value for `Y`).
- `yoy_pct[Y] = round((yield[Y] - yield[Y-1]) / yield[Y-1] * 100, 2)`, only where both years present and `yield[Y-1] != 0`.
- `trailing_5yr_avg[Y]` = mean of present yield values in years `[Y-4 .. Y]`; emitted only if at least `TRAILING_MIN_5 = 3` present. `trailing_10yr_avg[Y]` over `[Y-9 .. Y]`, emitted if at least `TRAILING_MIN_10 = 5` present. Rounded to 2 decimals.
- `slope_bu_per_year` = ordinary-least-squares slope of (year, yield) over all present points, `cov(year, yield) / var(year)`; emitted only with >= 2 distinct years. Rounded to 4 decimals.

**Shapes**

`data/derived/{fips}/counties/{code}/{slug}.json` (derived-county):
```json
{
  "schema_version": 3,
  "state": {"fips": "19", "alpha": "IA", "name": "IOWA"},
  "county": {"code": "169", "name": "STORY"},
  "commodity": {"slug": "corn", "desc": "CORN"},
  "revenue": {
    "2024": {"marketing_year": "2024", "yield": 215.5, "price": 4.80,
             "revenue_per_harvested_acre": 1034.4, "revenue_per_planted_acre": 1010.2}
  },
  "yield_trend": {
    "slope_bu_per_year": 1.85,
    "yoy_pct": {"2024": 3.2},
    "trailing_5yr_avg": {"2024": 201.4},
    "trailing_10yr_avg": {"2024": 195.0}
  },
  "rank": {
    "2024": {"rank_in_state": 12, "count_in_state": 99, "percentile_in_state": 0.8878,
             "rank_in_nation": 145, "count_in_nation": 2100, "percentile_in_nation": 0.9314}
  }
}
```
Top-level `revenue`/`yield_trend`/`rank` are always present (possibly with empty maps / absent optional `slope_bu_per_year` / absent optional `revenue_per_planted_acre`). A county+crop shard is emitted only when a canonical YIELD series exists (so `rank`/`yield_trend` have at least the year's own data); otherwise no shard.

`data/states/{fips}/derived/state-{slug}.json` (derived-state, the comparison scan):
```json
{
  "schema_version": 3,
  "state": {"fips": "19", "alpha": "IA", "name": "IOWA"},
  "commodity": {"slug": "corn", "desc": "CORN"},
  "production_weighted_yield": {"state": {"2024": 198.3}, "national": {"2024": 177.0}},
  "counties": {
    "169": {"name": "STORY",
            "yield": {"2024": 215.5},
            "rank": {"2024": {"rank_in_state": 12, "count_in_state": 99, "percentile_in_state": 0.8878,
                              "rank_in_nation": 145, "count_in_nation": 2100, "percentile_in_nation": 0.9314}}}
  }
}
```
`national` block repeats per state file (small; self-contained for state-vs-national). Rank/percentile is computed once across all counties and denormalized into both shard families.

**Pipeline / safety (spec 4.7):** Audit `data/_audit/derived.json` written UNCONDITIONALLY (zero-shard sentinel). `derived_bootstrap_needed()` (checks the audit) is ORed into `refresh.main`'s `bootstrap_needed` IN THIS PHASE (when the emitter exists). Per-family Gate 2 via `family_baseline(state, "derived")`, bootstrap- and zero-tolerant. Schemas `data/_schema/derived-county.json` + `derived-state.json` are committed static artifacts, protected from prune (added to the returned paths), never written by the refresh.

---

### Task 1: `prices.run_prices` returns grouped `price_states` (additive)

Derived needs the grouped price data in memory. Extend the SP-B result with an additive field (default keeps the existing test stub valid).

**Files:** Modify `scripts/prices.py`; modify `tests/test_prices.py`.

- [ ] **Step 1: failing test.** Append to `tests/test_prices.py` inside `EmitPricesTest` (after `test_run_prices_returns_counts`):

```python
    def test_run_prices_returns_grouped_states(self):
        import gzip as _gz
        rows = [_row(VALUE="4.80"),
                _row(FREQ_DESC="MONTHLY", REFERENCE_PERIOD_DESC="AUG", VALUE="5.20")]
        with tempfile.TemporaryDirectory() as td:
            gz = Path(td) / "q.gz"
            with _gz.open(gz, "wt", encoding="utf-8", newline="") as f:
                w = csv.writer(f, delimiter="\t")
                w.writerow(prices.REQUIRED_PRICE_COLS)
                for r in rows:
                    w.writerow([r[c] for c in prices.REQUIRED_PRICE_COLS])
            with mock.patch.object(refresh, "DATA_DIR", Path(td) / "data"):
                res = prices.run_prices(gz, {"url": "u", "etag": '"e"', "date": "2026-05-30"},
                                        "2026-05-30T00:00:00Z", baseline=None)
        self.assertIn("19", res.price_states)
        corn = res.price_states["19"]["crops"]["corn"]
        my = next(s for s in corn["series"] if s["period"] == "MARKETING YEAR")
        self.assertTrue(my.get("canonical"))
```

- [ ] **Step 2: run, expect fail** (`AttributeError: ... has no attribute 'price_states'`).

Run: `python -m unittest tests.test_prices.EmitPricesTest.test_run_prices_returns_grouped_states -v`

- [ ] **Step 3: implement.** In `scripts/prices.py`, extend the dataclass and import `field`:

```python
from dataclasses import dataclass, field
```
```python
@dataclass(frozen=True)
class PriceRunResult:
    paths: set
    shard_count: int
    kept_count: int
    price_states: dict = field(default_factory=dict)
```
And in `run_prices`, change the final return to pass the grouped states:
```python
    return PriceRunResult(paths=paths, shard_count=shard_count,
                          kept_count=len(kept), price_states=states)
```

- [ ] **Step 4: run, expect pass.** Then full file: `python -m unittest tests.test_prices -v` (expect OK).

- [ ] **Step 5: commit.**
```bash
git add scripts/prices.py tests/test_prices.py
git commit -m "feat(prices): run_prices returns grouped price_states for SP-C derived"
```

---

### Task 2: `derived.py` skeleton, constants, canonical helpers, revenue (+ tests)

**Files:** Create `scripts/derived.py`; create `tests/test_derived.py`.

- [ ] **Step 1: failing test.** Create `tests/test_derived.py`:

```python
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
```

- [ ] **Step 2: run, expect fail** (`ModuleNotFoundError: derived`).

Run: `python -m unittest tests.test_derived -v`

- [ ] **Step 3: implement skeleton + helpers + revenue.** Create `scripts/derived.py`:

```python
#!/usr/bin/env python3
"""SP-C: NASS derived families.

Pure-Python derivations computed at emit time from the in-memory structures a
refresh already built: the county leaves (canonical marked by
refresh.mark_canonical) and the state price tree (from prices.run_prices). No
re-parse of the bulk file. Emits two sharded families plus an audit:

  data/derived/{fips}/counties/{code}/{slug}.json   per-county revenue + trend + rank
  data/states/{fips}/derived/state-{slug}.json      prod-weighted yield + county scan

See spec sections 4.5 (families), 4.6 (marketing-year join), 4.7 (safety).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import refresh  # lazy-safe: refresh imports this module only inside main()

# Marketing-year label offset added to the yield year, per crop (spec 4.6).
# Identity for all three crops here; stated explicitly so a future spring or
# southern-hemisphere commodity does not blindly reuse it.
MARKETING_YEAR_LABEL = {"corn": 0, "soybeans": 0, "wheat": 0}

TRAILING_MIN_5 = 3    # min present years in a 5-year window to emit an average
TRAILING_MIN_10 = 5   # min present years in a 10-year window


@dataclass(frozen=True)
class DerivedRunResult:
    paths: set
    shard_count: int
    kept_count: int


def _canonical(com: dict, statistic: str) -> Optional[dict]:
    """The canonical series for a statistic within one commodity, or None."""
    return next((s for s in com["series"]
                 if s.get("canonical") and s["statistic"] == statistic), None)


def _canonical_price(price_states: dict, fips: str, slug: str) -> Optional[dict]:
    """The canonical (ALL CLASSES marketing-year) price series, or None."""
    st = price_states.get(fips)
    if not st:
        return None
    com = st["crops"].get(slug)
    if not com:
        return None
    return next((s for s in com["series"] if s.get("canonical")), None)


def _marketing_year(slug: str, yield_year: str) -> Optional[str]:
    off = MARKETING_YEAR_LABEL.get(slug)
    if off is None:
        return None
    return str(int(yield_year) + off)


def compute_revenue(states: dict, price_states: dict) -> dict:
    """{(fips, code, slug): {year: revenue_record}}.

    per-harvested = yield*price; per-planted = production*price/area_planted
    (optional, only where production+area_planted present and planted>0). Emits
    a year only where yield and the joined marketing-year price both exist.
    """
    out: dict = {}
    for fips, st in states.items():
        for code, cty in st["counties"].items():
            for slug, com in cty["commodities"].items():
                yld = _canonical(com, "YIELD")
                if yld is None:
                    continue
                price = _canonical_price(price_states, fips, slug)
                if price is None:
                    continue
                prod = _canonical(com, "PRODUCTION")
                planted = _canonical(com, "AREA PLANTED")
                recs: dict = {}
                for year, yv in yld["values"].items():
                    my = _marketing_year(slug, year)
                    if my is None or my not in price["values"]:
                        continue
                    pv = price["values"][my]
                    rec = {
                        "marketing_year": my,
                        "yield": yv,
                        "price": pv,
                        "revenue_per_harvested_acre": round(yv * pv, 4),
                    }
                    if prod is not None and planted is not None:
                        pa = planted["values"].get(year)
                        pq = prod["values"].get(year)
                        if pa and pa > 0 and pq is not None:
                            rec["revenue_per_planted_acre"] = round(pq * pv / pa, 4)
                    recs[year] = rec
                if recs:
                    out[(fips, code, slug)] = recs
    return out
```

- [ ] **Step 4: run, expect pass.** `python -m unittest tests.test_derived -v`

- [ ] **Step 5: commit.**
```bash
git add scripts/derived.py tests/test_derived.py
git commit -m "feat(derived): SP-C skeleton, canonical helpers, imputed revenue/acre"
```

---

### Task 3: rank + percentile (+ tests)

**Files:** Modify `scripts/derived.py`; modify `tests/test_derived.py`.

- [ ] **Step 1: failing test.** Append to `tests/test_derived.py`:

```python
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
```

- [ ] **Step 2: run, expect fail** (`AttributeError: ... 'compute_ranks'`).

- [ ] **Step 3: implement.** Append to `scripts/derived.py`:

```python
def _rank_one(value: float, all_values: list[float]) -> tuple[int, int, float]:
    """Competition rank (1=highest, ties share), count, percentile.

    rank = 1 + count strictly greater. percentile = (n - rank)/(n - 1), or 1.0
    when n == 1.
    """
    n = len(all_values)
    rank = 1 + sum(1 for v in all_values if v > value)
    pct = 1.0 if n == 1 else round((n - rank) / (n - 1), 4)
    return rank, n, pct


def compute_ranks(states: dict) -> dict:
    """{(fips, code, slug): {year: rank_record}} on canonical YIELD.

    rank/percentile within-state and within-nation, per (crop, year).
    """
    # Gather canonical yield values: by (slug, year) -> nation list; and
    # (fips, slug, year) -> state list; remember each county's own value.
    nation: dict = {}                 # (slug, year) -> [values]
    state_pool: dict = {}             # (fips, slug, year) -> [values]
    own: dict = {}                    # (fips, code, slug) -> {year: value}
    for fips, st in states.items():
        for code, cty in st["counties"].items():
            for slug, com in cty["commodities"].items():
                yld = _canonical(com, "YIELD")
                if yld is None:
                    continue
                for year, v in yld["values"].items():
                    nation.setdefault((slug, year), []).append(v)
                    state_pool.setdefault((fips, slug, year), []).append(v)
                    own.setdefault((fips, code, slug), {})[year] = v
    out: dict = {}
    for (fips, code, slug), years in own.items():
        recs: dict = {}
        for year, v in years.items():
            sr, sn, sp = _rank_one(v, state_pool[(fips, slug, year)])
            nr, nn, npc = _rank_one(v, nation[(slug, year)])
            recs[year] = {
                "rank_in_state": sr, "count_in_state": sn, "percentile_in_state": sp,
                "rank_in_nation": nr, "count_in_nation": nn, "percentile_in_nation": npc,
            }
        out[(fips, code, slug)] = recs
    return out
```

- [ ] **Step 4: run, expect pass.** `python -m unittest tests.test_derived.RankTest -v`

- [ ] **Step 5: commit.**
```bash
git add scripts/derived.py tests/test_derived.py
git commit -m "feat(derived): within-state and within-nation rank + percentile"
```

---

### Task 4: production-weighted yield + per-series yield stats (+ tests)

**Files:** Modify `scripts/derived.py`; modify `tests/test_derived.py`.

- [ ] **Step 1: failing test.** Append to `tests/test_derived.py`:

```python
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
```

- [ ] **Step 2: run, expect fail.**

- [ ] **Step 3: implement.** Append to `scripts/derived.py`:

```python
def compute_weighted_yield(states: dict) -> dict:
    """{(fips, slug): {"state": {year: bu/ac}, "national": {year: bu/ac}}}.

    sum(production)/sum(area harvested) over counties where both canonical
    values exist for the year. National repeats across every state's file.
    """
    # accumulate (prod_sum, area_sum) per (slug, year) nationally and per
    # (fips, slug, year) by state
    nat: dict = {}     # (slug, year) -> [prod, area]
    bystate: dict = {} # (fips, slug, year) -> [prod, area]
    slugs_by_state: dict = {}  # fips -> set(slug)
    for fips, st in states.items():
        for cty in st["counties"].values():
            for slug, com in cty["commodities"].items():
                prod = _canonical(com, "PRODUCTION")
                area = _canonical(com, "AREA HARVESTED")
                if prod is None or area is None:
                    continue
                for year, pv in prod["values"].items():
                    av = area["values"].get(year)
                    if av is None or av == 0:
                        continue
                    n = nat.setdefault((slug, year), [0.0, 0.0]); n[0] += pv; n[1] += av
                    b = bystate.setdefault((fips, slug, year), [0.0, 0.0]); b[0] += pv; b[1] += av
                    slugs_by_state.setdefault(fips, set()).add(slug)
    nat_yield = {k: round(v[0] / v[1], 4) for k, v in nat.items() if v[1] > 0}
    out: dict = {}
    for fips, slugs in slugs_by_state.items():
        for slug in slugs:
            state_y = {year: round(b[0] / b[1], 4)
                       for (f, s, year), b in bystate.items()
                       if f == fips and s == slug and b[1] > 0}
            national_y = {year: nat_yield[(slug, year)]
                          for (s, year) in nat_yield if s == slug}
            out[(fips, slug)] = {"state": state_y, "national": national_y}
    return out


def _slope(points: list[tuple[int, float]]) -> Optional[float]:
    """OLS slope of y over x; None with < 2 distinct x."""
    xs = [x for x, _ in points]
    if len(set(xs)) < 2:
        return None
    n = len(points)
    mx = sum(xs) / n
    my = sum(y for _, y in points) / n
    num = sum((x - mx) * (y - my) for x, y in points)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return round(num / den, 4)


def compute_yield_stats(states: dict) -> dict:
    """{(fips, code, slug): {slope_bu_per_year?, yoy_pct{}, trailing_5yr_avg{}, trailing_10yr_avg{}}}
    on canonical YIELD. Suppressed years are simply absent from values."""
    out: dict = {}
    for fips, st in states.items():
        for code, cty in st["counties"].items():
            for slug, com in cty["commodities"].items():
                yld = _canonical(com, "YIELD")
                if yld is None:
                    continue
                vals = {int(y): v for y, v in yld["values"].items()}
                years = sorted(vals)
                stats: dict = {"yoy_pct": {}, "trailing_5yr_avg": {}, "trailing_10yr_avg": {}}
                slope = _slope([(y, vals[y]) for y in years])
                if slope is not None:
                    stats["slope_bu_per_year"] = slope
                for y in years:
                    if (y - 1) in vals and vals[y - 1] != 0:
                        stats["yoy_pct"][str(y)] = round((vals[y] - vals[y - 1]) / vals[y - 1] * 100, 2)
                    w5 = [vals[k] for k in range(y - 4, y + 1) if k in vals]
                    if len(w5) >= TRAILING_MIN_5:
                        stats["trailing_5yr_avg"][str(y)] = round(sum(w5) / len(w5), 2)
                    w10 = [vals[k] for k in range(y - 9, y + 1) if k in vals]
                    if len(w10) >= TRAILING_MIN_10:
                        stats["trailing_10yr_avg"][str(y)] = round(sum(w10) / len(w10), 2)
                out[(fips, code, slug)] = stats
    return out
```

- [ ] **Step 4: run, expect pass.** `python -m unittest tests.test_derived -v`

- [ ] **Step 5: commit.**
```bash
git add scripts/derived.py tests/test_derived.py
git commit -m "feat(derived): production-weighted yield + trailing/YoY/trend yield stats"
```

---

### Task 5: schemas + shape asserts + emit_all (+ tests)

**Files:** Modify `scripts/derived.py`; create `data/_schema/derived-county.json`, `data/_schema/derived-state.json`; modify `tests/test_derived.py`.

- [ ] **Step 1: create schemas.** `data/_schema/derived-county.json`:
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/_schema/derived-county.json",
  "title": "NASS derived per-county shard (v3)",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "state", "county", "commodity", "revenue", "yield_trend", "rank"],
  "properties": {
    "schema_version": {"const": 3},
    "state": {"type": "object", "required": ["fips", "alpha", "name"]},
    "county": {"type": "object", "required": ["code", "name"]},
    "commodity": {"type": "object", "required": ["slug", "desc"]},
    "revenue": {"type": "object", "additionalProperties": {
      "type": "object",
      "required": ["marketing_year", "yield", "price", "revenue_per_harvested_acre"],
      "properties": {
        "marketing_year": {"type": "string"},
        "yield": {"type": "number"}, "price": {"type": "number"},
        "revenue_per_harvested_acre": {"type": "number"},
        "revenue_per_planted_acre": {"type": "number"}
      }
    }},
    "yield_trend": {"type": "object", "required": ["yoy_pct", "trailing_5yr_avg", "trailing_10yr_avg"],
      "properties": {"slope_bu_per_year": {"type": "number"}}},
    "rank": {"type": "object"}
  }
}
```
`data/_schema/derived-state.json`:
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/_schema/derived-state.json",
  "title": "NASS derived per-state shard (v3)",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "state", "commodity", "production_weighted_yield", "counties"],
  "properties": {
    "schema_version": {"const": 3},
    "state": {"type": "object", "required": ["fips", "alpha", "name"]},
    "commodity": {"type": "object", "required": ["slug", "desc"]},
    "production_weighted_yield": {"type": "object", "required": ["state", "national"]},
    "counties": {"type": "object"}
  }
}
```

- [ ] **Step 2: failing test.** Append to `tests/test_derived.py`:

```python
class EmitTest(unittest.TestCase):
    def test_emit_writes_both_families_and_audit(self):
        from test_derived import _states_one_county, _price_states  # self refs
        disc = {"url": "u", "etag": '"e"', "date": "2026-05-30"}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(refresh, "DATA_DIR", Path(td)):
                paths = derived.emit_all(_states_one_county(), _price_states(),
                                         disc, "2026-05-30T00:00:00Z")
                county = json.loads((Path(td) / "derived" / "19" / "counties" / "169" / "corn.json").read_text())
                state = json.loads((Path(td) / "states" / "19" / "derived" / "state-corn.json").read_text())
                audit = json.loads((Path(td) / "_audit" / "derived.json").read_text())
        self.assertEqual(county["schema_version"], 3)
        self.assertIn("2024", county["revenue"])
        self.assertEqual(state["schema_version"], 3)
        self.assertIn("169", state["counties"])
        self.assertIn(Path(td) / "_schema" / "derived-county.json", paths)
        self.assertIn(Path(td) / "_schema" / "derived-state.json", paths)
        self.assertEqual(audit["product_name"], "NASS derived families")

    def test_emit_zero_shards_still_writes_audit(self):
        disc = {"url": "u", "etag": '"e"', "date": "2026-05-30"}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(refresh, "DATA_DIR", Path(td)):
                derived.emit_all({}, {}, disc, "2026-05-30T00:00:00Z")
                self.assertTrue((Path(td) / "_audit" / "derived.json").exists())

    def test_schema_files_v3(self):
        for name in ("derived-county.json", "derived-state.json"):
            p = Path(refresh.DATA_DIR) / "_schema" / name
            self.assertTrue(p.exists(), name)
            self.assertEqual(json.loads(p.read_text())["properties"]["schema_version"]["const"], 3)
```

- [ ] **Step 3: run, expect fail.**

- [ ] **Step 4: implement.** Append to `scripts/derived.py`:

```python
# ---------- paths ----------

def _county_shard_path(fips: str, code: str, slug: str) -> Path:
    return refresh.DATA_DIR / "derived" / fips / "counties" / code / f"{slug}.json"


def _state_shard_path(fips: str, slug: str) -> Path:
    return refresh.DATA_DIR / "states" / fips / "derived" / f"state-{slug}.json"


def _audit_path() -> Path:
    return refresh.DATA_DIR / "_audit" / "derived.json"


def _county_schema_path() -> Path:
    return refresh.DATA_DIR / "_schema" / "derived-county.json"


def _state_schema_path() -> Path:
    return refresh.DATA_DIR / "_schema" / "derived-state.json"


def derived_bootstrap_needed() -> bool:
    """True when the derived audit sentinel is absent (drives same-publication
    re-emit). Mirrors refresh.sp_a_bootstrap_needed."""
    return not _audit_path().exists()


# ---------- shape asserts ----------

def _assert_county_shape(shard: dict) -> None:
    top = {"schema_version", "state", "county", "commodity", "revenue", "yield_trend", "rank"}
    if set(shard) != top:
        raise SystemExit(f"Derived county keys mismatch: {sorted(set(shard))}")
    if shard["schema_version"] != 3:
        raise SystemExit(f"Derived county schema_version not 3: {shard['schema_version']!r}")
    yt = shard["yield_trend"]
    for k in ("yoy_pct", "trailing_5yr_avg", "trailing_10yr_avg"):
        if k not in yt:
            raise SystemExit(f"Derived county yield_trend missing {k}")


def _assert_state_shape(shard: dict) -> None:
    top = {"schema_version", "state", "commodity", "production_weighted_yield", "counties"}
    if set(shard) != top:
        raise SystemExit(f"Derived state keys mismatch: {sorted(set(shard))}")
    if shard["schema_version"] != 3:
        raise SystemExit(f"Derived state schema_version not 3: {shard['schema_version']!r}")
    if set(shard["production_weighted_yield"]) != {"state", "national"}:
        raise SystemExit("Derived state production_weighted_yield needs state+national")


# ---------- emit ----------

def emit_all(states: dict, price_states: dict, discovery: dict, refreshed_at: str) -> set:
    """Compute all derived families and write both shard trees + audit. Audit
    written UNCONDITIONALLY (zero-shard bootstrap sentinel). Returns the
    protected path set (both schemas + shards + audit)."""
    revenue = compute_revenue(states, price_states)
    ranks = compute_ranks(states)
    weighted = compute_weighted_yield(states)
    ystats = compute_yield_stats(states)

    paths: set = {_county_schema_path(), _state_schema_path()}
    county_count = 0

    # county family: one shard per (fips, code, slug) that has a canonical yield
    # (== appears in ystats, which keys on canonical YIELD presence)
    for (fips, code, slug) in sorted(ystats):
        st_meta = states[fips]["state"]
        cty = states[fips]["counties"][code]
        com = cty["commodities"][slug]
        shard = {
            "schema_version": 3,
            "state": st_meta,
            "county": {"code": code, "name": cty["name"]},
            "commodity": {"slug": slug, "desc": com["commodity_desc"]},
            "revenue": revenue.get((fips, code, slug), {}),
            "yield_trend": ystats[(fips, code, slug)],
            "rank": ranks.get((fips, code, slug), {}),
        }
        _assert_county_shape(shard)
        p = _county_shard_path(fips, code, slug)
        refresh.write_if_changed(p, refresh._dump_json(shard))
        paths.add(p)
        county_count += 1

    # state family: one shard per (fips, slug) that has any weighted yield or
    # any ranked county
    state_keys = set(weighted) | {(f, s) for (f, _c, s) in ranks}
    for (fips, slug) in sorted(state_keys):
        st_meta = states[fips]["state"]
        # county scan: yield values + rank for every county of this (fips, slug)
        counties: dict = {}
        for code, cty in states[fips]["counties"].items():
            com = cty["commodities"].get(slug)
            if com is None:
                continue
            yld = _canonical(com, "YIELD")
            if yld is None:
                continue
            counties[code] = {
                "name": cty["name"],
                "yield": yld["values"],
                "rank": ranks.get((fips, code, slug), {}),
            }
        wy = weighted.get((fips, slug), {"state": {}, "national": {}})
        desc = next((c["commodities"][slug]["commodity_desc"]
                     for c in states[fips]["counties"].values()
                     if slug in c["commodities"]), slug.upper())
        shard = {
            "schema_version": 3,
            "state": st_meta,
            "commodity": {"slug": slug, "desc": desc},
            "production_weighted_yield": {"state": wy["state"], "national": wy["national"]},
            "counties": counties,
        }
        _assert_state_shape(shard)
        p = _state_shard_path(fips, slug)
        refresh.write_if_changed(p, refresh._dump_json(shard))
        paths.add(p)

    audit = {
        "product_name": "NASS derived families",
        "refreshed_at": refreshed_at,
        "source": {"url": discovery["url"], "etag": discovery["etag"],
                   "publication_date": discovery["date"]},
        "county_shard_count": county_count,
    }
    ap = _audit_path()
    refresh.write_if_changed(ap, refresh._dump_json(audit))
    paths.add(ap)
    return paths
```

- [ ] **Step 5: run, expect pass.** `python -m unittest tests.test_derived -v`

- [ ] **Step 6: commit.**
```bash
git add scripts/derived.py data/_schema/derived-county.json data/_schema/derived-state.json tests/test_derived.py
git commit -m "feat(derived): schemas + shape asserts + emit_all (both families, zero-shard audit)"
```

---

### Task 6: `run_derived` + Gate 2 band (+ tests)

**Files:** Modify `scripts/derived.py`; modify `tests/test_derived.py`.

- [ ] **Step 1: failing test.** Append to `tests/test_derived.py`:

```python
class RunDerivedTest(unittest.TestCase):
    def test_returns_counts_and_emits(self):
        from test_derived import _states_one_county, _price_states
        disc = {"url": "u", "etag": '"e"', "date": "2026-05-30"}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(refresh, "DATA_DIR", Path(td)):
                res = derived.run_derived(_states_one_county(), _price_states(),
                                          disc, "2026-05-30T00:00:00Z", baseline=None)
        self.assertEqual(res.kept_count, 1)        # one county+crop with canonical yield
        self.assertGreaterEqual(res.shard_count, 1)

    def test_band_abort(self):
        from test_derived import _states_one_county, _price_states
        disc = {"url": "u", "etag": '"e"', "date": "2026-05-30"}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(refresh, "DATA_DIR", Path(td)):
                with self.assertRaises(SystemExit):
                    derived.run_derived(_states_one_county(), _price_states(),
                                        disc, "2026-05-30T00:00:00Z", baseline=100)
```

- [ ] **Step 2: run, expect fail.**

- [ ] **Step 3: implement.** Append to `scripts/derived.py`:

```python
def _validate_band(kept: int, baseline: Optional[int]) -> None:
    """Per-family Gate 2: +/-10% band vs prior county-shard count. Bootstrap-
    tolerant (None) and zero-tolerant (0), per spec 4.7."""
    if baseline is None or baseline == 0:
        return
    delta = abs(kept - baseline) / baseline
    if delta > refresh.ROW_COUNT_TOLERANCE:
        raise SystemExit(
            f"SP-C derived shard count {kept} differs from baseline {baseline} "
            f"by {delta:.1%} (>{refresh.ROW_COUNT_TOLERANCE:.0%}). Aborting.")


def run_derived(states: dict, price_states: dict, discovery: dict,
                refreshed_at: str, baseline: Optional[int]) -> DerivedRunResult:
    """SP-C entrypoint, called from refresh.main() after SP-B (in memory; no
    re-parse). kept_count = number of per-county derived shards."""
    paths = emit_all(states, price_states, discovery, refreshed_at)
    county_count = sum(
        1 for fips, st in states.items()
        for code, cty in st["counties"].items()
        for slug, com in cty["commodities"].items()
        if _canonical(com, "YIELD") is not None
    )
    _validate_band(county_count, baseline)
    print(f"SP-C derived: county_shards={county_count} paths={len(paths)}", file=sys.stderr)
    return DerivedRunResult(paths=paths, shard_count=len(paths), kept_count=county_count)
```

Note: `_validate_band` runs after `emit_all` here for code simplicity; the audit is written regardless, so the zero-shard sentinel invariant holds. The band still aborts the run non-zero when tripped (consistent with prices, where a hard abort exits the workflow).

- [ ] **Step 4: run, expect pass.** `python -m unittest tests.test_derived -v`

- [ ] **Step 5: commit.**
```bash
git add scripts/derived.py tests/test_derived.py
git commit -m "feat(derived): run_derived entrypoint + per-family Gate 2 band"
```

---

### Task 7: wire SP-C into `refresh.main` (+ tests)

**Files:** Modify `scripts/refresh.py`; modify `tests/test_refresh.py`; modify `tests/test_planting_windows.py`.

- [ ] **Step 1: failing test.** Append a class to `tests/test_refresh.py` (it already imports `refresh`, `mock`, `date`):

```python
class DerivedBootstrapTest(unittest.TestCase):
    def test_bootstrap_needed_true_when_derived_audit_missing(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(refresh, "DATA_DIR", Path(td)):
                self.assertTrue(refresh._derived_bootstrap_needed())
                (Path(td) / "_audit").mkdir(parents=True)
                (Path(td) / "_audit" / "derived.json").write_text("{}", encoding="utf-8")
                self.assertFalse(refresh._derived_bootstrap_needed())
```

- [ ] **Step 2: run, expect fail** (`AttributeError: ... '_derived_bootstrap_needed'`).

Run: `python -m unittest tests.test_refresh.DerivedBootstrapTest -v`

- [ ] **Step 3: implement `refresh.py`.** Add the bootstrap hook after `_prices_bootstrap_needed` (refresh.py:442-444):
```python
def _derived_bootstrap_needed() -> bool:
    """True when the SP-C derived audit sentinel is absent."""
    return not (DATA_DIR / "_audit" / "derived.json").exists()
```
OR it into `bootstrap_needed` (refresh.py:762-766):
```python
    bootstrap_needed = (
        not _index_path().exists()
        or sp_a_bootstrap_needed()
        or _prices_bootstrap_needed()
        or _derived_bootstrap_needed()
    )
```
Insert the SP-C call after the SP-B block (after `expected |= price_result.paths`, refresh.py:853):
```python
    # SP-C: derived families. Pure in-memory compute over the grouped county
    # leaves (canonical already marked) + the SP-B price tree; no re-parse.
    # Runs before the global prune so its paths survive.
    import derived  # lazy: avoids a circular import at module load
    derived_result = derived.run_derived(
        states, price_result.price_states, discovery, refreshed_at,
        family_baseline(state, "derived"),
    )
    expected |= derived_result.paths
```
Extend `save_state` (refresh.py:866, 872): add `"derived"` to the baseline map and a shard count:
```python
        "last_filtered_row_count": {
            "leaf": len(kept_rows), "prices": price_result.kept_count,
            "derived": derived_result.kept_count,
        },
```
and after `"last_price_shard_count": price_result.shard_count,`:
```python
        "last_derived_shard_count": derived_result.shard_count,
```

- [ ] **Step 4: run, expect pass.** `python -m unittest tests.test_refresh.DerivedBootstrapTest -v`

- [ ] **Step 5: fix the two main() integration tests.** They now exercise the SP-C path.

In `tests/test_refresh.py`, the `MainCaughtUpTest.test_main_returns_zero_when_already_caught_up_today` mocks `sp_a_bootstrap_needed` and `_prices_bootstrap_needed`. Add a third nested mock so `_derived_bootstrap_needed` returns False (else `bootstrap_needed` is True and the caught-up early return is bypassed). Change:
```python
            with mock.patch.object(refresh, "_prices_bootstrap_needed",
                                   return_value=False):
                result = refresh.main(today=date(2026, 5, 1))
```
to:
```python
            with mock.patch.object(refresh, "_prices_bootstrap_needed",
                                   return_value=False), \
                 mock.patch.object(refresh, "_derived_bootstrap_needed",
                                   return_value=False):
                result = refresh.main(today=date(2026, 5, 1))
```

In `tests/test_planting_windows.py`, `MainIntegrationTest.test_main_saves_explicit_sp_a_shard_count` stubs `prices.run_prices`. Add `import derived` near the other imports (`import prices` line), add `"run_derived": derived.run_derived,` to the `original` dict, stub it next to the prices stub:
```python
            derived.run_prices  # no-op line removed; see below
```
(Do NOT add that no-op.) Concretely:
- `import derived  # noqa: E402` after `import prices  # noqa: E402`.
- In `original`: `"run_derived": derived.run_derived,`.
- After the `prices.run_prices = lambda ...` stub:
```python
            derived.run_derived = lambda states, ps, d, ts, baseline: derived.DerivedRunResult(
                paths=set(), shard_count=0, kept_count=0,
            )
```
- In the `finally` restore loop, add:
```python
                elif k == "run_derived":
                    derived.run_derived = v
```
Also make `prices.run_prices` stub return a `price_states` so `run_derived` (if ever un-stubbed) gets a dict; keep it as `PriceRunResult(paths=set(), shard_count=0, kept_count=0)` (price_states defaults to `{}`), which is fine since `run_derived` is stubbed.

- [ ] **Step 6: run full suite, expect pass.**

Run: `python -m unittest discover -s tests -v 2>&1 | tail -5`
Expected: `OK`. CONFIRM the final line is `OK` and the run count increased; do not assume.

- [ ] **Step 7: commit.**
```bash
git add scripts/refresh.py tests/test_refresh.py tests/test_planting_windows.py
git commit -m "feat(refresh): wire SP-C derived into main (bootstrap sentinel, baseline, prune set)"
```

---

### Task 8: end-to-end integration test + README (+ commit)

**Files:** Modify `tests/test_derived.py`; modify `README.md`.

- [ ] **Step 1: integration test.** Append to `tests/test_derived.py`:

```python
class DerivedIntegrationTest(unittest.TestCase):
    def test_two_state_nation_revenue_rank_weighted(self):
        # IA + KS, corn, with price; verify cross-state nation rank, weighted
        # yield, and revenue join all land in the emitted shards.
        def cy(name, y, prod, ah, ap):
            return _county(name, {"corn": _com("CORN", "corn", [
                _series("YIELD", "BU / ACRE", {"2024": y}, canonical=True),
                _series("PRODUCTION", "BU", {"2024": prod}, canonical=True),
                _series("AREA HARVESTED", "ACRES", {"2024": ah}, canonical=True),
                _series("AREA PLANTED", "ACRES", {"2024": ap}, canonical=True)])})
        states = {
            "19": {"state": {"fips": "19", "alpha": "IA", "name": "IOWA"},
                   "counties": {"001": cy("A", 200.0, 2000.0, 10.0, 11.0)}},
            "20": {"state": {"fips": "20", "alpha": "KS", "name": "KANSAS"},
                   "counties": {"010": cy("E", 100.0, 1000.0, 10.0, 10.0)}},
        }
        price_states = {
            "19": {"state": states["19"]["state"], "crops": {"corn": {"commodity_desc": "CORN",
                   "series": [{"class": "ALL CLASSES", "period": "MARKETING YEAR", "unit": "$ / BU",
                               "canonical": True, "values": {"2024": 5.00}, "suppressed": {}}]}}},
            "20": {"state": states["20"]["state"], "crops": {"corn": {"commodity_desc": "CORN",
                   "series": [{"class": "ALL CLASSES", "period": "MARKETING YEAR", "unit": "$ / BU",
                               "canonical": True, "values": {"2024": 5.00}, "suppressed": {}}]}}},
        }
        disc = {"url": "u", "etag": '"e"', "date": "2026-05-30"}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(refresh, "DATA_DIR", Path(td)):
                derived.run_derived(states, price_states, disc, "2026-05-30T00:00:00Z", baseline=None)
                ia = json.loads((Path(td) / "derived" / "19" / "counties" / "001" / "corn.json").read_text())
                ia_state = json.loads((Path(td) / "states" / "19" / "derived" / "state-corn.json").read_text())
        # IA county A: yield 200 highest nationally -> rank_in_nation 1 of 2
        self.assertEqual(ia["rank"]["2024"]["rank_in_nation"], 1)
        self.assertEqual(ia["rank"]["2024"]["count_in_nation"], 2)
        # revenue per harvested = 200*5 = 1000; per planted = 2000*5/11
        self.assertAlmostEqual(ia["revenue"]["2024"]["revenue_per_harvested_acre"], 1000.0, places=4)
        self.assertAlmostEqual(ia["revenue"]["2024"]["revenue_per_planted_acre"], 2000.0 * 5.0 / 11.0, places=4)
        # IA state weighted yield = 2000/10 = 200; national = (2000+1000)/(10+10) = 150
        self.assertAlmostEqual(ia_state["production_weighted_yield"]["state"]["2024"], 200.0, places=4)
        self.assertAlmostEqual(ia_state["production_weighted_yield"]["national"]["2024"], 150.0, places=4)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: run, expect pass.** `python -m unittest tests.test_derived.DerivedIntegrationTest -v`

- [ ] **Step 3: README.** After the "### State prices" section (before `## Scope`), add a "### Derived families" subsection documenting the two new paths, the marketing-year join, the rank/percentile definition, and that prices in revenue are state-imputed (label accordingly). Keep it consistent with the prices section; no em/en dashes.

- [ ] **Step 4: commit.**
```bash
git add tests/test_derived.py README.md
git commit -m "test(derived): end-to-end + docs(readme): derived families section"
```

---

### Final: full suite, push, PR, codex implementation review, squash-merge

- [ ] Run `python -m unittest discover -s tests 2>&1 | tail -3`; CONFIRM `OK` and the run count (~baseline 144 + new derived tests). Read the actual last line; do not assume.
- [ ] Verify all new defs are in the committed blobs (subprocess `git show HEAD:scripts/derived.py | grep ...`), working tree clean.
- [ ] Push the branch (plain `git push -u origin <branch>`, a visible solo command).
- [ ] Open the PR (`gh pr create`), body summarizing the two families + math + safety, noting the canary is intentionally NOT extended (derived shards do not exist on the CDN until a refresh publishes them) and that field-mcp has no derived consumer yet.
- [ ] Poll CI (route `gh` output to a temp file, Read it). If red, fetch `--log-failed`, fix inline TDD, repush.
- [ ] Run a codex IMPLEMENTATION review on the diff (`codex exec - -s read-only -c 'model_reasoning_effort="high"' --enable web_search_cached --json`, capture to a STABLE temp path); read the FULL output and triage P1s. Fix real P1s inline, repush, re-review until clean.
- [ ] When CI is green AND codex is clean: squash-merge as a plain solo command `gh pr merge <n> --squash`; confirm `MERGED` via `gh pr view`. Fast-forward local main in the main root. Clean up the worktree (ExitWorktree, then branch deletes). Update memory: Phase 3 MERGED with the squash SHA.

## Self-review (writing-plans)

- **Spec coverage:** 4.5a revenue (Task 2), 4.5b rank/percentile (Task 3), 4.5c prod-weighted yield (Task 4), 4.5d trailing/YoY/trend (Task 4); 4.6 marketing-year join (Task 2, `MARKETING_YEAR_LABEL` + `marketing_year` field, tested); 4.7 zero-shard audit (Task 5), bootstrap sentinel + ORed into `bootstrap_needed` in THIS phase (Task 7), per-family Gate 2 (Task 6), committed static schemas (Task 5). 4.5e bundle is Phase 4, out of scope here.
- **Type consistency:** `_canonical(com, statistic)`, `compute_revenue`/`compute_ranks`/`compute_weighted_yield`/`compute_yield_stats` keyed `(fips, code, slug)` / `(fips, slug)` consistently; `DerivedRunResult(paths, shard_count, kept_count)` mirrors `PriceRunResult`; `run_derived(states, price_states, discovery, refreshed_at, baseline)` matches the main() call site and the test stub signature.
- **Placeholder scan:** none; every step has complete code. (The deliberately-wrong assertion in Task 4 Step 1 is immediately corrected in the same step with the explicit replacement line.)
- **No second parse:** derived consumes `states` + `price_result.price_states` (Task 1 makes the latter available); confirmed against refresh.main's existing in-scope `states` and the SP-B result.
