# NASS County Crop Yields

A zero-cost queryable cache of USDA NASS county-level **crop** yields, served as static JSON over the jsDelivr CDN, refreshed weekly from the NASS bulk file.

Two parallel publication paths are served from the same refresh:

- **v2 (recommended for new consumers)** — finely sharded by `(state, county, crop)` so a point lookup downloads ~2-22 KB instead of ~5 MB.
- **v1 (still published; do not break)** — one ~1.5-5 MB shard per state. Existing consumers keep working unchanged.

## Quick lookup (v2, recommended)

A single `(state, county, crop, year)` lookup is two cached fetches: one `index.json` for discovery + freshness, then one tiny leaf for the actual data.

```
GET https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/v2/index.json
GET https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/v2/states/{fips}/counties/{county_code}/{crop_slug}.json
```

Pick the canonical series with one line: the producer pre-marks the `series[]` entry that matches `class="ALL CLASSES"` + `prodn_practice="ALL PRODUCTION PRACTICES"` + `unit="BU / ACRE"` with `"canonical": true`. Read `values[year]` off that series.

Example, "Story County, Iowa corn-grain yield 2024":

```
GET https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/v2/states/19/counties/169/corn.json
→ series[s for s if s.canonical]
→ values["2024"]   →   215.5
```

Working Python lookup (v2):

```python
import json, urllib.request

url = "https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/v2/states/19/counties/169/corn.json"
leaf = json.load(urllib.request.urlopen(url))
canonical = next(s for s in leaf["series"] if s.get("canonical"))
print(canonical["values"]["2024"])   # 215.5
```

For a state-wide scan of one crop (e.g., all 99 Iowa counties for corn), use the rollup so the whole answer comes in one fetch instead of ~99 cold leaf requests:

```
GET https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/v2/states/19/crops/corn.json
→ counties[county_code].series[s for s if s.canonical].values[year]
```

`refreshed_at`, source ETag, and `source.publication_date` live only in `data/v2/index.json`. v2 leaves and rollups carry no timestamps so unchanged data does not cause weekly file rewrites.

### CDN consistency window

v2 splits each state across `index.json` + per-county leaves + per-crop rollups, all served from `@main`. jsDelivr edges populate independently, so for up to ~12 h after a refresh an edge can return a fresh `index.json` alongside stale or missing leaves. Consumers needing point-in-time consistency should pin a commit SHA in the URL (`@<sha>` instead of `@main`); jsDelivr serves SHA-pinned URLs from immutable git history.

## Quick lookup (v1)

```
GET https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/v1/states/{state_fips}.json
```

Each file holds **one state's** counties × commodities × yield series. Lookup `(state, county, crop, year)` in three steps:

1. Fetch the state file (per-state, served from `@main`).
2. Pick the **canonical series** under `counties[county_code].commodities[commodity_slug].series[]`. There can be more than one entry per commodity (e.g. corn-grain BU/ACRE vs corn-silage TONS/ACRE), so do not blindly grab `series[0]`. Filter by:
   - `class == "ALL CLASSES"`
   - `prodn_practice == "ALL PRODUCTION PRACTICES"`
   - `unit` matching what you want (e.g. `"BU / ACRE"` for corn grain, `"TONS / ACRE"` for corn silage)
3. Read `values[year]`.

State and county codes are USDA / Census ANSI codes. Reference: https://www.census.gov/library/reference/code-lists/ansi.html.

Example, "Story County, Iowa corn-grain yield 2024":

```
GET https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/v1/states/19.json
→ counties["169"].commodities["corn"].series[]
  filter: class="ALL CLASSES", prodn_practice="ALL PRODUCTION PRACTICES", unit="BU / ACRE"
  → values["2024"]   →   215.5
```

Working Python lookup (v1):

```python
import json, urllib.request

url = "https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/v1/states/19.json"
state = json.load(urllib.request.urlopen(url))

corn = state["counties"]["169"]["commodities"]["corn"]
canonical = next(
    s for s in corn["series"]
    if s["class"] == "ALL CLASSES"
    and s["prodn_practice"] == "ALL PRODUCTION PRACTICES"
    and s["unit"] == "BU / ACRE"
)
print(canonical["values"]["2024"])   # 215.5
```

## File size and latency expectations

State files are **~1.5 MB to ~5 MB** for full-coverage Midwest states (Iowa, Illinois, Nebraska, Indiana). Sparse states are ~100 KB. All well under jsDelivr's 20 MB per-file cap.

Cold-fetch latency from a non-edge region: **~500 ms to ~2 s**. Warm-cache latency: **<100 ms**. The intended deployment pattern is a hot cache (e.g., Cloudflare 60-min TTL) in front of jsDelivr; this cache is the durable tier behind that.

## Schema

