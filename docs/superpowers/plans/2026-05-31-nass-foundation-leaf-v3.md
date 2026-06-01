# NASS Foundation: Leaf v3 (prices/derived/bundle are later plans)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the county leaf to v3 (multi-statistic: yield + production + area, each value carrying CV), keep the yield-only rollup, and harden the pipeline (per-(crop,statistic) canonical rules, candidate-counting guard, per-family Gate 2 baseline, bootstrap same-publication re-emit) so the new data ships without corrupting the live field-mcp consumer.

**Architecture:** All changes are internal to `scripts/refresh.py` plus its tests and a new committed schema file. The intake filter widens from YIELD-only to five statistics; `group_by_state` carries `statistic` and `cv` on each series; canonical marking becomes per-(crop,statistic) with a candidate-counting abort; every emitted artifact bumps `schema_version` 2 to 3 atomically; the rollup stays yield-only via a filtered copy. No new modules in this plan (those are the Prices and Derived plans).

**Tech Stack:** Python 3.11 stdlib only (no third-party deps), `unittest`. Tests run with `python -m unittest discover -s tests`.

**Spec:** `docs/superpowers/specs/2026-05-31-nass-prices-stats-derived-design.md` (sections 4.1, 4.1.1, 4.2, 4.3, 4.7).

**Baseline:** 92 tests pass today (`python -m unittest discover -s tests`). The suite is intentionally red for two specific classes between Task 3 and Task 6 (called out per task) and returns fully green at Task 6.

**Test-file facts the executor must know (verified against the current `tests/test_refresh.py`):**
- `HEADER` (line 26-35) already includes `"CV_%"` as the last column. The fixture already produces that column; Task 1 only makes `refresh` *require* it.
- `make_row(**overrides) -> list` builds a 39-column row list for filter tests fed through `refresh._parse_filter`.
- There is NO module-level dict-row helper and NO `mock` import yet. Task 1 adds both.
- `fixture_rows()` row 11 is currently `make_row(STATISTICCAT_DESC="PRODUCTION", ...)` commented "DROP: not YIELD statistic". After Task 2 widens the filter, PRODUCTION is KEPT, which would break five existing count assertions. Task 2 changes that fixture row to `STOCKS` (still excluded) so existing tests keep their intent, and adds dedicated PRODUCTION/AREA tests via the new helpers.

**Cross-repo dependency (NOT in this plan, manual release gate):** field-mcp's leaf picker (`apps/gateway/src/lib/providers/usda/yields-cache.ts`, `pickCanonicalSeries`) must change to `series.find(s => s.canonical && (s.statistic === "YIELD" || s.statistic === undefined))` and be deployed + verified live BEFORE the v3 producer change merges and republishes data. This repo cannot enforce it; it is a checklist item on the field-mcp PR. See spec 4.2.

---

### Task 1: Test scaffolding (helpers + mock import) and require `CV_%`

**Files:**
- Modify: `tests/test_refresh.py` (imports near line 12; add module-level helpers after `make_row`/`fixture_*`, around line 115)
- Modify: `scripts/refresh.py:59-67` (`REQUIRED_COLS`)

- [ ] **Step 1: Add the `mock` import**

In `tests/test_refresh.py`, change the `import unittest` line (line 12) to also import `mock`:

```python
import unittest
from unittest import mock
```

- [ ] **Step 2: Add two module-level test helpers**

In `tests/test_refresh.py`, after `fixture_csv_text()` (around line 115, before `# ---------- unit tests ----------`), add:

```python
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


def _filter(list_rows: list[list[str]]):
    """Run a list of make_row() lists through _parse_filter; returns (header, total, kept)."""
    text = io.StringIO()
    writer = csv.writer(text, delimiter="\t")
    writer.writerow(HEADER)
    for r in list_rows:
        writer.writerow(r)
    text.seek(0)
    return refresh._parse_filter(csv.reader(text, delimiter="\t"))
```

- [ ] **Step 3: Write the failing test for the required column**

Add to `tests/test_refresh.py` (after the existing `MissingRequiredColumnTest`, around line 261):

```python
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
```

- [ ] **Step 4: Run test to verify it fails**

Run: `python -m unittest tests.test_refresh.RequiredCvColTest -v`
Expected: FAIL (`CV_%` not in `REQUIRED_COLS`; the abort test does not raise).

