# USDA NASS County Crop Yields JSON API

USDA NASS county-level **corn, soybean, and wheat** yield data — a free static JSON API served from the jsDelivr CDN, refreshed weekly from the NASS bulk file. Per-county point lookups in 2-22 KB. No API key, no rate limits, no auth. Public-domain agricultural data for data scientists, ML pipelines, agronomy notebooks, and ag-tech tooling. Sharded by `(state, county, crop)` so a single lookup downloads a small leaf instead of a multi-megabyte state shard.

## Quick lookup

A single `(state, county, crop, year)` lookup is two cached fetches: one `index.json` for discovery + freshness, then one tiny leaf for the actual data.

```
GET https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/index.json
GET https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/states/{fips}/counties/{county_code}/{crop_slug}.json
```

Each leaf carries one series per statistic (yield, production, area harvested, area planted). The producer pre-marks the canonical series for each statistic with `"canonical": true`, so to read yield, pick the series that is both `canonical` and `statistic == "YIELD"`, then read `values[year]` off it. (A leaf has at most one canonical series per statistic; the yield one is the `class="ALL CLASSES"` + `prodn_practice="ALL PRODUCTION PRACTICES"` + `unit="BU / ACRE"` aggregate.)

Example, "Story County, Iowa corn-grain yield 2024":

```
GET https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/states/19/counties/169/corn.json
→ series[s for s if s.canonical and s.statistic == "YIELD"]
→ values["2024"]   →   215.5
```

Working lookup, three flavors:

```python
# Python
import json, urllib.request

url = "https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/states/19/counties/169/corn.json"
leaf = json.load(urllib.request.urlopen(url))
canonical = next(s for s in leaf["series"] if s.get("canonical") and s["statistic"] == "YIELD")
print(canonical["values"]["2024"])   # 215.5
```

```javascript
// JavaScript (Node 18+ or any modern browser)
const url = "https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/states/19/counties/169/corn.json";
const leaf = await fetch(url).then(r => r.json());
const canonical = leaf.series.find(s => s.canonical && s.statistic === "YIELD");
console.log(canonical.values["2024"]);  // 215.5
```

```bash
# curl + jq
curl -s "https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/states/19/counties/169/corn.json" \
  | jq '.series[] | select(.canonical and .statistic == "YIELD") | .values["2024"]'
# 215.5
```

State and county codes are USDA / Census ANSI codes. Reference: https://www.census.gov/library/reference/code-lists/ansi.html.

For a state-wide scan of one crop (e.g., all counties for corn in Iowa), use the rollup so the whole answer comes in one fetch instead of ~99 cold leaf requests:

```
GET https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/states/19/crops/corn.json
→ counties[county_code].series[s for s if s.canonical and s.statistic == "YIELD"].values[year]
```

`refreshed_at`, source ETag, and `source.publication_date` live only in `data/index.json`. Leaves and rollups carry no timestamps so unchanged data does not cause weekly file rewrites.

### CDN consistency window

A lookup spans `index.json` + per-county leaves + per-crop rollups, all served from `@main`. jsDelivr edges populate independently, so for up to ~12 h after a refresh an edge can return a fresh `index.json` alongside stale or missing leaves. Consumers needing point-in-time consistency should pin a commit SHA in the URL (`@<sha>` instead of `@main`); jsDelivr serves SHA-pinned URLs from immutable git history.

## File size and latency expectations

Point leaves are **~2 KB to ~22 KB** (wheat is largest because NASS publishes WINTER + SPRING + ALL CLASSES variants across multiple production practices). State-wide rollups for one crop run **~50 KB to ~550 KB**. `index.json` is ~30 KB. All well under jsDelivr's 20 MB per-file cap.

Cold-fetch latency from a non-edge region: **~500 ms to ~2 s**. Warm-cache latency: **<100 ms**. The intended deployment pattern is a hot cache (e.g., Cloudflare 60-min TTL) in front of jsDelivr; this cache is the durable tier behind that.

## Schema

The published tree:

