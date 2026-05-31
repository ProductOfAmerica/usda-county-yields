# NASS Foundation: Leaf v3 (prices/derived/bundle are later plans)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the county leaf to v3 (multi-statistic: yield + production + area, each value carrying CV), keep the yield-only rollup, and harden the pipeline (per-(crop,statistic) canonical rules, candidate-counting guard, per-family Gate 2 baseline, bootstrap same-publication re-emit) so the new data ships without corrupting the live field-mcp consumer.

**Architecture:** All changes are internal to `scripts/refresh.py` plus its tests and a new committed schema file. The intake filter widens from YIELD-only to five statistics; `group_by_state` carries `statistic` and `cv` on each series; canonical marking becomes per-(crop,statistic) with a candidate-counting abort; every emitted artifact bumps `schema_version` 2 to 3 atomically; the rollup stays yield-only via a filtered copy. No new modules in this plan (those are the Prices and Derived plans).

**Tech Stack:** Python 3.11 stdlib only (no third-party deps), `unittest`, gzipped TSV fixtures. Tests run with `python -m unittest discover -s tests`.

**Spec:** `docs/superpowers/specs/2026-05-31-nass-prices-stats-derived-design.md` (sections 4.1, 4.1.1, 4.2, 4.3, 4.7).

**Baseline:** 92 tests pass today (`python -m unittest discover -s tests`). Every task keeps the suite green.

**Cross-repo dependency (NOT in this plan, manual release gate):** field-mcp's leaf picker (`apps/gateway/src/lib/providers/usda/yields-cache.ts`, `pickCanonicalSeries`) must change to `series.find(s => s.canonical && (s.statistic === "YIELD" || s.statistic === undefined))` and be deployed + verified live BEFORE the v3 producer change merges and republishes data. This repo cannot enforce it; it is a checklist item on the field-mcp PR. See spec 4.2.

---

### Task 1: Add `CV_%` to required columns

**Files:**
- Modify: `scripts/refresh.py:59-67` (`REQUIRED_COLS`)
- Test: `tests/test_refresh.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_refresh.py` (after `StreamFilterTest`, around line 102):

```python
class RequiredColsTest(unittest.TestCase):
    def test_cv_pct_is_required(self):
        self.assertIn("CV_%", refresh.REQUIRED_COLS)

    def test_missing_cv_pct_aborts(self):
        # A bulk file whose header lacks CV_% must trip Gate 1.
        rows = [_row()]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.txt.gz"
            _rows_to_tsv_gz(rows, p)  # _row() has no CV_% key, so header omits it
            with self.assertRaises(SystemExit):
                refresh.stream_filter(p)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_refresh.RequiredColsTest -v`
Expected: FAIL (`CV_%` not in `REQUIRED_COLS`; `test_missing_cv_pct_aborts` does not raise).

- [ ] **Step 3: Add `CV_%` to `REQUIRED_COLS`**

In `scripts/refresh.py`, change the `REQUIRED_COLS` list (lines 59-67) so the final line reads:

```python
    "YEAR", "FREQ_DESC", "REFERENCE_PERIOD_DESC", "VALUE", "CV_%",
```

- [ ] **Step 4: Update the `_row()` fixture so existing tests still build valid files**

In `tests/test_refresh.py`, add `"CV_%"` to the `base` dict in `_row()` (after the `"VALUE"` line, around line 43):

```python
        "VALUE": "215.5",
        "CV_%": "1.8",
```

Then in `RequiredColsTest.test_missing_cv_pct_aborts`, drop the key so the header omits it:

```python
    def test_missing_cv_pct_aborts(self):
        row = _row()
        del row["CV_%"]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.txt.gz"
            _rows_to_tsv_gz([row], p)
            with self.assertRaises(SystemExit):
                refresh.stream_filter(p)
```

- [ ] **Step 5: Run tests to verify pass**

Run: `python -m unittest discover -s tests -v`
Expected: PASS, all green (94 tests: 92 baseline + 2 new).

- [ ] **Step 6: Commit**

```bash
git add scripts/refresh.py tests/test_refresh.py
git commit -m "feat(refresh): require CV_% column so a NASS drop trips Gate 1"
```

---

### Task 2: Widen the intake filter to five statistics

**Files:**
- Modify: `scripts/refresh.py:185-192` (`_parse_filter` row predicate)
- Test: `tests/test_refresh.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_refresh.py`:

```python
class FilterStatisticsTest(unittest.TestCase):
    STATS = ["YIELD", "PRODUCTION", "AREA HARVESTED", "AREA PLANTED", "AREA PLANTED, NET"]

    def test_keeps_five_statistics(self):
        rows = [_row(STATISTICCAT_DESC=s) for s in self.STATS]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.txt.gz"
            _rows_to_tsv_gz(rows, p)
            _, total, kept = refresh.stream_filter(p)
        self.assertEqual(total, 5)
        self.assertEqual(len(kept), 5)

    def test_excludes_other_statistics(self):
        rows = [_row(STATISTICCAT_DESC="STOCKS"), _row(STATISTICCAT_DESC="PRICE RECEIVED")]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.txt.gz"
            _rows_to_tsv_gz(rows, p)
            _, total, kept = refresh.stream_filter(p)
        self.assertEqual(len(kept), 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_refresh.FilterStatisticsTest -v`
Expected: FAIL on `test_keeps_five_statistics` (only YIELD kept, so `len(kept) == 1`).

- [ ] **Step 3: Add the statistic allowlist constant**

In `scripts/refresh.py`, after `COMMODITY_ALLOWLIST` (line 35), add:

```python
STATISTIC_ALLOWLIST = {
    "YIELD", "PRODUCTION", "AREA HARVESTED", "AREA PLANTED", "AREA PLANTED, NET",
}
```