- [ ] **Step 5: Add `CV_%` to `REQUIRED_COLS`**

In `scripts/refresh.py`, change the last line of `REQUIRED_COLS` (line 66) to:

```python
    "YEAR", "FREQ_DESC", "REFERENCE_PERIOD_DESC", "VALUE", "CV_%",
```

- [ ] **Step 6: Run the whole suite to verify pass**

Run: `python -m unittest discover -s tests -v`
Expected: PASS, all green (94: 92 baseline + 2 new).

- [ ] **Step 7: Commit**

```bash
git add scripts/refresh.py tests/test_refresh.py
git commit -m "feat(refresh): require CV_% column; add dict-row + filter test helpers"
```

---

### Task 2: Widen the intake filter to five statistics

**Files:**
- Modify: `scripts/refresh.py:35` (add `STATISTIC_ALLOWLIST`), `scripts/refresh.py:186` (filter clause)
- Modify: `tests/test_refresh.py` (fixture row 11; new filter tests)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_refresh.py` (after `RequiredCvColTest`):

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_refresh.FilterStatisticsTest -v`
Expected: FAIL on `test_keeps_five_statistics` (only YIELD kept).

- [ ] **Step 3: Add the statistic allowlist constant**

In `scripts/refresh.py`, after `COMMODITY_ALLOWLIST = {"CORN", "SOYBEANS", "WHEAT"}` (line 35), add:

```python
STATISTIC_ALLOWLIST = {
    "YIELD", "PRODUCTION", "AREA HARVESTED", "AREA PLANTED", "AREA PLANTED, NET",
}
```

- [ ] **Step 4: Replace the YIELD-only clause with the allowlist check**

In `_parse_filter`, change line 186 from:

```python
                or row[col_idx["STATISTICCAT_DESC"]] != "YIELD"
```

to:

```python
                or row[col_idx["STATISTICCAT_DESC"]] not in STATISTIC_ALLOWLIST
```

- [ ] **Step 5: Repoint the existing fixture's "dropped" row so existing counts hold**

`fixture_rows()` row 11 (line 100-101) is currently a PRODUCTION row asserted as dropped; PRODUCTION is now kept, which would break five existing count tests. Change it to `STOCKS` (still excluded), preserving every existing assertion. Replace those two lines:

```python
        # 11. DROP: STOCKS is not an allowed statistic
        make_row(STATISTICCAT_DESC="STOCKS", UNIT_DESC="BU", VALUE="16500000"),
```

- [ ] **Step 6: Run the whole suite to verify pass**

Run: `python -m unittest discover -s tests -v`
Expected: PASS, all green. (`FilterAndGroupTest.test_filter_keeps_seven`, `TolerantHeaderTest`, `GroupByStateTest`, `PointLeafTest`, `CropRollupTest` still see exactly the original 7 kept rows because row 11 is excluded again.)

- [ ] **Step 7: Commit**

```bash
git add scripts/refresh.py tests/test_refresh.py
git commit -m "feat(refresh): widen intake filter to yield + production + area"
```

---

### Task 3: Carry `statistic` and `cv` through grouping

**Files:**
- Modify: `scripts/refresh.py:252-284` (`group_by_state`: series key, lookup, new-series dict, value routing)
- Modify: `tests/test_refresh.py` (new tests)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_refresh.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_refresh.GroupStatisticCvTest -v`
Expected: FAIL (`KeyError: 'statistic'`, no `cv` key).

- [ ] **Step 3: Add `statistic` to the series key and lookup**

In `group_by_state`, replace `series_key` (lines 252-258) and the `next(...)` lookup (lines 259-263) with:

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

Replace the `if series is None:` block (lines 264-275) with:

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

- [ ] **Step 6: Run the new tests to verify pass**

Run: `python -m unittest tests.test_refresh.GroupStatisticCvTest -v`
Expected: PASS.

- [ ] **Step 7: Run the whole suite (expect two known-red classes)**

Run: `python -m unittest discover -s tests -v`
Expected: All green EXCEPT `LeafShapeAssertTest` and `CanonicalTest` (series now carry `statistic`/`cv` that the v2 `_assert_leaf_shape` rejects, and the tuple-vs-string canonical change lands in Task 4). This is expected and fixed by Tasks 4 and 6. If any OTHER class fails, stop and investigate.

- [ ] **Step 8: Commit**

```bash
git add scripts/refresh.py tests/test_refresh.py
git commit -m "feat(refresh): carry statistic and cv on each series"
```

---

### Task 4: Per-(crop, statistic) canonical rule table + module assertion

**Files:**
- Modify: `scripts/refresh.py:37-57` (`CANONICAL_RULES` + assertion); must sit AFTER `STATISTIC_ALLOWLIST` (Task 2)
- Modify: `tests/test_refresh.py` (new tests; fix one existing test)

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
        self.assertEqual(
            refresh.CANONICAL_RULES[("corn", "AREA PLANTED")]["util_practice"],
            "ALL UTILIZATION PRACTICES")

    def test_corn_area_harvested_is_grain(self):
        self.assertEqual(
            refresh.CANONICAL_RULES[("corn", "AREA HARVESTED")]["util_practice"],
            "GRAIN")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_refresh.CanonicalRulesTableTest -v`