```
data/
  index.json                                        ~30 KB     discovery + refreshed_at + source
  _audit/latest.json                                ~1 KB      header_observed (maintainer audit)
  states/{fips}/
    meta.json                                       ~3-8 KB    county code -> name + crop list
    counties/{county_code}/{crop_slug}.json         ~2-22 KB   POINT LEAF
    crops/{crop_slug}.json                          ~50-550 KB STATE x CROP ROLLUP
```

`data/index.json`:

```json
{
  "schema_version": 3,
  "product_name": "NASS county crop yields",
  "refreshed_at": "2026-04-30T07:13:37Z",
  "source": {
    "url": "https://www.nass.usda.gov/datasets/qs.crops_20260430.txt.gz",
    "last_modified": "Thu, 30 Apr 2026 07:13:37 GMT",
    "etag": "...",
    "publication_date": "2026-04-30",
    "freshness_lag_days": 0
  },
  "states": {
    "19": { "alpha": "IA", "name": "IOWA", "crops": ["corn", "soybeans", "wheat"], "county_count": 99 }
  }
}
```

`data/states/{fips}/counties/{county_code}/{crop_slug}.json` (point leaf, no timestamps):

```json
{
  "schema_version": 3,
  "state":     { "fips": "19", "alpha": "IA", "name": "IOWA" },
  "county":    { "code": "169", "name": "STORY" },
  "commodity": { "slug": "corn", "desc": "CORN" },
  "series": [
    {
      "statistic": "YIELD",
      "class": "ALL CLASSES",
      "prodn_practice": "ALL PRODUCTION PRACTICES",
      "util_practice": "GRAIN",
      "unit": "BU / ACRE",
      "short_desc": "CORN, GRAIN - YIELD, MEASURED IN BU / ACRE",
      "canonical": true,
      "values":     { "2024": 215.5, "2023": 211.4 },
      "cv":         { "2024": 1.8 },
      "suppressed": { "1980": "D" },
      "raw":        {}
    }
  ]
}
```

Each `series` entry carries a `statistic` (`YIELD`, `PRODUCTION`, `AREA HARVESTED`, `AREA PLANTED`, or `AREA PLANTED, NET`) and a `cv` map (NASS coefficient of variation, the percent sampling reliability of each value, parallel to `values` and present only where NASS published it). Filter on `canonical && statistic == "YIELD"` to read yields, as in the examples above. The per-(state, crop) rollup (`data/states/{fips}/crops/{crop_slug}.json`) carries YIELD series only; production and area are available on the point leaves.

- **Year keys are strings**, not numbers (JSON object key semantics).
- `values` carries numeric yields. `suppressed` carries NASS suppression codes (`D`, `NA`, `S`, `X`, `Z` per the NASS Quick Stats glossary). `raw` carries any cell value that didn't parse as numeric or suppression code, with the original string preserved for forensic audit.
- Multiple `series` entries per commodity capture both different statistics (yield vs production vs area) and variants within a statistic (corn-grain BU/ACRE vs corn-silage TONS/ACRE). The producer pre-marks one canonical entry per statistic with `"canonical": true`; consumers should filter on `canonical && statistic == "<STAT>"` rather than replicating the rule.
- **Supported commodity slugs:** `corn`, `soybeans`, `wheat`. Note `soybeans` is plural.
- `header_observed` is published once at `data/_audit/latest.json` (not per state). NASS adding a new column logs here without aborting the refresh; renaming a depended-on column does abort.
- `county.code` is always populated and zfilled to 3 digits. The legacy NASS `ansi` field is dropped at the leaf level because it equals `code` for every populated row and is sometimes blank.
- **Machine-readable contract**: [`data/_schema/leaf.json`](data/_schema/leaf.json) is a JSON Schema 2020-12 document describing the point-leaf shape. Producers and consumers can both validate against it.

### State prices

NASS publishes grain PRICE RECEIVED only at state and national level, never county. The state series live in a separate family:

```
GET https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/prices/states/{fips}/{crop_slug}.json
```

