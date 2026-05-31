# NASS Prices, Production/Area, CV, and Precomputed Joins

**Date:** 2026-05-31
**Branch:** `worktree-feat+nass-prices-stats-derived`
**Status:** Design, revised once after codex-spec round 1 (9 findings folded in: order-independent consumer picker, bootstrap-before-early-return, per-family validate at emit sites, the verified canonical rule table, explicit marketing-year mapping, derived-path fix, rollup shared-list filter, CV_% required column, schema-history correction). Pending codex-spec round 2, then user approval, then writing-plans.
**Base:** `02e2f17` (origin/main)

## 1. Problem

`scripts/refresh.py` downloads the weekly NASS crops bulk file (~23.7M rows) and keeps only 5.56% of it: county SURVEY ANNUAL YIELD rows for corn/soybeans/wheat (~1.32M rows). Everything else is downloaded and discarded. Verified against the live 2026-05-30 file, the discard pile includes, for the same three crops:

- County **PRODUCTION** (~1.06M county/survey/annual rows), **AREA HARVESTED** (~1.06M), **AREA PLANTED + AREA PLANTED, NET** (~0.94M). Same grain as the yields we already publish; blocked only by the `STATISTICCAT_DESC == "YIELD"` clause.
- **PRICE RECEIVED** in `$ / BU`, at STATE and NATIONAL aggregation only (never COUNTY), with monthly and marketing-year reference periods.
- The **`CV_%`** column (NASS coefficient of variation, the sampling reliability of each estimate), dropped even on rows we keep.

The customer ask that motivated this (FIE market-recap, Rittgers discovery call) wants a grain-price recap for context on rent and profitability. NASS state price is the free, already-downloaded source for it.

## 2. Goal

Stop discarding the valuable rows. Publish prices, production/area, a reliability signal, and a set of precomputed joins, while staying within jsDelivr's 20 MB/file limit and keeping the producer's zero-dependency, fail-fast, touched-only-write design.

## 3. Consumer reality (decisive context)

The only known consumer is `field-mcp` (`apps/gateway/src/lib/providers/usda/yields-cache.ts`). It fetches **only** per-county leaves (`data/states/{fips}/counties/{code}/{crop}.json`), reads the canonical series' `values` as bu/acre, and falls back to the NASS QuickStats API on a cache miss. It has **no** consumer for prices, production, area, or rollups.

jsDelivr traffic over the repo's lifetime (339 hits total) was pulled and bucketed. Leaf traffic (119) is consistent with field-mcp. Rollup traffic (144) and `/data/v1/states/*` traffic (31) are **not** field-mcp's pattern; git history shows `/data/v1/` was a published path deleted in commit `981e1ee`, so that traffic is stale-cache/bot noise against a dead URL. Rollups remain a live, README-advertised path, so their traffic cannot be fully attributed and we do not delete them.

The owner has decided to treat field-mcp as the sole real consumer and to update it in lockstep with the leaf schema change. This permits a breaking leaf change here, paired with a matching consumer change there.

## 4. Decisions (locked)

### 4.1 Leaf v3 (breaking, lockstep with field-mcp)

Replace the v2 county leaf with a v3 multi-statistic leaf. Each `series[]` entry gains:

- `statistic`: one of `YIELD`, `PRODUCTION`, `AREA HARVESTED`, `AREA PLANTED`, `AREA PLANTED, NET`.
- `cv`: a map `{year: cv_percent}` parallel to `values`, carrying `CV_%` where NASS publishes it (absent/blank where it doesn't).

`canonical` is redefined to mean **canonical for its statistic**. A leaf may therefore have multiple `canonical: true` series, at most one per statistic.

The producer filter at `refresh.py:185-192` is widened: keep county/SURVEY/ANNUAL/YEAR/TOTAL/NOT SPECIFIED rows for the three crops where `STATISTICCAT_DESC` is any of the five statistics above (not just YIELD).

**Why production/area go into the leaf (not a separate family):** the separate-family option existed only to protect an unknown public consumer. With field-mcp as the sole consumer and updated in lockstep, the single-leaf shape is simpler and is the natural home for per-county series. The yield series within the leaf is byte-stable except for the two added fields.

#### 4.1.1 Canonical rule table, per (crop, statistic) (closes codex P1 "one canonical per statistic")

The current rule (`refresh.py:42-46`) keys canonical on `(class, prodn_practice, unit)` per crop. That is insufficient once non-yield statistics enter the leaf. Verified against the live 2026-05-30 file (full scan of county/SURVEY/ANNUAL/YEAR/TOTAL rows), the canonical aggregate for every (crop, statistic) is the `class = ALL CLASSES` (wheat aggregate is `ALL CLASSES`, **not** the more-numerous `WINTER`) + `prodn_practice = ALL PRODUCTION PRACTICES` row, but **`util_practice` and `unit` vary by statistic**, and for corn `util_practice` is not constant across statistics:

| crop | statistic | class | prodn_practice | util_practice | unit |
|---|---|---|---|---|---|
| corn | YIELD | ALL CLASSES | ALL PRODUCTION PRACTICES | GRAIN | BU / ACRE |
| corn | PRODUCTION | ALL CLASSES | ALL PRODUCTION PRACTICES | GRAIN | BU |
| corn | AREA HARVESTED | ALL CLASSES | ALL PRODUCTION PRACTICES | GRAIN | ACRES |
| corn | AREA PLANTED | ALL CLASSES | ALL PRODUCTION PRACTICES | **ALL UTILIZATION PRACTICES** | ACRES |
| corn | AREA PLANTED, NET | ALL CLASSES | ALL PRODUCTION PRACTICES | GRAIN | ACRES |
| soybeans | YIELD | ALL CLASSES | ALL PRODUCTION PRACTICES | ALL UTILIZATION PRACTICES | BU / ACRE |
| soybeans | PRODUCTION | ALL CLASSES | ALL PRODUCTION PRACTICES | ALL UTILIZATION PRACTICES | BU |
| soybeans | AREA HARVESTED | ALL CLASSES | ALL PRODUCTION PRACTICES | ALL UTILIZATION PRACTICES | ACRES |
| soybeans | AREA PLANTED | ALL CLASSES | ALL PRODUCTION PRACTICES | ALL UTILIZATION PRACTICES | ACRES |
| soybeans | AREA PLANTED, NET | ALL CLASSES | ALL PRODUCTION PRACTICES | ALL UTILIZATION PRACTICES | ACRES |
| wheat | YIELD | ALL CLASSES | ALL PRODUCTION PRACTICES | ALL UTILIZATION PRACTICES | BU / ACRE |
| wheat | PRODUCTION | ALL CLASSES | ALL PRODUCTION PRACTICES | ALL UTILIZATION PRACTICES | BU |
| wheat | AREA HARVESTED | ALL CLASSES | ALL PRODUCTION PRACTICES | ALL UTILIZATION PRACTICES | ACRES |
| wheat | AREA PLANTED | ALL CLASSES | ALL PRODUCTION PRACTICES | ALL UTILIZATION PRACTICES | ACRES |
| wheat | AREA PLANTED, NET | ALL CLASSES | ALL PRODUCTION PRACTICES | ALL UTILIZATION PRACTICES | ACRES |

Two verified traps this table encodes, each of which a per-crop (statistic-blind) rule would get wrong:

1. **Corn `util_practice` is `GRAIN` for four statistics but `ALL UTILIZATION PRACTICES` for AREA PLANTED.** A rule that pinned `util=GRAIN` for all corn area would never mark corn AREA PLANTED canonical (it would land in Gate-3 missing-canonical and degrade silently). The util value is per (crop, statistic), not per crop.
2. **`unit` alone does not disambiguate corn area statistics.** Corn AREA HARVESTED at `ALL CLASSES/ALL PRODUCTION PRACTICES/ACRES` collides with SILAGE and FORAGE rows at the same class/prodn/unit; only `util_practice` separates them. So `util_practice` is a required match field, not optional.

Implementation: `CANONICAL_RULES` becomes a `dict[(crop_slug, statistic)] -> {class, prodn_practice, util_practice, unit}`. `mark_canonical` scopes candidate series to the target statistic **first**, then matches the 4-tuple (necessary because corn AREA HARVESTED and AREA PLANTED, NET share the identical 4-tuple `ALL CLASSES/ALL PRODUCTION PRACTICES/GRAIN/ACRES` and are distinguished only by statistic). The match was verified unique within each (crop, statistic) on the live file, so exactly one series per (crop, statistic) is marked. A module-load assertion (mirroring the existing one at `refresh.py:54-57`) requires every (crop in COMMODITY_ALLOWLIST x statistic in the five) to have a rule, so a future commodity or statistic cannot silently ship without a canonical rule.

### 4.2 Consumer-corruption guard (codex P1)

The live field-mcp picker is `series.find(s => s.canonical === true)` with no statistic filter. With multiple canonical series, the producer's `sort_series` (orders by `class, prodn_practice, util_practice, unit, short_desc`) would place `AREA HARVESTED` / `PRODUCTION` series before `YIELD`, so the unmodified picker would return acres/production where it expects bu/acre, silently. Two mitigations, both required:

1. **Producer gate:** two checks. (a) **Hard, per leaf:** each `statistic` present has at most one `canonical: true`; abort on any leaf with two canonical series of the same statistic. (b) **Ratio, preserving the existing Gate 3:** the fraction of (county, crop) pairs lacking a `YIELD` canonical stays under `CANONICAL_MISSING_TOLERANCE` (5%), exactly as today (`refresh.py:559-577`), now scoped to the YIELD statistic specifically. This is a tolerance, not a per-leaf hard requirement: a county that publishes acres but not yield is allowed (it counts toward the ratio), matching current soft-fail semantics. The non-YIELD statistics are not ratio-gated (their coverage varies legitimately by county).
2. **Consumer change (field-mcp, lockstep), order-independent (codex P1).** The picker must become **backward-compatible** so deploy order does not matter: `series.find(s => s.canonical && (s.statistic === "YIELD" || s.statistic === undefined))`. A v2 leaf (no `statistic`, one canonical yield) and a v3 leaf (multiple canonical, one per statistic) both resolve to the yield series under this picker. This removes the original "leaf not deployed until consumer ready" ordering constraint: the new consumer reads old v2 CDN data correctly (matches on `statistic === undefined`), and the old consumer would mis-read v3 data, so the consumer ships first or simultaneously, never after. The producer-side guard (mitigation 1) plus this picker make both deploy orders safe.

### 4.3 Keep the yield rollup, yield-only

`data/states/{fips}/crops/{crop}.json` is kept. It is the most-fetched data type in the traffic sample and is README-advertised; deleting it would create a multi-hour 404 window (codex P1/P2). It stays **yield-only** (production/area/price are not rolled up), which keeps it within its current size envelope (largest today: TX wheat 3.16 MB, far under 20 MB). The canonical yield series gains the v3 `statistic`/`cv` fields for consistency with leaves.

**Shared-list hazard (codex P2).** `emit_crop_rollups` currently copies `com["series"]` by reference into each county block (`refresh.py:487-492`). Once the in-memory commodity now carries production/area series too, that reference would leak non-yield series into the rollup. The rollup emitter must build a **filtered copy** per county, `[s for s in com["series"] if s["statistic"] == "YIELD"]`, never the shared list. This is called out as an explicit implementation constraint and gets a dedicated test (a commodity with mixed-statistic series must produce a yield-only rollup). No new rollups are created; new-statistic rollups were the only thing that would have breached 20 MB, and they are out of scope.

### 4.4 Prices: new family, strict filter

New family `data/prices/states/{fips}/{crop}.json` (state-level; a national file may be added if a consumer needs it, out of scope now). Second-pass module `scripts/prices.py` over the same download, following the SP-A `planting_windows.py` pattern (own schema + audit, hooked into `refresh.main()` before `prune_stale`).

Strict filter (codex P1): keep only `STATISTICCAT_DESC == "PRICE RECEIVED"` **and** `UNIT_DESC == "$ / BU"` at `AGG_LEVEL_DESC == "STATE"`. This excludes `PCT OF PARITY`, `PRICE RECEIVED AFTER REPORT`, `PRICE RECEIVED PRIOR TO CLOSING`, `PRICE RECEIVED, PARITY`, and the 10-year-average parity variants.

Reference periods are stored as two **separate** series per (state, crop): a `MARKETING YEAR` annual series and a `MONTHLY` series (keyed by month). Mixing them in one map would be ambiguous.

**Wheat class policy:** NASS publishes both a classless `WHEAT - PRICE RECEIVED` aggregate and per-class (`WINTER`, `SPRING`, `(EXCL DURUM)`, etc.) price rows at state level. Mark the classless aggregate `canonical: true`; retain class variants as additional series. This mirrors the yield canonical rule (`ALL CLASSES`).

### 4.5 Derived families (separate, SP-A pattern)

All five, each a separate sharded family with its own schema + audit, computed at emit time from in-memory state (production/area/price are colocated during the run):

a. **`state_price_imputed_revenue_per_acre`** (renamed from "revenue/acre", codex P2): `county yield x state marketing-year price`, per year, as both per-harvested-acre and per-planted-acre. The name makes the state-price imputation explicit so a report never implies a county-specific price. Join uses the commodity marketing-year convention (see 4.6).

b. **County rank + percentile** within-state and within-nation, per year, on canonical yield. Lives in `data/states/{fips}/derived/state-{crop}.json` (the per-state file that recovers the comparison-scan use case the rollup served; path matches the target tree in section 5).

c. **Production-weighted state/national yield** = `sum(production) / sum(area harvested)`. The correct aggregate; replaces the naive county-mean that a consumer would otherwise compute wrong.

d. **Per-series derived stats**: trailing 5- and 10-year average, year-over-year %, linear trend slope. Suppressed years are skipped, not treated as zero.

e. **Multi-crop bundle** `data/bundles/{fips}/{code}.json`: corn+soy+wheat canonical raw + headline derived in one fetch. Flagged YAGNI (leaves are ~5 KB and warm-cache fast, so three fetches is already cheap); retained because the owner asked for all five. First candidate to cut if scope tightens.

### 4.6 Marketing-year join, explicit mapping (codex P2)

Yield `values` are keyed by `YEAR` (the harvest/crop year). NASS marketing years are labeled by their **starting** calendar year. The join is: **a crop's `yield[Y]` joins to the marketing-year price labeled `Y`**, because that crop is harvested in fall of year `Y` and marketed across the marketing year that begins in `Y`. Concretely:

| Crop | Marketing year span | Yield year `Y` joins to price marketing-year label |
|---|---|---|
| Corn | Sep `Y` - Aug `Y+1` | `Y` |
| Soybeans | Sep `Y` - Aug `Y+1` | `Y` |
| Wheat | Jun `Y` - May `Y+1` | `Y` |

So `corn yield[2024]` (harvested fall 2024) joins to the `MARKETING YEAR` price row whose NASS marketing-year label is `2024` (the Sep-2024-through-Aug-2025 average). The derived output records `{yield_year: 2024, marketing_year: 2024, price, revenue_per_acre}` so the join is auditable, and emits nothing for a year where either side is absent or suppressed (no silent zero). The single edge nuance (NASS labels the marketing year by its start year, which equals the harvest year for all three crops here) is why the mapping is identity on the label; it is stated explicitly so a future spring-planted or southern-hemisphere commodity addition does not blindly reuse it.

### 4.7 Migration and pipeline safety

- **Per-family Gate 2 baselines, validated at each emit site (codex P1).** Today `validate(total_rows, kept_rows, last_filtered_row_count)` is called exactly once in `main()` immediately after `stream_filter()` and before grouping (`refresh.py:709-713`), against a single global `last_filtered_row_count`. That single pre-group call structurally cannot validate per-family counts, and the leaf jump from ~1.32M to ~4.3M would abort the first run. Restructure: `validate()` becomes per-family, called at each family's own point in the pipeline, not once up front. The leaf family validates its kept-count after the widened filter/group; `prices.py` and `derived.py` each validate their own kept/emitted count inside their `run_*` entrypoint. `.refresh-state.json` carries a baseline map, e.g. `last_filtered_row_count: {leaf: N, prices: N, derived: N}`, each with its own +-10% band and bootstrap-tolerant on first sight of a family (returns without aborting when its baseline is absent). No manual one-time global override that could silently disable the gate. The global `last_filtered_row_count` integer is migrated to this map shape in the same change; `save_state` writes the map.
- **Per-family bootstrap sentinels, checked before the caught-up early return (codex P1).** `main()` returns early on `is_caught_up(...)` (`refresh.py:676-679`) and again on ETag match (`refresh.py:698-701`), both **before** `bootstrap_needed` is computed (`refresh.py:696`). So a same-day or same-ETag rerun after a prior success never reaches any re-emit logic. SP-A already dodges this because its sentinel feeds `bootstrap_needed` and the ETag-match branch is guarded by `and not bootstrap_needed`; but the `is_caught_up` branch at the very top is **not** so guarded. Fix: compute a combined `bootstrap_needed` (index + every family audit sentinel: `_audit/latest.json`, `_audit/planting-windows.json`, `_audit/prices.json`, `_audit/derived.json`) **before** both early returns, and let a missing sentinel suppress the early return so the run proceeds to emit the absent family. Each new family contributes a `*_bootstrap_needed()` check exactly like SP-A's `sp_a_bootstrap_needed()` (`planting_windows.py:384-386`). This is a change to control flow at the top of `main()`, not just an added sentinel.
- **Atomic schema migration (codex P1).** `_assert_leaf_shape` hard-requires `schema_version == 2` and rejects unknown series keys (`refresh.py:594,602,612`); the assert, the schema file, the tests, and the emitters bump to v3 in one change so the pipeline never aborts mid-migration.
- **`CV_%` added to `REQUIRED_COLS` (codex P2).** `cv` is sourced from the bulk file's `CV_%` column, which the current header carries (`data/_audit/latest.json`) but `REQUIRED_COLS` omits (`refresh.py:59-67`). Add `CV_%` to `REQUIRED_COLS` so a NASS rename/drop trips Gate 1 (missing-required-column abort) loudly, instead of the leaf silently emitting empty `cv` maps forever.
- **Create `data/_schema/leaf.json` (codex flagged; corrected on verification).** README references this file as a published contract and is absent at HEAD; codex inferred "never existed," but git history shows it was committed (`f942378`, `f074e11`) and later removed, so the leaf shape is currently enforced only in Python. Net effect is the same: create it at v3 (JSON Schema 2020-12) and correct the README link. Add `data/_schema/price.json`, `data/_schema/derived-county.json`, `data/_schema/derived-state.json`.

## 5. Target tree (diff from today)

```
data/
  index.json                                    schema_version 2 -> 3; advertises new families
  _schema/
    leaf.json                  CREATE (v3; existed historically, was removed, absent now; README still links it)
    price.json                 CREATE
    derived-county.json        CREATE
    derived-state.json         CREATE
    planting-window.json                        (unchanged)
  _audit/
    latest.json                                 (unchanged)
    prices.json                CREATE
    derived.json               CREATE
    planting-windows.json, window-coverage.json (unchanged)
  states/{fips}/
    meta.json                                   (unchanged)
    counties/{code}/{crop}.json                 LEAF v3: yield+production+area series, each value carries cv
    crops/{crop}.json                           ROLLUP v3, yield-only (kept; not dropped)
    derived/state-{crop}.json  CREATE  prod-weighted state+national yield; per-county rank/percentile
  prices/states/{fips}/{crop}.json  CREATE  state PRICE RECEIVED $/bu (marketing-year + monthly series)
  derived/{fips}/counties/{code}/{crop}.json  CREATE  rank, percentile, imputed revenue/acre, trailing/YoY/trend
  bundles/{fips}/{code}.json  CREATE (YAGNI candidate)  corn+soy+wheat canonical raw + headline derived
  planting-windows/{fips}/{slug}.json           (unchanged)
```

## 6. Architecture

New stdlib-only modules mirror `planting_windows.py`: `scripts/prices.py` and `scripts/derived.py`, each with a `run_*` entrypoint returning its emitted paths, called from `refresh.main()` after the leaf/rollup emit and before `prune_stale` (so their paths join the `expected` set and are not pruned). `derived.py` consumes the in-memory grouped state plus the price result, so revenue/rank/weighted-yield are computed without a second parse.

The leaf/rollup change is internal to `refresh.py`: add `CV_%` to `REQUIRED_COLS`; widen the filter to the five statistics; carry `statistic`/`cv` through `group_by_state` and the series dict; generalize `mark_canonical` to the per-`(crop, statistic)` rule table (section 4.1.1); generalize `_assert_leaf_shape` to v3; and make `emit_crop_rollups` build a yield-only filtered copy (section 4.3). The single global `validate()` call is replaced by per-family validation at each emit site, and the top-of-`main()` early returns are reordered behind a combined `bootstrap_needed` (section 4.7).

## 7. Phasing

1. **Foundation:** leaf v3 (yield + production + area + cv), yield-only v3 rollup, create `leaf.json`, generalize canonical to per-statistic + new guard gate, per-family Gate 2 baselines, atomic schema/assert/test bump, README fix. Plus the lockstep field-mcp picker change (`canonical && statistic === "YIELD"`). Everything depends on this.
2. **Prices:** `prices.py` + `price.json` schema + audit + bootstrap sentinel.
3. **Derived:** `derived.py` + schemas + audit + bootstrap sentinel (rank/percentile, prod-weighted yield, imputed revenue/acre with marketing-year join, trailing/YoY/trend).
4. **Bundle:** `bundles/` (YAGNI candidate; cut if scope tightens).

Each phase is independently shippable and testable. TDD per phase: tests for the new shape/filter/gate precede implementation.

## 8. Testing

- Filter: production/area/price rows kept or excluded exactly; parity and after-report price rows excluded; monthly vs marketing-year separation.
- Leaf v3: `statistic` present on every series; `cv` parallel to `values`; exactly one canonical per statistic.
- Canonical table (section 4.1.1): corn AREA PLANTED resolves to the `ALL UTILIZATION PRACTICES` series (not GRAIN); corn AREA HARVESTED resolves to GRAIN, not SILAGE/FORAGE, at the shared `ACRES` unit; wheat YIELD resolves to the `ALL CLASSES` aggregate, not WINTER; each (crop, statistic) marks exactly one series on a fixture built from real tuples.
- Guard gate: (a) a leaf with two canonical series of the same statistic aborts; (b) the YIELD missing-canonical ratio gate still fires past 5%, and a county with acres-but-no-yield is tolerated (counts toward ratio, does not hard-abort).
- Rollup stays yield-only and within size envelope.
- Gate 2 per-family: first sight of a family bootstraps; subsequent +-10% drift aborts.
- Bootstrap sentinels: same-ETag run with a missing family audit re-emits that family.
- Marketing-year join: corn yield[2024] maps to the documented marketing-year price; suppressed years skipped.
- Derived math: prod-weighted yield equals sum(prod)/sum(area) on a fixture; rank/percentile correct on a small state.

## 9. Out of scope

National price files, census source, other commodities, sub-state/zip/watershed aggregations, futures/basis, and any non-NASS source. New-statistic rollups (would breach 20 MB). These remain deliberate future scope, not accidental omissions.

## 10. Risks

- **"Sole consumer" is a well-founded bet, not proof.** The repo is presented publicly. If an unknown consumer depends on the v2 leaf shape, the lockstep change breaks them. Bounded downside; owner's risk to accept.
- **Tree growth** ~93 MB -> ~160-180 MB (estimate, +-30%). All files stay far under 20 MB. Weekly churn stays ~0 via `write_if_changed`; the annual all-leaf rewrite (new year value) grows proportionally. Slow-burn git-history watch-item, not a wall.
- **field-mcp lockstep** is a cross-repo coordination point. The leaf v3 deploy and the field-mcp picker change must land together; this spec governs only this repo, and the field-mcp change is tracked as an external dependency.