- [ ] **Step 4: Replace the YIELD-only clause with the allowlist check**

In `_parse_filter` (lines 185-192), change the line

```python
                or row[col_idx["STATISTICCAT_DESC"]] != "YIELD"
```

to

```python
                or row[col_idx["STATISTICCAT_DESC"]] not in STATISTIC_ALLOWLIST
```

- [ ] **Step 5: Fix the existing StreamFilterTest expectation**

`StreamFilterTest.test_filters_to_target_rows` (line 89) uses `_row(STATISTICCAT_DESC="AREA HARVESTED")` as a row it expects to be *excluded*. AREA HARVESTED is now kept, so update the assertion. Replace that test body with:

```python
    def test_filters_to_target_rows(self):
        rows = [
            _row(),                                   # kept (county yield)
            _row(AGG_LEVEL_DESC="STATE"),             # excluded (not county)
            _row(STATISTICCAT_DESC="STOCKS"),         # excluded (not an allowed statistic)
            _row(COMMODITY_DESC="OATS"),              # excluded (not an allowed commodity)
        ]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.txt.gz"
            _rows_to_tsv_gz(rows, p)
            _, total, kept = refresh.stream_filter(p)
        self.assertEqual(total, 4)
        self.assertEqual(len(kept), 1)
```

- [ ] **Step 6: Run tests to verify pass**

Run: `python -m unittest discover -s tests -v`
Expected: PASS, all green.

- [ ] **Step 7: Commit**

```bash
git add scripts/refresh.py tests/test_refresh.py
git commit -m "feat(refresh): widen intake filter to yield + production + area"
```

---

### Task 3: Carry `statistic` and `cv` through grouping

**Files:**
- Modify: `scripts/refresh.py:208-285` (`group_by_state`: series key, series dict, value routing)
- Test: `tests/test_refresh.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_refresh.py`:

```python
class GroupStatisticCvTest(unittest.TestCase):
    def test_statistic_on_series(self):
        states = refresh.group_by_state([_row(STATISTICCAT_DESC="PRODUCTION",
                                              UNIT_DESC="BU", VALUE="1000",
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
        # YIELD bu/acre and YIELD bu/net-planted-acre are distinct series.
        states = refresh.group_by_state([
            _row(),
            _row(UNIT_DESC="BU / NET PLANTED ACRE",
                 SHORT_DESC="CORN, GRAIN - YIELD, MEASURED IN BU / NET PLANTED ACRE", VALUE="97.8"),
        ])
        com = states["19"]["counties"]["169"]["commodities"]["corn"]
        self.assertEqual(len(com["series"]), 2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_refresh.GroupStatisticCvTest -v`
Expected: FAIL (`KeyError: 'statistic'`, no `cv` key).

- [ ] **Step 3: Add `statistic` to the series key**

In `group_by_state`, change `series_key` (lines 252-258) to include the statistic, and the `next(...)` lookup (lines 259-263) to match it:

```python
        series_key = (
            row["STATISTICCAT_DESC"],
            row["CLASS_DESC"],
            row["PRODN_PRACTICE_DESC"],
            row["UTIL_PRACTICE_DESC"],
            row["UNIT_DESC"],
            row["SHORT_DESC"],
        )
        series = next(
            (s for s in com["series"]
             if (s["statistic"], s["class"], s["prodn_practice"], s["util_practice"], s["unit"], s["short_desc"]) == series_key),
            None,
        )
```

- [ ] **Step 4: Add `statistic` and `cv` to the new-series dict**

In the `if series is None:` block (lines 264-275), add the two fields:

```python
        if series is None:
            series = {
                "statistic": row["STATISTICCAT_DESC"],
                "class": row["CLASS_DESC"],
                "prodn_practice": row["PRODN_PRACTICE_DESC"],
                "util_practice": row["UTIL_PRACTICE_DESC"],
                "unit": row["UNIT_DESC"],
                "short_desc": row["SHORT_DESC"],
                "values": {},
                "cv": {},
                "suppressed": {},
                "raw": {},
            }
            com["series"].append(series)
```

- [ ] **Step 5: Route the CV value alongside the main value**

Replace the value-routing block (lines 277-284) with:

```python
        year = row["YEAR"]
        value, code, raw_str = parse_value(row["VALUE"])
        if value is not None:
            series["values"][year] = value
        elif code is not None:
            series["suppressed"][year] = code
        elif raw_str is not None:
            series["raw"][year] = raw_str

        cv_value, _cv_code, _cv_raw = parse_value(row["CV_%"])
        if cv_value is not None:
            series["cv"][year] = cv_value
```

- [ ] **Step 6: Run tests to verify pass**

Run: `python -m unittest discover -s tests -v`
Expected: PASS for `GroupStatisticCvTest`. NOTE: `AssertLeafShapeTest` and `MarkCanonicalTest` may now fail because series gained keys; those are fixed in Tasks 4 and 5. If only those two classes fail, proceed; if anything else fails, stop and investigate.

- [ ] **Step 7: Commit**

```bash
git add scripts/refresh.py tests/test_refresh.py
git commit -m "feat(refresh): carry statistic and cv on each series"
```

---

### Task 4: Per-(crop, statistic) canonical rule table + module assertion

**Files:**
- Modify: `scripts/refresh.py:37-57` (`CANONICAL_RULES` + assertion)
- Test: `tests/test_refresh.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_refresh.py`:

```python
class CanonicalRulesTableTest(unittest.TestCase):
    def test_every_crop_statistic_has_a_rule(self):
        crops = {c.lower() for c in refresh.COMMODITY_ALLOWLIST}
        for crop in crops:
            for stat in refresh.STATISTIC_ALLOWLIST:
                self.assertIn((crop, stat), refresh.CANONICAL_RULES,
                              f"missing rule for {(crop, stat)}")

    def test_corn_area_planted_is_all_utilization(self):
        rule = refresh.CANONICAL_RULES[("corn", "AREA PLANTED")]
        self.assertEqual(rule["util_practice"], "ALL UTILIZATION PRACTICES")

    def test_corn_area_harvested_is_grain(self):
        rule = refresh.CANONICAL_RULES[("corn", "AREA HARVESTED")]
        self.assertEqual(rule["util_practice"], "GRAIN")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_refresh.CanonicalRulesTableTest -v`
Expected: FAIL (`CANONICAL_RULES` is still keyed by crop string, not `(crop, statistic)` tuple).

- [ ] **Step 3: Replace `CANONICAL_RULES` with the per-(crop, statistic) table**

Replace lines 37-57 (the `CANONICAL_RULES` dict, the comment above it, and the `_MISSING_CANONICAL_RULES` assertion) with the verified table from spec 4.1.1:

```python
# Canonical-series rule per (crop_slug, statistic). The producer marks
# exactly one series per (county, crop, statistic) as canonical so consumers
# read one value per statistic without re-deriving NASS's filter. Verified
# against the live 2026-05-30 file: every rule is class=ALL CLASSES +
# prodn_practice=ALL PRODUCTION PRACTICES, but util_practice and unit vary by
# statistic, and corn AREA PLANTED uses ALL UTILIZATION PRACTICES while corn's
# other statistics use GRAIN. See spec section 4.1.1.
_AGG = ("ALL CLASSES", "ALL PRODUCTION PRACTICES")  # (class, prodn_practice) shared by every rule


def _rule(util: str, unit: str) -> dict[str, str]:
    return {"class": _AGG[0], "prodn_practice": _AGG[1], "util_practice": util, "unit": unit}


CANONICAL_RULES: dict[tuple[str, str], dict[str, str]] = {
    ("corn", "YIELD"):             _rule("GRAIN", "BU / ACRE"),
    ("corn", "PRODUCTION"):        _rule("GRAIN", "BU"),
    ("corn", "AREA HARVESTED"):    _rule("GRAIN", "ACRES"),
    ("corn", "AREA PLANTED"):      _rule("ALL UTILIZATION PRACTICES", "ACRES"),
    ("corn", "AREA PLANTED, NET"): _rule("GRAIN", "ACRES"),
    ("soybeans", "YIELD"):             _rule("ALL UTILIZATION PRACTICES", "BU / ACRE"),
    ("soybeans", "PRODUCTION"):        _rule("ALL UTILIZATION PRACTICES", "BU"),
    ("soybeans", "AREA HARVESTED"):    _rule("ALL UTILIZATION PRACTICES", "ACRES"),
    ("soybeans", "AREA PLANTED"):      _rule("ALL UTILIZATION PRACTICES", "ACRES"),
    ("soybeans", "AREA PLANTED, NET"): _rule("ALL UTILIZATION PRACTICES", "ACRES"),
    ("wheat", "YIELD"):             _rule("ALL UTILIZATION PRACTICES", "BU / ACRE"),
    ("wheat", "PRODUCTION"):        _rule("ALL UTILIZATION PRACTICES", "BU"),
    ("wheat", "AREA HARVESTED"):    _rule("ALL UTILIZATION PRACTICES", "ACRES"),
    ("wheat", "AREA PLANTED"):      _rule("ALL UTILIZATION PRACTICES", "ACRES"),
    ("wheat", "AREA PLANTED, NET"): _rule("ALL UTILIZATION PRACTICES", "ACRES"),
}

# Fail-fast: every (crop, statistic) in the allowlists must have a canonical
# rule, so a future commodity or statistic cannot silently ship without one.
_MISSING_CANONICAL_RULES = {
    (c.lower(), s) for c in COMMODITY_ALLOWLIST for s in STATISTIC_ALLOWLIST
} - set(CANONICAL_RULES)
assert not _MISSING_CANONICAL_RULES, (
    f"(crop, statistic) pairs missing from CANONICAL_RULES: {_MISSING_CANONICAL_RULES}"
)
```