Expected: FAIL (`CANONICAL_RULES` is keyed by crop string, not `(crop, statistic)`).

- [ ] **Step 3: Replace `CANONICAL_RULES` with the per-(crop, statistic) table**

Replace lines 37-57 (the `CANONICAL_RULES` dict, its comment, and the `_MISSING_CANONICAL_RULES` assertion) with the verified table. This block references `STATISTIC_ALLOWLIST` (Task 2), so it must appear after that definition; if needed move it just below `STATISTIC_ALLOWLIST`.

```python
# Canonical-series rule per (crop_slug, statistic). The producer marks exactly
# one series per (county, crop, statistic) as canonical so consumers read one
# value per statistic without re-deriving NASS's filter. Verified against the
# live 2026-05-30 file: every rule is class=ALL CLASSES + prodn_practice=ALL
# PRODUCTION PRACTICES, but util_practice and unit vary by statistic, and corn
# AREA PLANTED uses ALL UTILIZATION PRACTICES while corn's other statistics use
# GRAIN. See spec section 4.1.1.
def _rule(util: str, unit: str) -> dict[str, str]:
    return {"class": "ALL CLASSES", "prodn_practice": "ALL PRODUCTION PRACTICES",
            "util_practice": util, "unit": unit}


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

- [ ] **Step 4: Fix the existing commodity-coverage test (now tuple-keyed)**

The existing `CanonicalTest.test_canonical_rules_cover_all_commodities` (around line 428-433) computes `{crops} - set(CANONICAL_RULES)`, which is now always non-empty because keys are tuples. Replace that test body with:

```python
    def test_canonical_rules_cover_all_commodities(self) -> None:
        crops = {c.lower() for c in refresh.COMMODITY_ALLOWLIST}
        rule_crops = {crop for (crop, _stat) in refresh.CANONICAL_RULES}
        self.assertEqual(crops - rule_crops, set())
```

- [ ] **Step 5: Run the canonical-table tests to verify pass**

Run: `python -m unittest tests.test_refresh.CanonicalRulesTableTest -v`
Expected: PASS. (`CanonicalTest` and `LeafShapeAssertTest` are still red until Tasks 5 and 6; expected.)

- [ ] **Step 6: Commit**

```bash
git add scripts/refresh.py tests/test_refresh.py
git commit -m "feat(refresh): per-(crop,statistic) canonical rule table"
```

---

### Task 5: Candidate-counting `mark_canonical` + YIELD-scoped guard

**Files:**
- Modify: `scripts/refresh.py:326-353` (`mark_canonical`)
- Modify: `scripts/refresh.py:560-567` (`validate_canonical_coverage` docstring only)
- Modify: `tests/test_refresh.py` (new tests)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_refresh.py`:

```python
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
        # Two series both matching the (corn, YIELD) 4-tuple rule (same
        # class/prodn/util/unit), differing only by short_desc => ambiguous.
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_refresh.MarkCanonicalV3Test -v`
Expected: FAIL (current `mark_canonical` uses `CANONICAL_RULES.get(slug)` with a crop-string key, now absent).

- [ ] **Step 3: Rewrite `mark_canonical`**

Replace `mark_canonical` (lines 326-353) with:

```python
def mark_canonical(states: dict[str, dict]) -> tuple[int, list[tuple[str, str, str]]]:
    """Set series['canonical']=True on the per-(crop, statistic) match.

    For each (county, crop), for each statistic with a rule, collect every
    series matching that rule's 4-tuple (class, prodn_practice, util_practice,
    unit). Abort if a rule matches more than one series (ambiguous; NASS drift).
    Returns (missing_yield_count, samples): the number of (county, crop) pairs
    that have at least one series but no canonical YIELD series, with up to 10
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

- [ ] **Step 4: Update the Gate 3 docstring to say YIELD**

In `validate_canonical_coverage` (lines 560-567), replace the docstring's first two lines so the semantics read YIELD-scoped (the math is unchanged; `main` already passes `mark_canonical`'s count):

```python
    """Gate 3: abort if too many (county, crop) pairs lack a canonical YIELD.

    A spike means NASS structurally dropped the canonical YIELD variant for a
    crop, which would silently degrade every consumer point lookup. Empirical
    floor across published data is ~0.3%; 5% gives ~16x headroom for real drift
    while still catching a structural regression.
    """
```

- [ ] **Step 5: Run the whole suite (expect only `LeafShapeAssertTest` red)**

Run: `python -m unittest discover -s tests -v`
Expected: `MarkCanonicalV3Test` and `CanonicalTest` PASS now. `LeafShapeAssertTest` is still red until Task 6 (series carry `statistic`/`cv`; v2 assert rejects them). If anything besides `LeafShapeAssertTest` fails, stop and investigate.

Note for the reviewer: `CanonicalTest.test_canonical_flag_corn_grain_not_silage` and `test_canonical_flag_absent_when_no_match` still pass because (corn, YIELD)'s rule is `util=GRAIN` (marks grain, not silage) and the Kansas fixture wheat row is `class=WINTER` (no match to the `ALL CLASSES` rule, so counted missing). These were verified against the fixture.

- [ ] **Step 6: Commit**

```bash
git add scripts/refresh.py tests/test_refresh.py
git commit -m "feat(refresh): candidate-counting canonical guard, YIELD-scoped ratio"
```

---

### Task 6: Leaf shape v3 + atomic schema_version bump across all emitters

**Files:**
- Modify: `scripts/refresh.py:309-310` (`_series_sort_key`)
- Modify: `scripts/refresh.py:594` and `602-605` (`_assert_leaf_shape`)
- Modify: `scripts/refresh.py:410, 440, 462, 495, 514` (the five `"schema_version": 2` literals)
- Modify: `tests/test_refresh.py` (new tests; bump existing v2 literals to v3)

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_refresh.LeafV3ShapeTest tests.test_refresh.AllArtifactsV3Test -v`
Expected: FAIL (`_assert_leaf_shape` requires `schema_version == 2` and rejects the new series keys; emitters still write 2).

- [ ] **Step 3: Add `statistic` to the series sort key**

Replace `_series_sort_key` (lines 309-310) with:

```python
def _series_sort_key(s: dict) -> tuple:
    return (s["statistic"], s["class"], s["prodn_practice"], s["util_practice"], s["unit"], s["short_desc"])
```

- [ ] **Step 4: Update `_assert_leaf_shape` to v3**

Change the version check (line 594) to:

```python
    if leaf["schema_version"] != 3:
        raise SystemExit(f"Leaf schema_version not 3: {leaf['schema_version']!r}")
```

And the `required_series` set (lines 602-605) to:

```python
    required_series = {
        "statistic", "class", "prodn_practice", "util_practice", "unit", "short_desc",
        "values", "cv", "suppressed", "raw",
    }
```

- [ ] **Step 5: Bump every emitter's `schema_version` literal 2 to 3**

Change `"schema_version": 2,` to `"schema_version": 3,` at: line 410 (`emit_index`), 440 (`emit_state_meta`), 462 (`emit_point_leaves`), 495 (`emit_crop_rollups`), 514 (`emit_audit`).

- [ ] **Step 6: Bump the existing tests' hardcoded v2 expectations to v3**

