# NASS Prices, Production/Area, CV, and Precomputed Joins

**Date:** 2026-05-31
**Branch:** `worktree-feat+nass-prices-stats-derived`
**Status:** Design. Pending codex-spec adversarial review, then user approval, then writing-plans.
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

### 4.2 Consumer-corruption guard (codex P1)

The live field-mcp picker is `series.find(s => s.canonical === true)` with no statistic filter. With multiple canonical series, the producer's `sort_series` (orders by `class, prodn_practice, util_practice, unit, short_desc`) would place `AREA HARVESTED` / `PRODUCTION` series before `YIELD`, so the unmodified picker would return acres/production where it expects bu/acre, silently. Two mitigations, both required:

1. **Producer gate:** a new validation asserts that within each leaf, each `statistic` has at most one `canonical: true`, and that a `YIELD` canonical exists wherever any series exists (mirrors the spirit of the current Gate 3). Abort on violation.
2. **Consumer change (field-mcp, lockstep):** the picker must become `series.find(s => s.canonical && s.statistic === "YIELD")`. This is part of the same change set, tracked as an explicit cross-repo step. The leaf is not deployed until the consumer change is ready.

### 4.3 Keep the yield rollup, yield-only

`data/states/{fips}/crops/{crop}.json` is kept. It is the most-fetched data type in the traffic sample and is README-advertised; deleting it would create a multi-hour 404 window (codex P1/P2). It stays **yield-only** (production/area/price are not rolled up), which keeps it within its current size envelope (largest today: TX wheat 3.16 MB, far under 20 MB). Its series gain the v3 `statistic`/`cv` fields for consistency with leaves. No new rollups are created; new-statistic rollups were the only thing that would have breached 20 MB, and they are out of scope.

### 4.4 Prices: new family, strict filter

New family `data/prices/states/{fips}/{crop}.json` (state-level; a national file may be added if a consumer needs it, out of scope now). Second-pass module `scripts/prices.py` over the same download, following the SP-A `planting_windows.py` pattern (own schema + audit, hooked into `refresh.main()` before `prune_stale`).

Strict filter (codex P1): keep only `STATISTICCAT_DESC == "PRICE RECEIVED"` **and** `UNIT_DESC == "$ / BU"` at `AGG_LEVEL_DESC == "STATE"`. This excludes `PCT OF PARITY`, `PRICE RECEIVED AFTER REPORT`, `PRICE RECEIVED PRIOR TO CLOSING`, `PRICE RECEIVED, PARITY`, and the 10-year-average parity variants.

Reference periods are stored as two **separate** series per (state, crop): a `MARKETING YEAR` annual series and a `MONTHLY` series (keyed by month). Mixing them in one map would be ambiguous.

**Wheat class policy:** NASS publishes both a classless `WHEAT - PRICE RECEIVED` aggregate and per-class (`WINTER`, `SPRING`, `(EXCL DURUM)`, etc.) price rows at state level. Mark the classless aggregate `canonical: true`; retain class variants as additional series. This mirrors the yield canonical rule (`ALL CLASSES`).

### 4.5 Derived families (separate, SP-A pattern)

All five, each a separate sharded family with its own schema + audit, computed at emit time from in-memory state (production/area/price are colocated during the run):

a. **`state_price_imputed_revenue_per_acre`** (renamed from "revenue/acre", codex P2): `county yield x state marketing-year price`, per year, as both per-harvested-acre and per-planted-acre. The name makes the state-price imputation explicit so a report never implies a county-specific price. Join uses the commodity marketing-year convention (see 4.6).

b. **County rank + percentile** within-state and within-nation, per year, on canonical yield. Lives in `data/derived/state-{crop}.json` (the per-state file that recovers the comparison-scan use case the rollup served).

c. **Production-weighted state/national yield** = `sum(production) / sum(area harvested)`. The correct aggregate; replaces the naive county-mean that a consumer would otherwise compute wrong.

d. **Per-series derived stats**: trailing 5- and 10-year average, year-over-year %, linear trend slope. Suppressed years are skipped, not treated as zero.