Note: this block references `STATISTIC_ALLOWLIST` (added in Task 2) and must appear after it in the file. Place this block immediately after the `STATISTIC_ALLOWLIST` definition.

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m unittest tests.test_refresh.CanonicalRulesTableTest -v`
Expected: PASS. (`MarkCanonicalTest` still fails until Task 5; that is expected.)

- [ ] **Step 5: Commit**

```bash
git add scripts/refresh.py tests/test_refresh.py
git commit -m "feat(refresh): per-(crop,statistic) canonical rule table"
```

---

### Task 5: Candidate-counting `mark_canonical` + statistic-scoped guard

**Files:**
- Modify: `scripts/refresh.py:310-337` (`mark_canonical`)
- Modify: `scripts/refresh.py:543-561` (`validate_canonical_coverage` docstring only; semantics now YIELD-scoped, computed by caller)
- Test: `tests/test_refresh.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_refresh.py`:

```python
class MarkCanonicalV3Test(unittest.TestCase):
    def test_marks_one_per_statistic(self):
        states = refresh.group_by_state([
            _row(),  # corn YIELD
            _row(STATISTICCAT_DESC="PRODUCTION", UNIT_DESC="BU", VALUE="1000",
                 SHORT_DESC="CORN, GRAIN - PRODUCTION, MEASURED IN BU"),
        ])
        refresh.sort_series(states)
        refresh.mark_canonical(states)
        series = states["19"]["counties"]["169"]["commodities"]["corn"]["series"]
        canon = {s["statistic"]: s for s in series if s.get("canonical")}
        self.assertEqual(set(canon), {"YIELD", "PRODUCTION"})

    def test_duplicate_candidate_aborts(self):
        # Two series matching the SAME (corn, YIELD) rule => ambiguous => abort.
        dup = _row()
        dup2 = _row(SHORT_DESC="CORN, GRAIN - YIELD, MEASURED IN BU / ACRE (DUP)")
        # Same statistic/class/prodn/util/unit, different short_desc => two
        # series both matching the 4-tuple rule.
        states = refresh.group_by_state([dup, dup2])
        refresh.sort_series(states)
        with self.assertRaises(SystemExit):
            refresh.mark_canonical(states)

    def test_missing_yield_counted(self):
        # Only a PRODUCTION series, no YIELD => counts toward missing-YIELD.
        states = refresh.group_by_state([
            _row(STATISTICCAT_DESC="PRODUCTION", UNIT_DESC="BU", VALUE="1000",
                 SHORT_DESC="CORN, GRAIN - PRODUCTION, MEASURED IN BU"),
        ])
        refresh.sort_series(states)
        missing, samples = refresh.mark_canonical(states)
        self.assertEqual(missing, 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_refresh.MarkCanonicalV3Test -v`
Expected: FAIL (current `mark_canonical` uses `CANONICAL_RULES.get(slug)` with a crop-string key, which is now absent; raises or marks nothing).

- [ ] **Step 3: Rewrite `mark_canonical` to count candidates per (crop, statistic)**

Replace `mark_canonical` (lines 310-337) with:

```python
def mark_canonical(states: dict[str, dict]) -> tuple[int, list[tuple[str, str, str]]]:
    """Set series['canonical']=True on the per-(crop, statistic) match.

    For each (county, crop), group its series by statistic and, for each
    statistic that has a rule, collect every series matching that rule's
    4-tuple (class, prodn_practice, util_practice, unit). Abort if a rule
    matches more than one series (ambiguous; NASS drift). Returns
    (missing_yield_count, samples): the number of (county, crop) pairs that
    have at least one series but no canonical YIELD series, with up to 10
    (state_fips, county_name, crop_slug) samples for stderr.
    """
    missing_yield = 0
    samples: list[tuple[str, str, str]] = []
    for fips, st in states.items():
        for cty in st["counties"].values():
            for slug, com in cty["commodities"].items():
                series = com["series"]
                if not series:
                    continue
                has_yield_canonical = False
                for stat in STATISTIC_ALLOWLIST:
                    rule = CANONICAL_RULES.get((slug, stat))
                    if rule is None:
                        continue
                    candidates = [
                        s for s in series
                        if s["statistic"] == stat
                        and all(s.get(k) == v for k, v in rule.items())
                    ]
                    if len(candidates) > 1:
                        raise SystemExit(
                            f"Ambiguous canonical rule for {(slug, stat)} in "
                            f"{fips}/{cty['name']}: {len(candidates)} series match"
                        )
                    if candidates:
                        candidates[0]["canonical"] = True
                        if stat == "YIELD":
                            has_yield_canonical = True
                if not has_yield_canonical:
                    missing_yield += 1
                    if len(samples) < 10:
                        samples.append((fips, cty["name"], slug))
    return missing_yield, samples
```

- [ ] **Step 4: Update `validate_canonical_coverage` docstring to say YIELD**

In `validate_canonical_coverage` (lines 543-561), change the first docstring line and the `ALL CLASSES` reference so it reads "missing a canonical YIELD series" (semantics only; the math is unchanged, the caller now passes the missing-YIELD count from `mark_canonical`). Replace the docstring body's first paragraph:

```python
    """Gate 3: abort if too many (county, crop) pairs lack a canonical YIELD.

    A spike here means NASS structurally dropped the canonical YIELD variant
    for a crop, which would silently degrade every consumer point lookup.
    Empirical floor across published data is ~0.3%; 5% gives ~16x headroom
    for real drift while still catching a structural regression.
    """
```

- [ ] **Step 5: Remove the now-obsolete old MarkCanonicalTest**

Delete the old `MarkCanonicalTest` class (the one asserting silage counts as missing, lines ~120-136) since its `test_missing_canonical_counted` semantics are superseded by `MarkCanonicalV3Test.test_missing_yield_counted`. Keep `MarkCanonicalV3Test`.

- [ ] **Step 6: Run tests to verify pass**

Run: `python -m unittest tests.test_refresh.MarkCanonicalV3Test -v`
Expected: PASS. (`AssertLeafShapeTest` still fails until Task 6; expected.)

- [ ] **Step 7: Commit**

```bash
git add scripts/refresh.py tests/test_refresh.py
git commit -m "feat(refresh): candidate-counting canonical guard, YIELD-scoped ratio"
```

---

### Task 6: Leaf shape v3 + atomic schema_version bump across all emitters

**Files:**
- Modify: `scripts/refresh.py:293-294` (`_series_sort_key`)
- Modify: `scripts/refresh.py:564-598` (`_assert_leaf_shape`)
- Modify: `scripts/refresh.py` schema literals at lines 394, 424, 446, 479, 498 (index, meta, leaf, rollup, audit)
- Test: `tests/test_refresh.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_refresh.py`:

```python
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

    def test_v3_leaf_with_statistic_and_cv_passes(self):
        refresh._assert_leaf_shape(self._leaf())  # must not raise

    def test_v2_leaf_now_rejected(self):
        leaf = self._leaf()
        leaf["schema_version"] = 2
        with self.assertRaises(SystemExit):
            refresh._assert_leaf_shape(leaf)

    def test_series_missing_statistic_rejected(self):
        leaf = self._leaf()
        del leaf["series"][0]["statistic"]
        with self.assertRaises(SystemExit):
            refresh._assert_leaf_shape(leaf)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_refresh.LeafV3ShapeTest -v`
Expected: FAIL (`_assert_leaf_shape` requires `schema_version == 2` and rejects the new `statistic`/`cv` series keys).

- [ ] **Step 3: Add `statistic` to the series sort key**

Replace `_series_sort_key` (lines 293-294) with:

```python
def _series_sort_key(s: dict) -> tuple:
    return (s["statistic"], s["class"], s["prodn_practice"], s["util_practice"], s["unit"], s["short_desc"])
```

- [ ] **Step 4: Update `_assert_leaf_shape` to v3**

In `_assert_leaf_shape` (lines 564-598): change the version check (line 578) to require 3, and add `statistic` + `cv` to the required series keys (lines 586-589):

```python
    if leaf["schema_version"] != 3:
        raise SystemExit(f"Leaf schema_version not 3: {leaf['schema_version']!r}")
```

```python
    required_series = {
        "statistic", "class", "prodn_practice", "util_practice", "unit", "short_desc",
        "values", "cv", "suppressed", "raw",
    }
```

- [ ] **Step 5: Bump every emitter's `schema_version` literal 2 to 3**

Change the five `"schema_version": 2,` literals to `3` in: `emit_index` (line 394), `emit_state_meta` (line 424), `emit_point_leaves` (line 446), `emit_crop_rollups` (line 479), `emit_audit` (line 498).

- [ ] **Step 6: Update the legacy `AssertLeafShapeTest` fixtures to v3**

In the original `AssertLeafShapeTest` (lines 139-160), change both `"schema_version": 2` literals to `3` so those two tests still pass under the v3 assert.

- [ ] **Step 7: Add the no-artifact-at-v2 test for index/meta/audit**

Spec 4.7 requires asserting every artifact bumps. Add to `tests/test_refresh.py`:

```python
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
```

- [ ] **Step 8: Run tests to verify pass**

Run: `python -m unittest discover -s tests -v`
Expected: PASS, all green.

- [ ] **Step 9: Commit**

```bash
git add scripts/refresh.py tests/test_refresh.py
git commit -m "feat(refresh): leaf shape v3 + atomic schema_version 2->3 across emitters"
```

---

### Task 7: Yield-only rollup via filtered copy

**Files:**
- Modify: `scripts/refresh.py:460-488` (`emit_crop_rollups`)
- Test: `tests/test_refresh.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_refresh.py`:

```python
class RollupYieldOnlyTest(unittest.TestCase):
    def test_rollup_excludes_non_yield_series(self):
        states = refresh.group_by_state([
            _row(),  # corn YIELD
            _row(STATISTICCAT_DESC="PRODUCTION", UNIT_DESC="BU", VALUE="1000",
                 SHORT_DESC="CORN, GRAIN - PRODUCTION, MEASURED IN BU"),
        ])
        refresh.sort_series(states)
        refresh.mark_canonical(states)
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(refresh, "DATA_DIR", Path(td)):
                refresh.emit_crop_rollups(states)
                rollup = json.loads((Path(td) / "states" / "19" / "crops" / "corn.json").read_text())
        series = rollup["counties"]["169"]["series"]
        stats = {s["statistic"] for s in series}
        self.assertEqual(stats, {"YIELD"})

    def test_rollup_does_not_mutate_leaf_series(self):
        states = refresh.group_by_state([
            _row(),
            _row(STATISTICCAT_DESC="PRODUCTION", UNIT_DESC="BU", VALUE="1000",
                 SHORT_DESC="CORN, GRAIN - PRODUCTION, MEASURED IN BU"),
        ])
        refresh.sort_series(states)
        refresh.mark_canonical(states)
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(refresh, "DATA_DIR", Path(td)):
                refresh.emit_crop_rollups(states)
        leaf_series = states["19"]["counties"]["169"]["commodities"]["corn"]["series"]
        self.assertEqual(len({s["statistic"] for s in leaf_series}), 2)  # leaf still has both
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_refresh.RollupYieldOnlyTest -v`
Expected: FAIL (`test_rollup_excludes_non_yield_series`: rollup currently carries both YIELD and PRODUCTION).

- [ ] **Step 3: Filter the rollup county series to YIELD only**

In `emit_crop_rollups` (lines 468-476), change the per-county block so it copies only YIELD series into a new list (never the shared reference):

```python
        for code in sorted(st["counties"]):
            cty = st["counties"][code]
            for slug in sorted(cty["commodities"]):
                com = cty["commodities"][slug]
                yield_series = [s for s in com["series"] if s["statistic"] == "YIELD"]
                if not yield_series:
                    continue
                per_crop.setdefault(slug, {"desc": com["commodity_desc"], "counties": {}})
                per_crop[slug]["counties"][code] = {
                    "name": cty["name"],
                    "series": yield_series,
                }
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m unittest tests.test_refresh.RollupYieldOnlyTest -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/refresh.py tests/test_refresh.py
git commit -m "feat(refresh): rollup stays yield-only via filtered copy"
```

---

### Task 8: Per-family Gate 2 baseline map

**Files:**
- Modify: `scripts/refresh.py:650-697` (`main`: baseline read + validate call)
- Modify: `scripts/refresh.py:750-761` (`main`: `save_state` payload)
- Test: `tests/test_refresh.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_refresh.py`:

```python
class BaselineMapTest(unittest.TestCase):
    def test_legacy_int_baseline_treated_as_absent(self):
        # Old state stored last_filtered_row_count as an int. The new code
        # must read the leaf baseline as absent (bootstrap), not crash.
        self.assertIsNone(refresh.leaf_baseline({"last_filtered_row_count": 1318932}))

    def test_map_baseline_read(self):
        self.assertEqual(
            refresh.leaf_baseline({"last_filtered_row_count": {"leaf": 4300000}}),
            4300000,
        )

    def test_absent_baseline_is_none(self):
        self.assertIsNone(refresh.leaf_baseline({}))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_refresh.BaselineMapTest -v`
Expected: FAIL (`refresh.leaf_baseline` does not exist).

- [ ] **Step 3: Add the `leaf_baseline` helper**

In `scripts/refresh.py`, add after `load_state`/`save_state` (after line 612):

```python
def leaf_baseline(state: dict) -> Optional[int]:
    """Per-family Gate 2 baseline for the leaf family.

    Returns None (bootstrap, no abort) when the baseline is absent or stored
    in the legacy scalar shape, since the v2->v3 row count changes ~3.3x and
    a legacy scalar is not a valid v3 leaf baseline.
    """
    counts = state.get("last_filtered_row_count")
    if isinstance(counts, dict):
        return counts.get("leaf")
    return None
```

- [ ] **Step 4: Wire it into `main` and write the map on save**

In `main`, change the `validate(...)` call (line 697) to use the helper:

```python
    validate(total_rows, len(kept_rows), leaf_baseline(state))
```

And in the `save_state({...})` payload (line 755), change the count line to a map:

```python
        "last_filtered_row_count": {"leaf": len(kept_rows)},
```

- [ ] **Step 5: Run tests to verify pass**

Run: `python -m unittest discover -s tests -v`
Expected: PASS, all green.

- [ ] **Step 6: Commit**

```bash
git add scripts/refresh.py tests/test_refresh.py
git commit -m "feat(refresh): per-family Gate 2 baseline map (leaf)"
```

---

### Task 9: Bootstrap same-publication re-emit

**Files:**
- Modify: `scripts/refresh.py:111-137` (`discover`: inclusive bound)
- Modify: `scripts/refresh.py:650-687` (`main`: compute `bootstrap_needed` before early returns; guard `is_caught_up`)
- Test: `tests/test_refresh.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_refresh.py`:

```python
class DiscoverInclusiveTest(unittest.TestCase):
    def test_inclusive_includes_last_known(self):
        # With inclusive=True and last_known == today, discover must probe
        # today (re-find the already-published file) rather than return None.
        with mock.patch.object(refresh, "head_request",
                               return_value={"status": 200, "etag": '"e"',
                                             "last_modified": "x", "content_length": 1}):
            d = refresh.discover(date(2026, 5, 23), date(2026, 5, 23), inclusive=True)
        self.assertIsNotNone(d)
        self.assertEqual(d["date"], "2026-05-23")

    def test_default_exclusive_skips_last_known(self):
        with mock.patch.object(refresh, "head_request",
                               return_value={"status": 200, "etag": '"e"',
                                             "last_modified": "x", "content_length": 1}):
            d = refresh.discover(date(2026, 5, 23), date(2026, 5, 23))
        self.assertIsNone(d)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_refresh.DiscoverInclusiveTest -v`
Expected: FAIL (`discover()` has no `inclusive` parameter).

- [ ] **Step 3: Add the `inclusive` parameter to `discover`**

Change the `discover` signature (line 111) and the earliest-bound computation (lines 118-120):

```python
def discover(last_known: Optional[date], today: date, inclusive: bool = False) -> Optional[dict]:
```

```python
    earliest = today - timedelta(days=PROBE_WINDOW_DAYS)
    if last_known:
        offset = timedelta(days=0) if inclusive else timedelta(days=1)
        earliest = max(earliest, last_known + offset)
```

- [ ] **Step 4: Run the discover test to verify pass**

Run: `python -m unittest tests.test_refresh.DiscoverInclusiveTest -v`
Expected: PASS.

- [ ] **Step 5: Write the failing test for the main() reorder**

Add to `tests/test_refresh.py`:

```python
class MainBootstrapReemitTest(unittest.TestCase):
    def test_caught_up_but_missing_index_does_not_early_return(self):
        # Caught up (last_known == today) but index.json missing => must NOT
        # early-return; should proceed to discover(inclusive=True).
        calls = {}

        def fake_discover(last_known, today, inclusive=False):
            calls["inclusive"] = inclusive
            return None  # abort after, we only assert we reached discover

        with mock.patch.object(refresh, "load_state", return_value={
            "last_successful_date": "2026-05-23", "last_etag": '"x"',
        }), mock.patch.object(refresh, "_index_path",
                              return_value=Path("/nonexistent/index.json")), \
             mock.patch.object(refresh, "sp_a_bootstrap_needed", return_value=False), \
             mock.patch.object(refresh, "discover", side_effect=fake_discover):
            rc = refresh.main(today=date(2026, 5, 23))
        self.assertEqual(calls.get("inclusive"), True)
        self.assertEqual(rc, 1)  # discover returned None -> no-fresh-file abort
```

- [ ] **Step 6: Run it to verify it fails**

Run: `python -m unittest tests.test_refresh.MainBootstrapReemitTest -v`
Expected: FAIL (current `main` early-returns 0 at `is_caught_up` before computing `bootstrap_needed`, so `discover` is never called).

- [ ] **Step 7: Reorder `main` so bootstrap is computed before the early returns**

In `main`, move the `bootstrap_needed` assignment above the `is_caught_up` check and guard that check. Replace lines 659-691 (from the first `print(...)` through `download_with_retry`) with:

```python
    print(f"Last known publication: {last_known}; today: {today}")

    # Compute bootstrap need BEFORE the early returns: a missing index or a
    # missing family audit must suppress the caught-up / ETag-match shortcut
    # so the run re-emits the absent family. See spec section 4.7.
    bootstrap_needed = not _index_path().exists() or sp_a_bootstrap_needed()

    if is_caught_up(last_known, today) and not bootstrap_needed:
        print(f"Already caught up (last_known={last_known} >= today={today}); nothing to do.")
        ping_healthchecks()
        return 0
    discovery = discover(last_known, today, inclusive=bootstrap_needed)
    if not discovery:
        print(
            f"No fresh NASS file in last {PROBE_WINDOW_DAYS} days. Aborting.",
            file=sys.stderr,
        )
        return 1
    print(
        f"Discovered: {discovery['url']} "
        f"(publication {discovery['date']}, lag {discovery['lag_days']} days)"
    )

    if last_etag and discovery["etag"] == last_etag and not bootstrap_needed:
        print("ETag matches last successful run; nothing to do.")
        ping_healthchecks()
        return 0
    if bootstrap_needed and last_etag and discovery["etag"] == last_etag:
        print("ETag matches but bootstrap artifacts are missing; bootstrapping from cached download.")

    download_path = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / Path(discovery["url"]).name
    print(f"Downloading {discovery['url']} -> {download_path}")
    download_with_retry(discovery["url"], download_path)
```

(This deletes the old bootstrap-guard comment block at lines 676-680 and the old `bootstrap_needed = ...` line at 680, since it now lives above.)

- [ ] **Step 8: Run tests to verify pass**

Run: `python -m unittest discover -s tests -v`
Expected: PASS, all green. The existing `MainBootstrapTest.test_caught_up_returns_zero` still passes because its mocked state has no missing index in the real data tree... NOTE: that test does not mock `_index_path`, so `bootstrap_needed` depends on the real `data/index.json` existing. It does exist in the repo, and `sp_a_bootstrap_needed()` reads the real `data/_audit/planting-windows.json` which also exists, so `bootstrap_needed` is False and the test still returns 0. Confirm this; if `MainBootstrapTest` fails, add `mock.patch.object(refresh, "_index_path", return_value=<an existing file>)` and `mock.patch.object(refresh, "sp_a_bootstrap_needed", return_value=False)` to it.

- [ ] **Step 9: Commit**

```bash
git add scripts/refresh.py tests/test_refresh.py
git commit -m "feat(refresh): bootstrap re-emit before early returns + inclusive discover"
```

---

### Task 10: Create `data/_schema/leaf.json` at v3 + fix README link

**Files:**
- Create: `data/_schema/leaf.json`
- Modify: `README.md` (the `leaf.json` reference and the schema-version note)
- Test: `tests/test_refresh.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_refresh.py`:

```python
class LeafSchemaFileTest(unittest.TestCase):
    def test_leaf_schema_exists_and_is_v3(self):
        p = Path(refresh.DATA_DIR) / "_schema" / "leaf.json"
        self.assertTrue(p.exists(), "data/_schema/leaf.json must exist")
        schema = json.loads(p.read_text(encoding="utf-8"))
        sv = schema["properties"]["schema_version"]
        self.assertEqual(sv.get("const"), 3)

    def test_leaf_schema_series_requires_statistic_and_cv(self):
        p = Path(refresh.DATA_DIR) / "_schema" / "leaf.json"
        schema = json.loads(p.read_text(encoding="utf-8"))
        series_required = set(schema["properties"]["series"]["items"]["required"])
        self.assertTrue({"statistic", "cv"} <= series_required)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_refresh.LeafSchemaFileTest -v`
Expected: FAIL (`data/_schema/leaf.json` does not exist).

- [ ] **Step 3: Create `data/_schema/leaf.json`**

Create `data/_schema/leaf.json` with this content (JSON Schema 2020-12, matching the v3 `_assert_leaf_shape` contract):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/_schema/leaf.json",
  "title": "NASS county point leaf (v3)",
  "description": "One leaf per (state, county, crop). Multi-statistic: each series carries a statistic (YIELD, PRODUCTION, AREA HARVESTED, AREA PLANTED, AREA PLANTED, NET), a values map, and a parallel cv map (NASS CV_%). At most one canonical series per statistic.",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "state", "county", "commodity", "series"],
  "properties": {
    "schema_version": { "const": 3 },
    "state": {
      "type": "object",
      "additionalProperties": false,
      "required": ["fips", "alpha", "name"],
      "properties": {
        "fips": { "type": "string", "pattern": "^[0-9]{2}$" },
        "alpha": { "type": "string", "pattern": "^[A-Z]{2}$" },
        "name": { "type": "string" }
      }
    },
    "county": {
      "type": "object",
      "additionalProperties": false,
      "required": ["code", "name"],
      "properties": {
        "code": { "type": "string", "pattern": "^[0-9]{3}$" },
        "name": { "type": "string" }
      }
    },
    "commodity": {
      "type": "object",
      "additionalProperties": false,
      "required": ["slug", "desc"],
      "properties": {
        "slug": { "type": "string" },
        "desc": { "type": "string" }
      }
    },
    "series": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["statistic", "class", "prodn_practice", "util_practice", "unit", "short_desc", "values", "cv", "suppressed", "raw"],
        "properties": {
          "statistic": { "type": "string", "enum": ["YIELD", "PRODUCTION", "AREA HARVESTED", "AREA PLANTED", "AREA PLANTED, NET"] },
          "class": { "type": "string" },
          "prodn_practice": { "type": "string" },
          "util_practice": { "type": "string" },
          "unit": { "type": "string" },
          "short_desc": { "type": "string" },
          "canonical": { "type": "boolean" },
          "values": { "type": "object", "additionalProperties": { "type": "number" } },
          "cv": { "type": "object", "additionalProperties": { "type": "number" } },
          "suppressed": { "type": "object", "additionalProperties": { "type": "string" } },
          "raw": { "type": "object", "additionalProperties": { "type": "string" } }
        }
      }
    }
  }
}
```

- [ ] **Step 4: Fix the README references**

In `README.md`, update the two `leaf.json` mentions and the schema-version expectations to v3. Find the schema section that documents `"schema_version": 2` and the canonical picker (`series[s for s if s.canonical]`), and change:
- every documented `"schema_version": 2` in a leaf/index/rollup example to `3`;
- the canonical-pick guidance to note the consumer should filter `canonical && statistic == "YIELD"` (a v3 leaf has one canonical per statistic);
- confirm the `data/_schema/leaf.json` link target now resolves (the file exists as of Step 3).

Run to locate them: `grep -n "schema_version\|leaf.json\|s.canonical\|select(.canonical)" README.md`

- [ ] **Step 5: Run tests to verify pass**

Run: `python -m unittest discover -s tests -v`
Expected: PASS, all green.

- [ ] **Step 6: Commit**

```bash
git add data/_schema/leaf.json README.md tests/test_refresh.py
git commit -m "feat(schema): create v3 leaf.json contract; update README to v3"
```

---

### Task 11: Full-pipeline integration test (multi-statistic end to end)

**Files:**
- Test: `tests/test_refresh.py`

- [ ] **Step 1: Write the integration test**

Add to `tests/test_refresh.py`. This drives `group_by_state` -> `sort_series` -> `mark_canonical` -> `emit_point_leaves` -> `emit_crop_rollups` against a fixture covering all three crops and the canonical-table traps (corn AREA PLANTED util, corn AREA HARVESTED vs silage):

```python
class FoundationIntegrationTest(unittest.TestCase):
    def test_multi_statistic_leaf_and_yield_only_rollup(self):
        rows = [
            _row(),  # corn YIELD GRAIN BU/ACRE
            _row(STATISTICCAT_DESC="PRODUCTION", UNIT_DESC="BU", VALUE="1000000",
                 SHORT_DESC="CORN, GRAIN - PRODUCTION, MEASURED IN BU"),
            _row(STATISTICCAT_DESC="AREA HARVESTED", UNIT_DESC="ACRES", VALUE="5000",
                 SHORT_DESC="CORN, GRAIN - ACRES HARVESTED"),
            _row(STATISTICCAT_DESC="AREA PLANTED", UNIT_DESC="ACRES", VALUE="5100",
                 UTIL_PRACTICE_DESC="ALL UTILIZATION PRACTICES",
                 SHORT_DESC="CORN - ACRES PLANTED"),
            # A silage AREA HARVESTED row at the same class/prodn/unit but
            # util=SILAGE must NOT be marked canonical for AREA HARVESTED.
            _row(STATISTICCAT_DESC="AREA HARVESTED", UNIT_DESC="ACRES", VALUE="200",
                 UTIL_PRACTICE_DESC="SILAGE", SHORT_DESC="CORN, SILAGE - ACRES HARVESTED"),
        ]
        states = refresh.group_by_state(rows)
        refresh.sort_series(states)
        missing, _ = refresh.mark_canonical(states)
        self.assertEqual(missing, 0)  # has a canonical YIELD

        com = states["19"]["counties"]["169"]["commodities"]["corn"]
        canon = {s["statistic"] for s in com["series"] if s.get("canonical")}
        self.assertEqual(canon, {"YIELD", "PRODUCTION", "AREA HARVESTED", "AREA PLANTED"})
        # The silage AREA HARVESTED series exists but is not canonical.
        silage = [s for s in com["series"]
                  if s["statistic"] == "AREA HARVESTED" and s["util_practice"] == "SILAGE"]
        self.assertEqual(len(silage), 1)
        self.assertFalse(silage[0].get("canonical"))

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(refresh, "DATA_DIR", Path(td)):
                refresh.emit_point_leaves(states)
                refresh.emit_crop_rollups(states)
                leaf = json.loads((Path(td) / "states" / "19" / "counties" / "169" / "corn.json").read_text())
                rollup = json.loads((Path(td) / "states" / "19" / "crops" / "corn.json").read_text())
        self.assertEqual(leaf["schema_version"], 3)
        self.assertEqual({s["statistic"] for s in rollup["counties"]["169"]["series"]}, {"YIELD"})
```

- [ ] **Step 2: Run it to verify it passes**

Run: `python -m unittest tests.test_refresh.FoundationIntegrationTest -v`
Expected: PASS (all prior tasks make this green; if it fails, the failure pinpoints which task's behavior regressed).

- [ ] **Step 3: Run the whole suite**

Run: `python -m unittest discover -s tests -v`
Expected: PASS, all green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_refresh.py
git commit -m "test(refresh): foundation integration (multi-statistic leaf, yield-only rollup)"
```

---

## Self-review notes (for the implementer)

- **`_row()` ordering matters.** `_rows_to_tsv_gz` derives the header from `rows[0].keys()`. Because Task 1 adds `CV_%` to the `_row()` base dict, every fixture file carries the column. Tests that build a row list with differing keys must keep key order consistent across rows (all use `_row()`, which fixes this).
- **Task interdependence:** Tasks 3, 4, 5, 6 are a chain (the data model gains fields, then the rules, then marking, then the shape assert). The suite is intentionally red between Task 3 and Task 6 for `AssertLeafShapeTest`/`MarkCanonical`; each task's own new tests go green, and the whole suite returns green at Task 6. The plan calls this out at each step so a per-task reviewer is not surprised.
- **Not in this plan:** prices, derived families, the multi-crop bundle, and the per-family baseline/bootstrap terms for those families. Each is its own plan (spec phases 2-4), authored after Foundation merges so it builds on the real `run_*` result-object shape introduced here.
- **Before pushing:** the field-mcp picker (spec 4.2) must be deployed and verified live first. This plan produces no data publish on its own (publishing happens when the refresh cron runs against `@main` after merge), so the ordering gate lands on the merge/publish step, tracked on the field-mcp PR.