In `tests/test_refresh.py`, change `schema_version` assertions/fixtures from 2 to 3 in: `IndexTest.test_index_carries_refreshed_at_and_source` (line 355), `PointLeafTest.test_point_leaf_shape_minimal_and_complete` (line 379), `CropRollupTest.test_crop_rollup_includes_all_counties_for_that_crop` (line 472), `AuditTest.test_audit_carries_header_observed` (line 505), and both fixtures in `LeafShapeAssertTest` (lines 704 and 716). Also, those two `LeafShapeAssertTest` fixtures hand-build series dicts lacking `statistic`/`cv`; add `"statistic": "YIELD",` and `"cv": {},` to the series dict in `test_leaf_shape_assert_rejects_extra_top_key` (the series list is empty there, so only the top-level `schema_version` needs 3) and to the series in `test_leaf_shape_assert_rejects_missing_series_keys` keep it INTACT as a deliberately-broken series (that test asserts a missing-keys abort, so leave its series missing keys; only bump its `schema_version` to 3 so the version check passes before the series check runs).

- [ ] **Step 7: Run the whole suite to verify pass**

Run: `python -m unittest discover -s tests -v`
Expected: PASS, all green.

- [ ] **Step 8: Commit**

```bash
git add scripts/refresh.py tests/test_refresh.py
git commit -m "feat(refresh): leaf shape v3 + atomic schema_version 2->3 across emitters"
```

---

### Task 7: Yield-only rollup via filtered copy

**Files:**
- Modify: `scripts/refresh.py:484-492` (`emit_crop_rollups` per-county block)
- Modify: `tests/test_refresh.py` (new tests)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_refresh.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_refresh.RollupYieldOnlyTest -v`
Expected: FAIL (`test_rollup_excludes_non_yield_series`: rollup carries both YIELD and PRODUCTION).

- [ ] **Step 3: Filter the rollup county series to YIELD only**

In `emit_crop_rollups`, replace the per-county block (lines 484-492) with:

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

- [ ] **Step 4: Run the whole suite to verify pass**

Run: `python -m unittest discover -s tests -v`
Expected: PASS, all green. (`CropRollupTest.test_crop_rollup_includes_all_counties_for_that_crop` still expects 2 series for Iowa Story corn: the fixture's two corn series are both YIELD, grain + silage, so the yield-only filter keeps both.)

- [ ] **Step 5: Commit**

```bash
git add scripts/refresh.py tests/test_refresh.py
git commit -m "feat(refresh): rollup stays yield-only via filtered copy"
```

---

### Task 8: Per-family Gate 2 baseline map (leaf)

**Files:**
- Modify: `scripts/refresh.py` (add `leaf_baseline` after `save_state`, around line 628)
- Modify: `scripts/refresh.py:713` (validate call) and `:771` (save_state payload)
- Modify: `tests/test_refresh.py` (new tests)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_refresh.py`:

```python
class BaselineMapTest(unittest.TestCase):
    def test_legacy_int_baseline_treated_as_absent(self):
        self.assertIsNone(refresh.leaf_baseline({"last_filtered_row_count": 1318932}))

    def test_map_baseline_read(self):
        self.assertEqual(
            refresh.leaf_baseline({"last_filtered_row_count": {"leaf": 4300000}}),
            4300000)

    def test_absent_baseline_is_none(self):
        self.assertIsNone(refresh.leaf_baseline({}))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_refresh.BaselineMapTest -v`
Expected: FAIL (`refresh.leaf_baseline` does not exist).

- [ ] **Step 3: Add the `leaf_baseline` helper**

In `scripts/refresh.py`, after `save_state` (after line 628), add:

```python
def leaf_baseline(state: dict) -> Optional[int]:
    """Per-family Gate 2 baseline for the leaf family.

    Returns None (bootstrap, no abort) when the baseline is absent or stored in
    the legacy scalar shape, since the v2->v3 row count changes ~3.3x and a
    legacy scalar is not a valid v3 leaf baseline.
    """
    counts = state.get("last_filtered_row_count")
    if isinstance(counts, dict):
        return counts.get("leaf")
    return None
```

- [ ] **Step 4: Use it in `main` and write the map on save**

In `main`, change the `validate(...)` call (line 713) to:

```python
    validate(total_rows, len(kept_rows), leaf_baseline(state))
```

And in the `save_state({...})` payload, change the `last_filtered_row_count` line (line 771) to:

```python
        "last_filtered_row_count": {"leaf": len(kept_rows)},
```

- [ ] **Step 5: Run the whole suite to verify pass**

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
- Modify: `scripts/refresh.py:111-120` (`discover`: `inclusive` param + bound)
- Modify: `scripts/refresh.py:675-707` (`main`: compute `bootstrap_needed` before early returns; guard `is_caught_up`)
- Modify: `tests/test_refresh.py` (new tests)

