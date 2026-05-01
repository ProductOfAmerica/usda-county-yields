# NASS County Crop Yields (v1)

A zero-cost queryable cache of USDA NASS county-level **crop** yields, served as static JSON over the jsDelivr CDN, refreshed weekly from the NASS bulk file.

## Quick lookup

```
GET https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/v1/states/{state_fips}.json
```

Each file holds **one state's** counties × commodities × yield series. Lookup `(state, county, crop, year)` in two steps:

1. Fetch the state file (per-state, served from `@main`).
2. Navigate `counties[county_code].commodities[commodity_slug].series[].values[year]`.

Example, "Story County, Iowa corn yield 2024":

```
GET https://cdn.jsdelivr.net/gh/ProductOfAmerica/usda-county-yields@main/data/v1/states/19.json
→ counties["169"].commodities["corn"].series[0].values["2024"]   →   218.9
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
              "values":     { "2024": 218.9, "2023": 201.5 },
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
- Multiple `series` entries per commodity capture variants like corn-grain (BU/ACRE) vs corn-silage (TONS/ACRE).
- `header_observed` records the actual columns from this refresh's bulk file. NASS adding a new column logs here without aborting the refresh; renaming a depended-on column does abort.

## v1 scope

- **SURVEY** rows only (annual; CENSUS deferred until a consumer asks for it).
- **CORN, SOYBEANS, WHEAT** only. Other commodities ship when consumer demos require them.
- **County-level YIELD** only. State rollups, production, area-harvested, etc. are out of scope.

## Refresh cadence

- **Weekly, Monday at 09:17 UTC.** Year-round safe vs NASS's ~03:13 ET publication time (covers EDT and EST).
- Healthchecks.io configured with **9-day grace window** and **alerts on two consecutive missed pings** (single-Monday GitHub cron drops auto-recover the next week).
- Recovery from 60-day workflow auto-disable is one click on the Actions page (`Run workflow`).

## Caveats

- **Connecticut**: in 2022 CT switched to planning regions for federal statistics (Federal Register 2022-12063). The cache reflects whatever FIPS codes NASS publishes; consumers should resolve county codes from each state file's `counties` map rather than hard-coding.
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

Code: MIT (see [LICENSE](LICENSE)). Data published under `data/v1/` derives from USDA NASS public-domain sources.