```json
{
  "schema_version": 3,
  "state": { "fips": "19", "alpha": "IA", "name": "IOWA" },
  "commodity": { "slug": "corn", "desc": "CORN" },
  "series": [
    { "class": "ALL CLASSES", "period": "MARKETING YEAR", "unit": "$ / BU",
      "canonical": true, "values": { "2024": 4.80 }, "suppressed": {} },
    { "class": "ALL CLASSES", "period": "MONTHLY", "unit": "$ / BU",
      "values": { "2024-08": 5.20 }, "suppressed": {} }
  ]
}
```

- Two `period` shapes: `MARKETING YEAR` (keyed by year, the recap-grade price) and `MONTHLY` (keyed by `YYYY-MM`). Calendar-year annual prices are not published.
- Pick the headline price by filtering `canonical && period == "MARKETING YEAR"` (the `ALL CLASSES` aggregate). Wheat also carries non-canonical class series; corn and soybeans are `ALL CLASSES` only.
- Prices are **state-level**: a county report joining a state price to a county yield should label it state-imputed, not county revenue.
- Schema: [`data/_schema/price.json`](data/_schema/price.json).

### Derived families

Precomputed joins and statistics, so a consumer fetches a result instead of re-deriving it. Two families.

Per-(county, crop), revenue + yield trend + rank:

```
GET https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/derived/{fips}/counties/{county_code}/{crop_slug}.json
```

```json
{
  "schema_version": 3,
  "state": { "fips": "19", "alpha": "IA", "name": "IOWA" },
  "county": { "code": "169", "name": "STORY" },
  "commodity": { "slug": "corn", "desc": "CORN" },
  "revenue": {
    "2024": { "marketing_year": "2024", "yield": 215.5, "price": 4.80,
              "revenue_per_harvested_acre": 1034.4, "revenue_per_planted_acre": 1010.2 }
  },
  "yield_trend": { "slope_bu_per_year": 1.85, "yoy_pct": { "2024": 3.2 },
                   "trailing_5yr_avg": { "2024": 201.4 }, "trailing_10yr_avg": { "2024": 195.0 } },
  "rank": { "2024": { "rank_in_state": 12, "count_in_state": 99, "percentile_in_state": 0.8878,
                      "rank_in_nation": 145, "count_in_nation": 2100, "percentile_in_nation": 0.9314 } }
}
```

Per-(state, crop), production-weighted yield + a county comparison scan:

```
GET https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/states/{fips}/derived/state-{crop_slug}.json
```

- **Revenue is state-imputed.** `revenue_per_harvested_acre = county yield * state marketing-year price`; `revenue_per_planted_acre = county production * state price / county area planted`. The price is the state figure (NASS publishes no county price), so a report must label revenue as state-imputed, not a county-specific price. The marketing-year join is recorded per record (`marketing_year`): a crop's `yield[Y]` joins the marketing-year price labelled `Y` (corn, soybeans, wheat alike). A year is emitted only where both yield and the joined price exist; suppressed years are skipped, never zero-filled.
- **Production-weighted yield** = `sum(production) / sum(area harvested)` across counties (the correct aggregate, not a county-mean), as both a `state` and a `national` series. The `national` block is identical across every state file for a crop.
- **Rank/percentile** is competition rank (1 = highest, ties share a rank) within-state and within-nation, on canonical yield, per year. `percentile = (n - rank) / (n - 1)`, and `1.0` for a lone county.
- **Yield trend**: `slope_bu_per_year` (OLS), `yoy_pct`, and trailing 5/10-year averages (emitted only with enough present years). Suppressed years are excluded from every statistic.
- Schemas: [`data/_schema/derived-county.json`](data/_schema/derived-county.json), [`data/_schema/derived-state.json`](data/_schema/derived-state.json).

## Scope

- **SURVEY** rows only (annual; CENSUS deferred until a consumer asks for it).
- **CORN, SOYBEANS, WHEAT** only. Other commodities ship when consumer demos require them.
- **County-level YIELD, PRODUCTION, and AREA** (harvested, planted, planted-net), each as its own series on the point leaf. State-level prices (`data/prices/`) and precomputed derived joins (`data/derived/` + `data/states/{fips}/derived/`) are separate families. Other statistics remain out of scope.