- [ ] **Step 1: Write the failing test for `discover`**

Add to `tests/test_refresh.py`:

```python
class DiscoverInclusiveTest(unittest.TestCase):
    def test_inclusive_includes_last_known(self):
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

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m unittest tests.test_refresh.DiscoverInclusiveTest -v`
Expected: FAIL (`discover()` has no `inclusive` parameter).

- [ ] **Step 3: Add the `inclusive` parameter**

Change the `discover` signature (line 111) to:

```python
def discover(last_known: Optional[date], today: date, inclusive: bool = False) -> Optional[dict]:
```

And the earliest-bound computation (lines 118-120) to:

```python
    earliest = today - timedelta(days=PROBE_WINDOW_DAYS)
    if last_known:
        offset = timedelta(days=0) if inclusive else timedelta(days=1)
        earliest = max(earliest, last_known + offset)
```

- [ ] **Step 4: Run the discover test to verify pass**

Run: `python -m unittest tests.test_refresh.DiscoverInclusiveTest -v`
Expected: PASS.

- [ ] **Step 5: Write the failing test for the `main` reorder**

Add to `tests/test_refresh.py`:

```python
class MainBootstrapReemitTest(unittest.TestCase):
    def test_caught_up_but_missing_index_reaches_discover(self):
        calls = {}

        def fake_discover(last_known, today, inclusive=False):
            calls["inclusive"] = inclusive
            return None  # abort after; we only assert we reached discover

        with mock.patch.object(refresh, "load_state", return_value={
                    "last_successful_date": "2026-05-23", "last_etag": '"x"'}), \
             mock.patch.object(refresh, "_index_path",
                               return_value=Path("/nonexistent/index.json")), \
             mock.patch.object(refresh, "sp_a_bootstrap_needed", return_value=False), \
             mock.patch.object(refresh, "discover", side_effect=fake_discover), \
             mock.patch.object(refresh, "ping_healthchecks"):
            rc = refresh.main(today=date(2026, 5, 23))
        self.assertEqual(calls.get("inclusive"), True)
        self.assertEqual(rc, 1)
```

- [ ] **Step 6: Run it to verify it fails**

Run: `python -m unittest tests.test_refresh.MainBootstrapReemitTest -v`
Expected: FAIL (current `main` early-returns 0 at `is_caught_up` before computing `bootstrap_needed`, so `discover` is never called).

- [ ] **Step 7: Reorder `main` so bootstrap is computed before the early returns**

In `main`, replace the block from the first `print("Last known...")` (line 675) through the `download_with_retry(...)` call (line 707). The current code has the `is_caught_up` early return at the top and the `bootstrap_needed` assignment further down (line 696); move bootstrap above both returns and guard the caught-up return:

```python
    print(f"Last known publication: {last_known}; today: {today}")

    # Compute bootstrap need BEFORE the early returns: a missing index or a
    # missing family audit must suppress the caught-up / ETag shortcut so the
    # run re-emits the absent family. See spec section 4.7. Foundation's
    # bootstrap set is index + planting-windows (SP-A); prices/derived add
    # their own terms in later phases, only once their emitters exist.
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

This deletes the old standalone bootstrap-guard comment + `bootstrap_needed = ...` line (the lines that were at 692-696) since it now lives above the early returns.

- [ ] **Step 8: Run the whole suite to verify pass**

Run: `python -m unittest discover -s tests -v`
Expected: PASS, all green. `MainCaughtUpTest.test_main_returns_zero_when_already_caught_up_today` still passes: its mocked state is caught-up, and it runs against the real worktree `data/` tree where `data/index.json` and `data/_audit/planting-windows.json` both exist, so `bootstrap_needed` is False and the guarded caught-up return fires. If `MainCaughtUpTest` fails, the worktree is missing those files; add `mock.patch.object(refresh, "_index_path", ...)` to an existing file and `mock.patch.object(refresh, "sp_a_bootstrap_needed", return_value=False)` to that test.

- [ ] **Step 9: Commit**

```bash
git add scripts/refresh.py tests/test_refresh.py
git commit -m "feat(refresh): bootstrap re-emit before early returns + inclusive discover"
```

---

### Task 10: Create `data/_schema/leaf.json` at v3 + fix README link

**Files:**
- Create: `data/_schema/leaf.json`
- Modify: `README.md`
- Modify: `tests/test_refresh.py` (new tests)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_refresh.py`:

