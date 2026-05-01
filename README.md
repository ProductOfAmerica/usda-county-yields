# NASS County Crop Yields

A zero-cost queryable cache of USDA NASS county-level **crop** yields, served as static JSON over the jsDelivr CDN, refreshed weekly from the NASS bulk file. Sharded by `(state, county, crop)` so a point lookup downloads a small leaf instead of a multi-megabyte state shard.

## Quick lookup

A single `(state, county, crop, year)` lookup is two cached fetches: one `index.json` for discovery + freshness, then one tiny leaf for the actual data.

```
GET https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/index.json
GET https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/states/{fips}/counties/{county_code}/{crop_slug}.json
```

Pick the canonical series with one line: the producer pre-marks the `series[]` entry that matches `class="ALL CLASSES"` + `prodn_practice="ALL PRODUCTION PRACTICES"` + `unit="BU / ACRE"` with `"canonical": true`. Read `values[year]` off that series.

Example, "Story County, Iowa corn-grain yield 2024":

```
GET https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/states/19/counties/169/corn.json
→ series[s for s if s.canonical]
→ values["2024"]   →   215.5
```

Working Python lookup:

```python
import json, urllib.request

url = "https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/states/19/counties/169/corn.json"
leaf = json.load(urllib.request.urlopen(url))
canonical = next(s for s in leaf["series"] if s.get("canonical"))
print(canonical["values"]["2024"])   # 215.5
```

State and county codes are USDA / Census ANSI codes. Reference: https://www.census.gov/library/reference/code-lists/ansi.html.

For a state-wide scan of one crop (e.g., all counties for corn in Iowa), use the rollup so the whole answer comes in one fetch instead of ~99 cold leaf requests:

```
GET https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/states/19/crops/corn.json
→ counties[county_code].series[s for s if s.canonical].values[year]
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
  "schema_version": 2,
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
  "schema_version": 2,
  "state":     { "fips": "19", "alpha": "IA", "name": "IOWA" },
  "county":    { "code": "169", "name": "STORY" },
  "commodity": { "slug": "corn", "desc": "CORN" },
  "series": [
    {
      "class": "ALL CLASSES",
      "prodn_practice": "ALL PRODUCTION PRACTICES",
      "util_practice": "GRAIN",
      "unit": "BU / ACRE",
      "short_desc": "CORN, GRAIN - YIELD, MEASURED IN BU / ACRE",
      "canonical": true,
      "values":     { "2024": 215.5, "2023": 211.4 },
      "suppressed": { "1980": "D" },
      "raw":        {}
    }
  ]
}
```

- **Year keys are strings**, not numbers (JSON object key semantics).
- `values` carries numeric yields. `suppressed` carries NASS suppression codes (`D`, `NA`, `S`, `X`, `Z` per the NASS Quick Stats glossary). `raw` carries any cell value that didn't parse as numeric or suppression code, with the original string preserved for forensic audit.
- Multiple `series` entries per commodity capture variants like corn-grain (BU/ACRE) vs corn-silage (TONS/ACRE). The producer pre-marks the canonical entry with `"canonical": true`; consumers should prefer that flag instead of replicating the filter.
- **Supported commodity slugs:** `corn`, `soybeans`, `wheat`. Note `soybeans` is plural.
- `header_observed` is published once at `data/_audit/latest.json` (not per state). NASS adding a new column logs here without aborting the refresh; renaming a depended-on column does abort.
- `county.code` is always populated and zfilled to 3 digits. The legacy NASS `ansi` field is dropped at the leaf level because it equals `code` for every populated row and is sometimes blank.
- **Machine-readable contract**: [`data/_schema/leaf.json`](data/_schema/leaf.json) is a JSON Schema 2020-12 document describing the point-leaf shape. Producers and consumers can both validate against it.

## Scope

- **SURVEY** rows only (annual; CENSUS deferred until a consumer asks for it).
- **CORN, SOYBEANS, WHEAT** only. Other commodities ship when consumer demos require them.
- **County-level YIELD** only. State rollups, production, area-harvested, etc. are out of scope.

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