e. **Multi-crop bundle** `data/bundles/{fips}/{code}.json`: corn+soy+wheat canonical raw + headline derived in one fetch. Flagged YAGNI (leaves are ~5 KB and warm-cache fast, so three fetches is already cheap); retained because the owner asked for all five. First candidate to cut if scope tightens.

### 4.6 Marketing-year join correctness (codex P2)

Yield `values` are keyed by `YEAR` (calendar/crop year). Corn and soybean marketing years span Sep-Aug (two calendar years); wheat's is Jun-May. `yield[Y] x price[Y]` is not self-evidently the same economic period. The derived module models an explicit per-commodity marketing-year mapping and documents which yield year joins to which marketing-year price. The output records both the yield year and the marketing-year label so the join is auditable.

### 4.7 Migration and pipeline safety

- **Per-family Gate 2 baselines (codex P1).** `validate()` compares kept-row count to a single `last_filtered_row_count`; the jump from ~1.32M to ~4.3M would abort the first run. Replace the single baseline with per-family baselines in `.refresh-state.json` (e.g. `last_filtered_row_count.leaf`, `.prices`, etc.), each with its own +-10% band, bootstrap-tolerant on first sight of a family. This avoids a manual one-time override that could silently disable the gate.
- **Per-family bootstrap sentinels (codex P1).** `bootstrap_needed` currently checks only `index.json` and the SP-A audit. A same-ETag deploy would otherwise ship code but emit no new artifacts until the next NASS file. Each new family (prices, derived, bundle) contributes its audit-file presence to `bootstrap_needed`, exactly as SP-A's `sp_a_bootstrap_needed()` does.
- **Atomic schema migration (codex P1).** `_assert_leaf_shape` hard-requires `schema_version == 2` and rejects unknown series keys; the assert, the schema file, the tests, and the emitters bump to v3 in one change so the pipeline never aborts mid-migration.
- **Create `data/_schema/leaf.json` (codex, verified).** This file is referenced by README as a published contract but was never committed (the leaf shape is enforced only in Python). Create it at v3 (JSON Schema 2020-12) and correct the README. Add `data/_schema/price.json`, `data/_schema/derived-county.json`, `data/_schema/derived-state.json`.

## 5. Target tree (diff from today)

```
data/
  index.json                                    schema_version 2 -> 3; advertises new families
  _schema/
    leaf.json                  CREATE (v3; never existed despite README)
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

The leaf/rollup change is internal to `refresh.py`: widen the filter, carry `statistic`/`cv` through `group_by_state` and the series dict, generalize `mark_canonical` to per-statistic, generalize `_assert_leaf_shape` to v3, and keep `emit_crop_rollups` filtering to yield only.

## 7. Phasing

1. **Foundation:** leaf v3 (yield + production + area + cv), yield-only v3 rollup, create `leaf.json`, generalize canonical to per-statistic + new guard gate, per-family Gate 2 baselines, atomic schema/assert/test bump, README fix. Plus the lockstep field-mcp picker change (`canonical && statistic === "YIELD"`). Everything depends on this.
2. **Prices:** `prices.py` + `price.json` schema + audit + bootstrap sentinel.
3. **Derived:** `derived.py` + schemas + audit + bootstrap sentinel (rank/percentile, prod-weighted yield, imputed revenue/acre with marketing-year join, trailing/YoY/trend).
4. **Bundle:** `bundles/` (YAGNI candidate; cut if scope tightens).

Each phase is independently shippable and testable. TDD per phase: tests for the new shape/filter/gate precede implementation.

## 8. Testing

- Filter: production/area/price rows kept or excluded exactly; parity and after-report price rows excluded; monthly vs marketing-year separation.
- Leaf v3: `statistic` present on every series; `cv` parallel to `values`; exactly one canonical per statistic; YIELD canonical present.
- Guard gate: a leaf with two canonical YIELD series, or a non-YIELD-only leaf, aborts.
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