```python
class LeafSchemaFileTest(unittest.TestCase):
    def test_leaf_schema_exists_and_is_v3(self):
        p = Path(refresh.DATA_DIR) / "_schema" / "leaf.json"
        self.assertTrue(p.exists(), "data/_schema/leaf.json must exist")
        schema = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"].get("const"), 3)

    def test_leaf_schema_series_requires_statistic_and_cv(self):
        p = Path(refresh.DATA_DIR) / "_schema" / "leaf.json"
        schema = json.loads(p.read_text(encoding="utf-8"))
        required = set(schema["properties"]["series"]["items"]["required"])
        self.assertTrue({"statistic", "cv"} <= required)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m unittest tests.test_refresh.LeafSchemaFileTest -v`
Expected: FAIL (file does not exist).

- [ ] **Step 3: Create `data/_schema/leaf.json`**

Create `data/_schema/leaf.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/_schema/leaf.json",
  "title": "NASS county point leaf (v3)",
  "description": "One leaf per (state, county, crop). Multi-statistic: each series carries a statistic, a values map, and a parallel cv map (NASS CV_%). At most one canonical series per statistic.",
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

Locate them: `grep -n "schema_version\|leaf.json\|s.canonical\|select(.canonical)\|\.find(s =>" README.md`. Then:
- change documented `"schema_version": 2` in leaf/index/rollup examples to `3`;
- change the canonical-pick guidance so the consumer filters `canonical && statistic === "YIELD"` (a v3 leaf has one canonical per statistic);
- note that each series now carries `statistic` and a parallel `cv` map.

- [ ] **Step 5: Run the whole suite to verify pass**

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

Add to `tests/test_refresh.py`. Drives `group_by_state` -> `sort_series` -> `mark_canonical` -> `emit_point_leaves` -> `emit_crop_rollups`, covering the canonical-table traps (corn AREA PLANTED util, corn AREA HARVESTED vs silage):

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
            # Silage AREA HARVESTED at the same class/prodn/unit but util=SILAGE
            # must NOT be marked canonical for AREA HARVESTED.
            _row(STATISTICCAT_DESC="AREA HARVESTED", UNIT_DESC="ACRES", VALUE="200",
                 UTIL_PRACTICE_DESC="SILAGE", SHORT_DESC="CORN, SILAGE - ACRES HARVESTED"),
        ]
        states = refresh.group_by_state(rows)
        refresh.sort_series(states)
        missing, _ = refresh.mark_canonical(states)
        self.assertEqual(missing, 0)

        com = states["19"]["counties"]["169"]["commodities"]["corn"]
        canon = {s["statistic"] for s in com["series"] if s.get("canonical")}
        self.assertEqual(canon, {"YIELD", "PRODUCTION", "AREA HARVESTED", "AREA PLANTED"})
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
Expected: PASS (all prior tasks make this green; a failure pinpoints which task regressed).

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

- **Helpers are introduced once, in Task 1:** `_row()` (dict, for `group_by_state`/emit tests) and `_filter()` (list, for `_parse_filter` tests), plus `from unittest import mock`. Every later task relies on them.
- **The fixture's "dropped" row 11 becomes `STOCKS` in Task 2** so the original count assertions (7 kept) stay valid; PRODUCTION/AREA coverage lives in the new helper-based tests.
- **Intentionally-red window:** after Task 3, `LeafShapeAssertTest` (and briefly `CanonicalTest`) fail because series gained `statistic`/`cv` and the canonical keying changed; the suite returns fully green at Task 6. Each task states its expected red/green so a per-task reviewer is not surprised.
- **Not in this plan:** prices, derived families, the multi-crop bundle, and the per-family baseline/bootstrap terms for those families. Each is its own plan (spec phases 2-4), authored after Foundation merges so it builds on the real `run_*` result-object shape and the bootstrap-term phasing rule established here.
- **Before merge/publish:** the field-mcp picker (spec 4.2) must be deployed and verified live first. This plan produces no data publish on its own; publishing happens when the refresh cron runs against `@main` after merge, so the ordering gate lands on the merge/publish step, tracked on the field-mcp PR.
