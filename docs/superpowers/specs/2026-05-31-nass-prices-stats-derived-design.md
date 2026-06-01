# NASS Prices, Production/Area, CV, and Precomputed Joins

**Date:** 2026-05-31
**Branch:** `worktree-feat+nass-prices-stats-derived`
**Status:** Design, ready for review.
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

The owner treats field-mcp as the sole real consumer and will update it in lockstep with the leaf schema change. This permits a breaking leaf change here, paired with a matching consumer change there.

## 4. Decisions

### 4.1 Leaf v3 (breaking, lockstep with field-mcp)

Replace the v2 county leaf with a v3 multi-statistic leaf. Each `series[]` entry gains:

- `statistic`: one of `YIELD`, `PRODUCTION`, `AREA HARVESTED`, `AREA PLANTED`, `AREA PLANTED, NET`.
- `cv`: a map `{year: cv_percent}` parallel to `values`, carrying `CV_%` where NASS publishes it (absent/blank where it doesn't).

`canonical` is redefined to mean **canonical for its statistic**. A leaf may therefore have multiple `canonical: true` series, at most one per statistic.

The producer filter at `refresh.py:185-192` is widened: keep county/SURVEY/ANNUAL/YEAR/TOTAL/NOT SPECIFIED rows for the three crops where `STATISTICCAT_DESC` is any of the five statistics above (not just YIELD).

Production and area go into the leaf rather than a separate family: with field-mcp the sole consumer and updated in lockstep, the single-leaf shape is the natural home for per-county series. The yield series within the leaf is byte-stable except for the two added fields.

#### 4.1.1 Canonical rule table, per (crop, statistic)

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

Two traps this table encodes, each of which a per-crop (statistic-blind) rule gets wrong:

1. **Corn `util_practice` is `GRAIN` for four statistics but `ALL UTILIZATION PRACTICES` for AREA PLANTED.** A rule that pinned `util=GRAIN` for all corn area would never mark corn AREA PLANTED canonical (it would land in the missing-canonical gate and degrade silently). The util value is per (crop, statistic), not per crop.
2. **`unit` alone does not disambiguate corn area statistics.** Corn AREA HARVESTED at `ALL CLASSES/ALL PRODUCTION PRACTICES/ACRES` collides with SILAGE and FORAGE rows at the same class/prodn/unit; only `util_practice` separates them. So `util_practice` is a required match field.

Implementation: `CANONICAL_RULES` becomes a `dict[(crop_slug, statistic)] -> {class, prodn_practice, util_practice, unit}`. `mark_canonical` scopes candidate series to the target statistic **first**, then matches the 4-tuple (necessary because corn AREA HARVESTED and AREA PLANTED, NET share the identical 4-tuple `ALL CLASSES/ALL PRODUCTION PRACTICES/GRAIN/ACRES` and are distinguished only by statistic). The match was verified unique within each (crop, statistic) on the live file, so exactly one series per (crop, statistic) is marked. A module-load assertion (mirroring `refresh.py:54-57`) requires every (crop in COMMODITY_ALLOWLIST × statistic in the five) to have a rule, so a future commodity or statistic cannot silently ship without a canonical rule.

### 4.2 Consumer-corruption guard

The live field-mcp picker is `series.find(s => s.canonical === true)` with no statistic filter. With multiple canonical series, the producer's `sort_series` (orders by `class, prodn_practice, util_practice, unit, short_desc`) places `AREA HARVESTED`/`PRODUCTION` before `YIELD`, so the unmodified picker would return acres/production where it expects bu/acre, silently. Two mitigations, both required:

1. **Producer gate, two checks at mark time.** `mark_canonical` currently marks the first rule-matching series and `break`s (`refresh.py:344-348`), so it can never *produce* two canonical of one statistic; a guard that counts `canonical: true` after marking is vacuous and never fires on an ambiguous rule. The guard therefore operates on **rule-matching candidates** while marking: for each `(crop, statistic)`, collect every series matching that statistic's 4-tuple rule (section 4.1.1), then (a) **hard abort if the candidate count is > 1** (an ambiguous rule, e.g. NASS drift introducing a second match), and mark the single match canonical; and (b) **ratio gate, preserving the existing Gate 3** (`refresh.py:559-577`): the fraction of (county, crop) pairs with **zero** YIELD candidates stays under `CANONICAL_MISSING_TOLERANCE` (5%), now scoped to the YIELD statistic. (b) is a tolerance, not a per-leaf hard requirement: a county that publishes acres but not yield is allowed (counts toward the ratio), matching current soft-fail semantics. Non-YIELD statistics are not ratio-gated; the >1 hard abort applies to every statistic.
2. **Consumer change (field-mcp), backward-compatible picker.** The picker becomes `series.find(s => s.canonical && (s.statistic === "YIELD" || s.statistic === undefined))`. It resolves the yield series on **both** a v2 leaf (no `statistic`; matches `=== undefined`) and a v3 leaf (matches `=== "YIELD"`), so the new consumer is safe against whatever is currently on the CDN.

   **Deploy ordering is strictly consumer-first.** The unsafe combination is the *old* picker reading *v3* data: after `sort_series`, the first canonical series in a v3 leaf is `AREA HARVESTED`/`PRODUCTION`, so the old picker returns acres as bu/acre. Simultaneous release is therefore not safe: any window where v3 data is on the CDN while an old field-mcp instance is live corrupts. The rule: **(1) deploy the backward-compatible picker to field-mcp, (2) verify it live in production, (3) only then merge the producer v3 change.** The new picker reads v2 correctly, so step 1 is safe against today's CDN at any time; it also reads v3, so there is no rush after step 3. This sequencing is a release-checklist item in the field-mcp PR; this repo's producer guard (mitigation 1) makes a v3 leaf well-formed but cannot protect an old consumer, so the ordering is mandatory.

### 4.3 Keep the yield rollup, yield-only

`data/states/{fips}/crops/{crop}.json` is kept. It is the most-fetched data type in the traffic sample and is README-advertised; deleting it would create a multi-hour 404 window. It stays **yield-only** (production/area/price are not rolled up), keeping it within its current size envelope (largest today: TX wheat 3.16 MB, far under 20 MB). The canonical yield series gains the v3 `statistic`/`cv` fields for consistency with leaves.

**Shared-list hazard.** `emit_crop_rollups` currently copies `com["series"]` by reference into each county block (`refresh.py:487-492`). Once the in-memory commodity carries production/area series too, that reference would leak non-yield series into the rollup. The rollup emitter must build a **filtered copy** per county, `[s for s in com["series"] if s["statistic"] == "YIELD"]`, never the shared list, with a dedicated test (a commodity with mixed-statistic series must produce a yield-only rollup). No new rollups are created; new-statistic rollups were the only thing that would have breached 20 MB and are out of scope.

### 4.4 Prices: new family, strict filter

New family `data/prices/states/{fips}/{crop}.json` (state-level; a national file may be added if a consumer needs it, out of scope now). Second-pass module `scripts/prices.py` over the same download, following the `planting_windows.py` pattern (own schema + audit, hooked into `refresh.main()` before `prune_stale`).

Strict filter: keep only `STATISTICCAT_DESC == "PRICE RECEIVED"` **and** `UNIT_DESC == "$ / BU"` at `AGG_LEVEL_DESC == "STATE"`. This excludes `PCT OF PARITY`, `PRICE RECEIVED AFTER REPORT`, `PRICE RECEIVED PRIOR TO CLOSING`, `PRICE RECEIVED, PARITY`, and the 10-year-average parity variants.

Reference periods are stored as two **separate** series per (state, crop): a `MARKETING YEAR` annual series and a `MONTHLY` series (keyed by month). Mixing them in one map would be ambiguous.

**Wheat class policy:** NASS publishes both a classless `WHEAT - PRICE RECEIVED` aggregate and per-class (`WINTER`, `SPRING`, `(EXCL DURUM)`, etc.) price rows at state level. Mark the classless aggregate `canonical: true`; retain class variants as additional series. This mirrors the yield canonical rule (`ALL CLASSES`).

### 4.5 Derived families (separate, planting-windows pattern)

All five, each a separate sharded family with its own schema + audit, computed at emit time from in-memory state (production/area/price are colocated during the run):

a. **`state_price_imputed_revenue_per_acre`**: `county yield × state marketing-year price`, per year, as both per-harvested-acre and per-planted-acre. The name makes the state-price imputation explicit so a report never implies a county-specific price. Join uses the commodity marketing-year convention (section 4.6).

b. **County rank + percentile** within-state and within-nation, per year, on canonical yield. Lives in `data/states/{fips}/derived/state-{crop}.json` (the per-state file that recovers the comparison-scan use case the rollup served).

c. **Production-weighted state/national yield** = `sum(production) / sum(area harvested)`. The correct aggregate; replaces the naive county-mean a consumer would otherwise compute wrong.

d. **Per-series derived stats**: trailing 5- and 10-year average, year-over-year %, linear trend slope. Suppressed years are skipped, not treated as zero.

e. **Multi-crop bundle** `data/bundles/{fips}/{code}.json`: corn+soy+wheat canonical raw + headline derived in one fetch. YAGNI candidate (leaves are ~5 KB and warm-cache fast, so three fetches is already cheap); retained because the owner asked for all five. First to cut if scope tightens. **CUT 2026-06-01 (see section 9):** not built. The bundle is pure denormalized duplication of data already published in the leaf and derived families, has no consumer (field-mcp reads only county leaves and consumes neither prices nor derived yet), and would add weekly git churn and tree growth to save a hypothetical consumer two small warm-cache fetches. Revivable from this spec if a real multi-crop fetch consumer appears.

### 4.6 Marketing-year join, explicit mapping

Yield `values` are keyed by `YEAR` (the harvest/crop year). NASS marketing years are labeled by their **starting** calendar year. The join is: **a crop's `yield[Y]` joins to the marketing-year price labeled `Y`**, because that crop is harvested in fall of year `Y` and marketed across the marketing year that begins in `Y`.

| Crop | Marketing year span | Yield year `Y` joins to price marketing-year label |
|---|---|---|
| Corn | Sep `Y` to Aug `Y+1` | `Y` |
| Soybeans | Sep `Y` to Aug `Y+1` | `Y` |
| Wheat | Jun `Y` to May `Y+1` | `Y` |

So `corn yield[2024]` (harvested fall 2024) joins to the `MARKETING YEAR` price row labeled `2024` (the Sep-2024-through-Aug-2025 average). The derived output records `{yield_year, marketing_year, price, revenue_per_acre}` so the join is auditable, and emits nothing for a year where either side is absent or suppressed (no silent zero). The mapping is identity on the label because NASS labels the marketing year by its start year, which equals the harvest year for all three crops here; it is stated explicitly so a future spring-planted or southern-hemisphere commodity does not blindly reuse it.

### 4.7 Migration and pipeline safety

- **Per-family Gate 2 baselines, validated at each emit site.** Today `validate(total_rows, kept_rows, last_filtered_row_count)` is called once in `main()` after `stream_filter()` and before grouping (`refresh.py:709-713`), against a single global `last_filtered_row_count`. That single pre-group call cannot validate per-family counts, and the leaf jump from ~1.32M to ~4.3M would abort the first run. `validate()` becomes per-family, called at each family's own point in the pipeline: the leaf family validates after the widened filter/group; `prices.py` and `derived.py` each validate their own count inside `run_*`. `.refresh-state.json` carries a baseline map, e.g. `last_filtered_row_count: {leaf: N, prices: N, derived: N}`, each with its own ±10% band and bootstrap-tolerant on first sight of a family (returns without aborting when its baseline is absent). The global integer is migrated to this map shape in the same change.

  The per-family count reaches `save_state` the same way SP-A's does: `save_state` writes the dict `main()` builds (`refresh.py:625-628,766-777`), and `run_planting_windows` returns `PlantingWindowRunResult(paths, shard_count)` whose `shard_count` `main()` reads into state (`planting_windows.py:52-55`, `refresh.py:755-776`). So `prices.run_prices` and `derived.run_derived` each return a result object carrying both their emitted paths (for the `expected` set / prune) and their kept/emitted count; `main()` assembles the baseline map and passes it to `save_state`. The leaf count is already in `main()`'s scope (`len(kept_rows)`). Persistence stays centralized in `main()`/`save_state`; no module writes `.refresh-state.json` directly.

- **Bootstrap sentinels, with a working same-publication re-emit path.** Two early returns block re-emit: `is_caught_up(...)` at the top (`refresh.py:676-679`) and the ETag-match branch (`refresh.py:698-701`), both before `bootstrap_needed` is computed (`refresh.py:696`). Hoisting `bootstrap_needed` above them is not enough: after bypassing `is_caught_up`, `discover()` computes `earliest = last_known + 1` and returns `None` when that is past `today` (`refresh.py:118-121,137`), so `main()` aborts at the no-discovery branch (`refresh.py:680-686`) before emit. The fix has three parts: **(1)** compute a combined `bootstrap_needed` (index + every family audit sentinel) before both early returns; **(2)** guard both early returns with `and not bootstrap_needed` (the ETag branch already has it; the `is_caught_up` branch does not); **(3)** give `discover()` an inclusive lower bound when `bootstrap_needed` is true, so `earliest = last_known`. With (3), discover re-finds the already-published file, the run falls into the existing "bootstrapping from cached download" branch (`refresh.py:702-703`), re-downloads `last_url` via the normal path, and re-emits. Bootstrap reuses `discover` + `download_with_retry` exactly as a normal run does; each new family contributes a `*_bootstrap_needed()` check mirroring `sp_a_bootstrap_needed()` (`planting_windows.py:384-386`).

  **`bootstrap_needed` references only families whose emitters exist in the merged code.** If Foundation shipped a `bootstrap_needed` that ORed in `prices`/`derived` sentinels before those modules exist, the audit files could never be written, `bootstrap_needed` would be permanently true, and every run would re-download ~1 GB and re-emit forever. Each `*_bootstrap_needed()` term is added in the **same phase that adds its emitter**, as `sp_a_bootstrap_needed()` was introduced with `planting_windows.py`. Foundation's `bootstrap_needed` is `index + leaf + planting-windows` only; phase 2 adds prices; phase 3 adds derived.

  **Sentinels are always written, even for a zero-shard family.** The bootstrap loop clears only when the sentinel exists, so a sentinel written conditionally on having emitted shards would never appear for a family that legitimately emits zero shards, and `bootstrap_needed` would stay true forever. `run_planting_windows` is the template: it calls `emit_all` unconditionally, and `emit_all` writes the audit and coverage regardless of shard count (`planting_windows.py:377-427`). So `prices.py` and `derived.py` must write their audit sentinel unconditionally inside `run_*`, independent of shard count (a zero-shard family writes an audit recording zero, a valid state). A family that aborts hard (its own Gate 2) is the only case that leaves the sentinel unwritten, and that path exits non-zero so the run fails loudly rather than looping.

  Scope bound: this self-heals a redeploy as long as `last_url` is still served by NASS (recent files persist on the datasets server). If NASS has rotated the file out, the families materialize on the next successful publication instead, at most a few days given the weekly cron and near-daily NASS crop publications. This is the same eventual-consistency posture SP-A already accepts.

- **Atomic schema migration, all artifacts bump together.** `_assert_leaf_shape` hard-requires `schema_version == 2` and rejects unknown series keys (`refresh.py:594,602,612`); the assert, schema file, tests, and emitters bump to v3 in one change so the pipeline never aborts mid-migration. **Every artifact that today carries `schema_version: 2` moves to 3 in the same commit:** `index.json` (`emit_index`), the point leaf (`emit_point_leaves`), the state×crop rollup (`emit_crop_rollups`, `refresh.py:476-504`), state `meta.json` (`emit_state_meta`), and the maintainer audit (`emit_audit`). The published tree is never mixed-version. New families (`prices`, `derived`, `bundle`) are born at v3. The `planting-window` artifacts are on a separate version line (their own `method`/`definition` contract, no `schema_version` field) and are not renumbered. A test asserts no published artifact under `data/` carries `schema_version: 2` after the migration.

- **Schemas are static committed artifacts, not bootstrap-sentineled.** The existing `data/_schema/planting-window.json` is hand-authored and committed; the producer only protects its path from prune, it does not regenerate it. The new `leaf.json`, `price.json`, `derived-county.json`, `derived-state.json` follow the same model: authored and committed in this PR, protected from prune, never written by the refresh. A missing schema file is a broken checkout, not a self-healable state, which is why the bootstrap sentinels key on **audit** files (which the refresh generates) and omit schema files.

- **`CV_%` added to `REQUIRED_COLS`.** `cv` is sourced from the bulk file's `CV_%` column, which the current header carries but `REQUIRED_COLS` omits (`refresh.py:59-67`). Adding it means a NASS rename/drop trips Gate 1 (missing-required-column abort) loudly, instead of the leaf silently emitting empty `cv` maps.

- **Create `data/_schema/leaf.json`.** README references this file as a published contract; it existed historically (`f942378`, `f074e11`), was removed, and is absent now, so the leaf shape is currently enforced only in Python. Create it at v3 (JSON Schema 2020-12) and fix the README link. Add `data/_schema/price.json`, `data/_schema/derived-county.json`, `data/_schema/derived-state.json`.

## 5. Target tree (diff from today)

```
data/
  index.json                                    schema_version 2 -> 3 (bumped with all artifacts, section 4.7); advertises new families
  _schema/
    leaf.json                  CREATE (v3; existed historically, was removed, absent now; README still links it)
    price.json                 CREATE
    derived-county.json        CREATE
    derived-state.json         CREATE
    planting-window.json                        (unchanged)
  _audit/
    latest.json                                 schema_version 2 -> 3 (bumped with all artifacts, section 4.7); content otherwise unchanged
    prices.json                CREATE
    derived.json               CREATE
    planting-windows.json, window-coverage.json (unchanged; SP-A version line, no schema_version field)
  states/{fips}/
    meta.json                                   schema_version 2 -> 3 (bumped with all artifacts, section 4.7); county/crop list otherwise unchanged
    counties/{code}/{crop}.json                 LEAF v3: yield+production+area series, each value carries cv
    crops/{crop}.json                           ROLLUP v3, yield-only (kept; not dropped)
    derived/state-{crop}.json  CREATE  prod-weighted state+national yield; per-county rank/percentile
  prices/states/{fips}/{crop}.json  CREATE  state PRICE RECEIVED $/bu (marketing-year + monthly series)
  derived/{fips}/counties/{code}/{crop}.json  CREATE  rank, percentile, imputed revenue/acre, trailing/YoY/trend
  bundles/{fips}/{code}.json  CREATE (YAGNI candidate)  corn+soy+wheat canonical raw + headline derived
  planting-windows/{fips}/{slug}.json           (unchanged)
```

## 6. Architecture

New stdlib-only modules mirror `planting_windows.py`: `scripts/prices.py` and `scripts/derived.py`, each with a `run_*` entrypoint that returns a small frozen result object (an emitted-paths set plus its kept/emitted row count, mirroring `PlantingWindowRunResult`'s `paths` + `shard_count`), called from `refresh.main()` after the leaf/rollup emit and before `prune_stale` (so their paths join the `expected` set and are not pruned, and their count flows into the per-family Gate 2 baseline map, section 4.7). `derived.py` consumes the in-memory grouped state plus the price result, so revenue/rank/weighted-yield are computed without a second parse.

The leaf/rollup change is internal to `refresh.py`: add `CV_%` to `REQUIRED_COLS`; widen the filter to the five statistics; carry `statistic`/`cv` through `group_by_state` and the series dict; generalize `mark_canonical` to the per-`(crop, statistic)` rule table (section 4.1.1); generalize `_assert_leaf_shape` to v3; make `emit_crop_rollups` build a yield-only filtered copy (section 4.3). The single global `validate()` call is replaced by per-family validation at each emit site, and the top-of-`main()` early returns are reordered behind a combined `bootstrap_needed` (section 4.7).

## 7. Phasing

1. **Foundation:** leaf v3 (yield + production + area + cv), yield-only v3 rollup, create `leaf.json`, the per-`(crop, statistic)` rule table + candidate-counting guard, per-family Gate 2 baselines, the bootstrap/`discover` inclusive-bound change, `CV_%` in `REQUIRED_COLS`, atomic schema/assert/test bump, README fix. Plus the field-mcp backward-compatible picker (section 4.2), deployed and verified live before any v3 data is published. Everything depends on this. Foundation's `bootstrap_needed` references `index + leaf + planting-windows` only.
2. **Prices:** `prices.py` + `price.json` schema + audit. The prices term is added to `bootstrap_needed` in this phase, when `prices.py` exists to emit `_audit/prices.json`. Adds `prices` to the Gate 2 baseline map.
3. **Derived:** `derived.py` + schemas + audit (rank/percentile, prod-weighted yield, imputed revenue/acre with marketing-year join, trailing/YoY/trend). The derived term is added to `bootstrap_needed` in this phase. Adds `derived` to the baseline map.
4. **Bundle:** `bundles/` (YAGNI candidate; cut if scope tightens). If kept, its sentinel/baseline term is added in this phase. **CUT 2026-06-01, not built (section 9).**

Each phase is independently shippable and testable: at the end of each, the merged `bootstrap_needed` references only sentinels for emitters that exist as of that phase, so a same-publication rerun early-returns cleanly instead of looping. TDD per phase: tests for the new shape/filter/gate precede implementation.

**Shipped status (2026-06-01):** Foundation merged `3a7a60f` (PR #3), Prices merged `84a69c7` (PR #4), Derived merged `fc0012d` (PR #5). Bundle cut as YAGNI. The expansion is complete: the producer now publishes county yield+production+area+cv leaves, state prices, and derived joins, up from yield-only.

## 8. Testing

- Filter: production/area/price rows kept or excluded exactly; parity and after-report price rows excluded; monthly vs marketing-year separation.
- Leaf v3: `statistic` present on every series; `cv` parallel to `values`; exactly one canonical per statistic.
- Canonical table (4.1.1): corn AREA PLANTED resolves to `ALL UTILIZATION PRACTICES` (not GRAIN); corn AREA HARVESTED resolves to GRAIN, not SILAGE/FORAGE, at the shared `ACRES` unit; wheat YIELD resolves to the `ALL CLASSES` aggregate, not WINTER; each (crop, statistic) marks exactly one series on a fixture built from real tuples.
- Guard gate (candidate-counting, 4.2): a fixture where a `(crop, statistic)` rule matches two series aborts; the YIELD missing-canonical ratio gate still fires past 5%; a county with acres-but-no-yield is tolerated.
- Rollup stays yield-only and within size envelope (a mixed-statistic commodity produces a yield-only rollup).
- Gate 2 per-family: first sight of a family bootstraps; subsequent ±10% drift aborts.
- Bootstrap same-publication re-emit (4.7): with `today == last_successful_date` and a family audit deleted, `main()` must not early-return; `discover` with the inclusive bound re-finds the same-dated file and the family is re-emitted. A second run with all audits present early-returns normally.
- Zero-shard sentinel: a family run emitting zero shards still writes its audit; a follow-up run early-returns (no infinite re-download loop).
- Version atomicity (4.7): after a v3 migration run on a fixture, no emitted artifact under `data/` carries `schema_version: 2`.
- Marketing-year join: corn yield[2024] maps to the documented marketing-year price; suppressed years skipped.
- Derived math: prod-weighted yield equals sum(prod)/sum(area) on a fixture; rank/percentile correct on a small state.

## 9. Out of scope

National price files, census source, other commodities, sub-state/zip/watershed aggregations, futures/basis, and any non-NASS source. New-statistic rollups (would breach 20 MB). The **multi-crop bundle** (`data/bundles/`, section 4.5e), cut 2026-06-01: it duplicates data the leaf and derived families already publish, has no consumer, and the per-leaf fetch is already small and warm-cache fast, so the bundle would add maintenance and tree growth for no benefit; revive it only if a consumer materializes that genuinely needs corn+soy+wheat in one request. These are deliberate future scope, not accidental omissions.

A separate, larger dependency lives in field-mcp: a price/production consumer must be written there for any of this new data to reach a report. Adding the data here is necessary but not sufficient for the FIE market-recap feature. Tracked as a field-mcp ticket.

## 10. Risks

- **"Sole consumer" is a well-founded bet, not proof.** The repo is presented publicly. If an unknown consumer depends on the v2 leaf shape, the breaking change breaks them. Bounded downside; owner's risk to accept.
- **Tree growth** ~93 MB -> ~160-180 MB (estimate, ±30%). All files stay far under 20 MB. Weekly churn stays ~0 via `write_if_changed`; the annual all-leaf rewrite (new year value) grows proportionally. A slow-burn git-history watch-item, not a wall.
- **field-mcp deploy ordering is consumer-first** (section 4.2): the backward-compatible picker is deployed and verified live before any v3 data is published. Simultaneous release is unsafe (an old picker reading v3 bytes returns acres as bu/acre). This repo cannot enforce the cross-repo ordering; it is a release-checklist dependency.