## Refresh cadence

- **Weekly, Monday at 09:17 UTC.** Year-round safe vs NASS's ~03:13 ET publication time (covers EDT and EST).
- Healthchecks.io configured with **9-day grace window** and **alerts on two consecutive missed pings** (single-Monday GitHub cron drops auto-recover the next week).
- Recovery from 60-day workflow auto-disable is one click on the Actions page (`Run workflow`).
- **Worst-case staleness:** ~7 to 8 days (one weekly refresh interval plus up to ~12 h CDN propagation). Steady-state is much fresher; this is the ceiling, not the typical case.

## Caveats

- **State coverage is partial.** A state appears under `data/states/` only if NASS published county-level SURVEY yields for corn, soybeans, or wheat in the source file. As of this writing the cache holds 42 of 50 states. Absent FIPS: `02` (AK), `09` (CT), `11` (DC), `15` (HI), `23` (ME), `25` (MA), `33` (NH), `44` (RI), `50` (VT). Consumers should resolve fips and county codes from `data/index.json` rather than hard-coding. If CT later returns to NASS county output, expect planning-region codes per Federal Register 2022-12063 rather than the legacy county codes.
- **Suppressed values** (`(D)`, `(NA)`, etc.) are NOT silently null. They land in the `suppressed` map keyed by year. Consumers choose to surface, ignore, or fall back.
- **Soft-fail semantics**: a missing `(state, county, crop)` is an absence in the file, not an error. The cache makes no completeness guarantee; it reflects what NASS published in the most recent bulk file.
- **Eventual consistency**: after a refresh, jsDelivr's `s-maxage=43200` means up to ~12 h before edges return the new content. The multi-file fetch shape can briefly serve a fresh `index.json` alongside stale leaves on the same edge. Consumers needing bit-stable reads can pin a commit SHA in the URL (`@<sha>` instead of `@main`).

## Architecture

- **Source**: `qs.crops_YYYYMMDD.txt.gz` from `https://www.nass.usda.gov/datasets/`, refreshed business-daily by NASS.
- **Refresh**: GitHub Actions cron runs `scripts/refresh.py` on a public repo (free). Tests run before refresh on every CI invocation.
- **Storage**: this Git repo. Sharded JSON committed weekly via touched-only writes (unchanged leaves are not rewritten).
- **CDN**: jsDelivr-fronted GitHub raw, free, no auth.
- **Monitoring**: healthchecks.io free tier deadman switch (refresh ping). A separate [CDN canary workflow](.github/workflows/canary.yml) runs Mondays at 22:00 UTC, fetches `data/index.json` and a known point leaf from jsDelivr, asserts freshness within the 9-day grace window, and pings a second healthchecks URL on success.

Three validation gates and three inline guards before any publish:
- **Gate 1**: required columns present in NASS header (tolerant reader; extra columns OK).
- **Gate 2**: filtered row count within ±10% of last successful run (skipped on bootstrap).
- **Gate 3**: missing-canonical ratio within 5% (`CANONICAL_MISSING_TOLERANCE`). Catches NASS structurally dropping the canonical variant for a crop.
- **Inline guard 1**: slug collision check during commodity slugification.
- **Inline guard 2**: stale-file prune. Any leaf present in the prior tree but absent in this refresh is unlinked so the next git commit captures the deletion.
- **Inline guard 3**: leaf-shape assert at emit time. Every point leaf is structurally checked against `data/_schema/leaf.json` before write. A producer regression aborts before any file lands on disk.

Failures abort before commit. The previous snapshot stays live; healthchecks alerts on the next ping miss.

## Local development

```bash
# Run the refresh locally (downloads ~1 GB from NASS)
python scripts/refresh.py

# Run tests (no network)
python -m unittest tests.test_refresh
```

## License

Code: MIT (see [LICENSE](LICENSE)). Data published under `data/` derives from USDA NASS public-domain sources.