```json
{
  "schema_version": 1,
  "product_name": "NASS county crop yields (v1)",
  "refreshed_at": "2026-04-30T07:13:37Z",
  "source": {
    "url": "https://www.nass.usda.gov/datasets/qs.crops_20260430.txt.gz",
    "last_modified": "Thu, 30 Apr 2026 07:13:37 GMT",
    "etag": "...",
    "publication_date": "2026-04-30",
    "freshness_lag_days": 0
  },
  "header_observed": ["SOURCE_DESC", "SECTOR_DESC", "..."],
  "state": { "fips": "19", "alpha": "IA", "name": "IOWA" },
  "counties": {
    "169": {
      "name": "STORY",
      "ansi": "169",
      "commodities": {
        "corn": {
          "commodity_desc": "CORN",
          "series": [
            {
              "class": "ALL CLASSES",
              "prodn_practice": "ALL PRODUCTION PRACTICES",
              "util_practice": "GRAIN",
              "unit": "BU / ACRE",
              "short_desc": "CORN, GRAIN - YIELD, MEASURED IN BU / ACRE",
              "values":     { "2024": 215.5, "2023": 211.4 },
              "suppressed": { "1980": "D" },
              "raw":        {}
            }
          ]
        }
      }
    }
  }
}
```

- **Year keys are strings**, not numbers (JSON object key semantics).
- `values` carries numeric yields. `suppressed` carries NASS suppression codes (`D`, `NA`, `S`, `X`, `Z` per the NASS Quick Stats glossary). `raw` carries any cell value that didn't parse as numeric or suppression code, with the original string preserved for forensic audit.
- Multiple `series` entries per commodity capture variants like corn-grain (BU/ACRE) vs corn-silage (TONS/ACRE). See Quick Lookup above for the canonical-series filter.
- **Supported commodity slugs (v1):** `corn`, `soybeans`, `wheat`. Note `soybeans` is plural.
- `header_observed` records the actual columns from this refresh's bulk file. NASS adding a new column logs here without aborting the refresh; renaming a depended-on column does abort.

## v1 scope

- **SURVEY** rows only (annual; CENSUS deferred until a consumer asks for it).
- **CORN, SOYBEANS, WHEAT** only. Other commodities ship when consumer demos require them.
- **County-level YIELD** only. State rollups, production, area-harvested, etc. are out of scope.

## Refresh cadence

- **Weekly, Monday at 09:17 UTC.** Year-round safe vs NASS's ~03:13 ET publication time (covers EDT and EST).
- Healthchecks.io configured with **9-day grace window** and **alerts on two consecutive missed pings** (single-Monday GitHub cron drops auto-recover the next week).
- Recovery from 60-day workflow auto-disable is one click on the Actions page (`Run workflow`).
- **Worst-case staleness:** ~7 to 8 days (one weekly refresh interval plus up to ~12 h CDN propagation). Steady-state is much fresher; this is the ceiling, not the typical case.

## Caveats

- **State coverage is partial.** A state shard exists only if NASS published county-level SURVEY yields for corn, soybeans, or wheat in the source file. As of this writing the cache holds 42 of 50 state shards. Absent FIPS: `02` (AK), `09` (CT), `11` (DC), `15` (HI), `23` (ME), `25` (MA), `33` (NH), `44` (RI), `50` (VT). Consumers should resolve fips and county codes from the live shard's `counties` map rather than hard-coding. If CT later returns to NASS county output, expect planning-region codes per Federal Register 2022-12063 rather than the legacy county codes.
- **Suppressed values** (`(D)`, `(NA)`, etc.) are NOT silently null. They land in the `suppressed` map keyed by year. Consumers choose to surface, ignore, or fall back.
- **Soft-fail semantics**: a missing `(state, county, crop)` is an absence in the file, not an error. The cache makes no completeness guarantee; it reflects what NASS published in the most recent bulk file.
- **Eventual consistency**: after a refresh, jsDelivr's `s-maxage=43200` means up to ~12 h before edges return the new content. If a consumer needs bit-stable reads, they can pin a tag (none currently published in v1; add later if needed).

## Architecture

- **Source**: `qs.crops_YYYYMMDD.txt.gz` from `https://www.nass.usda.gov/datasets/`, refreshed business-daily by NASS.
- **Refresh**: GitHub Actions cron runs `scripts/refresh.py` on a public repo (free).
- **Storage**: this Git repo. Per-state JSON committed weekly.
- **CDN**: jsDelivr-fronted GitHub raw, free, no auth.
- **Monitoring**: healthchecks.io free tier deadman switch.

Two validation gates and two inline guards before any publish:
- **Gate 1**: required columns present in NASS header (tolerant reader; extra columns OK).
- **Gate 2**: filtered row count within ±10% of last successful run (skipped on bootstrap).
- **Inline guard 1**: slug collision check during commodity slugification.
- **Inline guard 2**: `git rm` for any state file present in the prior tree but absent in this refresh's staging.

Failures abort before commit. The previous snapshot stays live; healthchecks alerts on the next ping miss.

## Why this and not the heavy version

See [docs/archive/heavy-plan-v0.md](docs/archive/heavy-plan-v0.md) for the original heavier design (per-county-commodity sharding, SHA-pin manifest protocol, business-daily cadence, 6-gate validator, regression tests for unobserved failures). It was correct for *some* consumer; the slim v1 here is calibrated to the actual first consumer (a soft-fail Layer-3 narrative-report workflow that captures snapshots once and freezes them onto a row).

## Local development

```bash
# Run the refresh locally (downloads ~1 GB from NASS)
python scripts/refresh.py

# Run tests (no network)
python -m unittest tests.test_refresh
```

## License

Code: MIT (see [LICENSE](LICENSE)). Data published under `data/v1/` and `data/v2/` derives from USDA NASS public-domain sources.
