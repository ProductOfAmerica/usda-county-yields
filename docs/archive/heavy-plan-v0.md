> **Superseded by slim v1 derived from field-mcp consumer scan.** Kept for context on why decisions were made, not as a build spec.

# Zero-Cost Queryable Cache for USDA NASS County Crop Yields (v1)

> **v1 scope:** county-level CROP yields from `qs.crops_*.txt.gz`. Animal-product yields (milk/cow, honey/colony, etc.) are deferred to v2 — see [TODOS.md](TODOS.md).

## 1. Context

USDA NASS publishes the authoritative U.S. agricultural-statistics database. Their Quick Stats HTTP API (`quickstats.nass.usda.gov/api`) is free but slow and prone to outages, so it cannot be the read path for application code that needs sub-second lookups by `(state_fips, county_code, commodity, year)`.

NASS also publishes the *same data* as a date-stamped bulk file (`qs.crops_YYYYMMDD.txt.gz`), refreshed every business day. Building our own queryable cache from that bulk source is an order of magnitude more reliable than calling Quick Stats live, and at the volumes stated (hundreds of reads/day initially, business-daily batch writes) the entire system fits inside multiple providers' free tiers without paid infrastructure.

Goals:
- Free at low-to-moderate volume; stays free as reads grow 100×.
- No servers, databases, or paid services to maintain.
- Survives intact for years with minimal human intervention.
- `(state, county, crop, year)` lookup under one second from server-side application code.
- Refreshes business-daily from the NASS bulk file with revision tracking and silent-failure detection.

## 2. Verified Facts

All facts confirmed against primary sources with `curl` evidence captured during investigation. Items marked **unverified** are flagged for runtime resolution.

### 2.1 NASS bulk source

- **Canonical bulk URL pattern** — `https://www.nass.usda.gov/datasets/qs.<sector>_YYYYMMDD.txt.gz` plus `qs.census<YEAR>.txt.gz` for Census of Ag snapshots. Verified live with `curl -I`:
  ```
  HTTP/1.1 200 OK
  Last-Modified: Thu, 30 Apr 2026 07:13:37 GMT
  Content-Length: 1122047958
  Content-Type: application/x-gzip
  Accept-Ranges: bytes
  cache-control: max-age=86400
  ```
- **Refresh cadence** — *"You can also download QuickStats data (\*.gz). The data files are updated following each week day."* Source: `https://www.nass.usda.gov/Quick_Stats/`. Business days only (Mon–Fri). URL changes each weekday; must be discovered, not hard-coded.
- **Sector files** in `https://www.nass.usda.gov/datasets/`: `qs.crops_*` (1.04 GB), `qs.animals_products_*` (462 MB; v2 scope), `qs.economics_*` (584 MB), `qs.demographics_*` (~445 MB), `qs.environmental_*` (~70 MB), plus `qs.census<YYYY>.txt.gz`. **v1 ingests `qs.crops_*` only.**
- **Range requests** are accepted (`Accept-Ranges: bytes`) but gzip cannot be decompressed mid-stream — partial reads must start at byte 0. Streaming end-to-end is required for a full refresh.

### 2.2 Schema (verified by reading byte-zero of `qs.crops_20260430.txt.gz`)

39 tab-separated columns:

```
SOURCE_DESC, SECTOR_DESC, GROUP_DESC, COMMODITY_DESC, CLASS_DESC,
PRODN_PRACTICE_DESC, UTIL_PRACTICE_DESC, STATISTICCAT_DESC, UNIT_DESC,
SHORT_DESC, DOMAIN_DESC, DOMAINCAT_DESC, AGG_LEVEL_DESC, STATE_ANSI,
STATE_FIPS_CODE, STATE_ALPHA, STATE_NAME, ASD_CODE, ASD_DESC,
COUNTY_ANSI, COUNTY_CODE, COUNTY_NAME, REGION_DESC, ZIP_5,
WATERSHED_CODE, WATERSHED_DESC, CONGR_DISTRICT_CODE, COUNTRY_CODE,
COUNTRY_NAME, LOCATION_DESC, YEAR, FREQ_DESC, BEGIN_CODE, END_CODE,
REFERENCE_PERIOD_DESC, WEEK_ENDING, LOAD_TIME, VALUE, CV_%
```

Definitions verified at `https://quickstats.nass.usda.gov/api`. Notable gotchas:
- Corn-grain vs corn-silage lives in `UTIL_PRACTICE_DESC`, not `CLASS_DESC`.
- CV column header is literally `CV_%` (not `cv_pct`); populated only for the 2012 Census of Agriculture per the official reference.
- `LOAD_TIME` is database insert timestamp, not publication date.
- `VALUE` is mixed numeric/string. Suppressed cells appear as parenthesized text per `https://quickstats.nass.usda.gov/src/glossary.pdf`: `(D)` withheld, `(NA)` not available, `(S)` insufficient reports, `(X)` not applicable, `(Z)` less than half rounding unit. May also include whitespace, commas, decimals — must normalize before numeric cast.
- `DOMAIN_DESC` and `DOMAINCAT_DESC` slice the data along orthogonal demographic/economic axes. **For yield series, the canonical row has `DOMAIN_DESC = 'TOTAL'` and `DOMAINCAT_DESC = 'NOT SPECIFIED'`** — must be in the filter.

### 2.3 Filter for "<crop> yield in <county>, <state>, <year>"

```sql
SOURCE_DESC IN ('SURVEY', 'CENSUS')           -- both retained as separate series
AGG_LEVEL_DESC = 'COUNTY'
STATISTICCAT_DESC = 'YIELD'
DOMAIN_DESC = 'TOTAL'                         -- excludes demographic/economic slices
DOMAINCAT_DESC = 'NOT SPECIFIED'              -- pairs with DOMAIN_DESC=TOTAL
FREQ_DESC = 'ANNUAL'
REFERENCE_PERIOD_DESC = 'YEAR'
COMMODITY_DESC, CLASS_DESC, PRODN_PRACTICE_DESC, UTIL_PRACTICE_DESC, UNIT_DESC = ...
STATE_FIPS_CODE = '19'                        -- Iowa
COUNTY_CODE = '169'                           -- Story
YEAR = 2024
```

Real matching row from the bulk file (Kentucky's Union County, 2021):

```
SURVEY  CROPS  FIELD CROPS  CORN  ALL CLASSES  ALL PRODUCTION PRACTICES
GRAIN  YIELD  BU / ACRE  CORN, GRAIN - YIELD, MEASURED IN BU / ACRE
TOTAL  NOT SPECIFIED  COUNTY  21  21  KY  KENTUCKY  20  MIDWESTERN
225  225  UNION  ...  KENTUCKY, MIDWESTERN, UNION  2021  ANNUAL
00  00  YEAR    2024-02-23 15:00:00  218.9  1.3
```

### 2.4 Edge cases

- **Class taxonomy**: CORN has `ALL CLASSES, TRADITIONAL OR INDIAN`; SOYBEANS has `ALL CLASSES`; WHEAT has 10 classes (`ALL CLASSES`, `RED HARD`, `SPRING (EXCL DURUM)`, `SPRING DURUM`, `SPRING RED HARD`, `WHITE`, `WINTER`, `WINTER RED HARD`, `WINTER RED SOFT`, `WINTER WHITE SOFT`). The cache preserves all variants.
- **Corn silage** uses `UTIL_PRACTICE_DESC = 'SILAGE'` and `UNIT_DESC = 'TONS / ACRE'`. Separate rows from grain.
- **Connecticut**: in 2022 CT transitioned from 8 counties to 9 planning regions (Federal Register 2022-12063). The 5 MB sample shows NASS still serving CT under the legacy 8-county FIPS through Census 2022. Whether NASS post-2022 ANNUAL SURVEY uses the new planning-region codes (09110–09190) is **unverified** — must inspect a 2024+ row at first refresh. The pipeline accepts whatever FIPS codes appear; per-state manifest reports actual codes present.
- **County identifier**: use `COUNTY_CODE` (3-digit) for the URL path. Full FIPS = `STATE_FIPS_CODE + COUNTY_CODE` (5-digit). County names (`COUNTY_NAME`) are not stable identifiers — independent cities, parishes, boroughs, renames — codes only.
- **Suppressed values** stored as parenthesized text in `VALUE`: parser routes `^\([A-Z]+\)$` matches to `suppressed.{year}`, normalizes leading/trailing whitespace and embedded thousand-separator commas before numeric cast, and falls back to a `raw_value` escape hatch for any cell that matches neither pattern.
- **Year coverage**: county-level CORN yield reaches back to **1911** in SURVEY. Other commodities range 1915–1955.
- **Filtered row count** for `(AGG_LEVEL_DESC = COUNTY) AND (STATISTICCAT_DESC = YIELD) AND (DOMAIN_DESC = TOTAL)`: order of magnitude **2–4 million rows** across major commodities. To be confirmed on first refresh; recorded as `row_counts.after_county_yield_filter` in every snapshot manifest.

### 2.5 Hosting (free-tier, primary-source-cited)

| Option | Free-tier headroom | Critical constraint | Verdict |
|---|---|---|---|
| **GitHub repo + jsDelivr CDN** | jsDelivr unlimited bandwidth on open-source CDN; GitHub repo recommended ≤10 GB | jsDelivr GitHub-path single-file cap **20 MB** (verified jsDelivr docs); 14 GB Actions runner disk is the operational ceiling | **Primary choice** |
| Cloudflare R2 + custom domain | 10 GB storage, 10M reads/mo, 1M writes/mo, **$0 egress** | `r2.dev` is rate-limited dev-only; production needs custom domain (~$10/yr, not strictly $0) | Documented upgrade path |
| GitHub Pages | 1 GB site, 100 GB/mo bandwidth | 1 GB cap risks long-term overflow | Backup |
| `raw.githubusercontent.com` direct | n/a | 60/hr unauth (May 2025 changelog); `Cache-Control: max-age=300`; ToS prohibits "excessive automated bulk activity" | Disqualified at 100× growth |
| Vercel Hobby | 100 GB bandwidth | ToS: non-commercial only | Disqualified |
| Netlify Free | 100 GB bandwidth | ToS forbids "remote storage server" use | Disqualified |
| Cloudflare KV | 100k reads/day | 1k writes/day kills batch refresh | Disqualified |
| Cloudflare D1 | n/a | Requires Worker for HTTP query | Out of scope |
| Turso | n/a | HTTP API requires bearer token | Doesn't fit "stable public URL" |

Verified header captures:

```
# raw.githubusercontent.com — short cache
$ curl -sI https://raw.githubusercontent.com/torvalds/linux/master/README
Cache-Control: max-age=300

# jsDelivr-fronted GitHub — week-long max-age, immutable when ref pinned
$ curl -sI https://cdn.jsdelivr.net/gh/torvalds/linux@master/README
cache-control: public, max-age=604800, s-maxage=43200
cf-cache-status: REVALIDATED
```

Latency from this Windows machine (residential link, not a CDN edge):

```
$ curl -w "%{time_total}s %{size_download}B\n" -o /dev/null -s https://cdn.jsdelivr.net/gh/torvalds/linux@v6.14/Makefile
3.432453s 70563B    # cold first hit
0.078429s           # warm
0.074143s           # warm
```

For target shard size (5–15 KB), warm-cache p50 is well under 100 ms; cold-first-ever fetches finish in low single-digit seconds, bounded by jsDelivr's edge fill.

### 2.6 Cron / refresh

| Option | Free quota | Single-job | Verdict |
|---|---|---|---|
| **GitHub Actions on a public repo** | Free, no cap | 4 vCPU / 16 GB RAM / 14 GB SSD, 6 h cap | **Primary choice** |
| GitHub Actions, private repo | 2,000 min/mo | 2 vCPU / 8 GB / 14 GB | Equivalent if private |
| Vercel Cron Hobby | n/a | Daily-only, ±59 min jitter, function timeout | Disqualified |
| Cloudflare Workers cron | 100k req/day | 30 s wall, 10 ms CPU | Disqualified (CPU/RAM too small) |
| GitLab CI | 400 min/mo | shared runners | Workable, tighter |
| AWS Lambda + EventBridge | 1M req/mo + 400k GB-s | 15 min hard | Workable, multi-stage |

Documented quirks the design must mitigate:

> *"The `schedule` event can be delayed during periods of high loads of GitHub Actions workflow runs. High load times include the start of every hour. If the load is sufficiently high enough, some queued jobs may be dropped."* — `docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows`

Plus: scheduled workflows on public repos auto-disable after 60 days of repository inactivity. Mitigations: schedule at an off-peak minute, pair with healthchecks.io alerting, and accept that recovering from inactivity-disable is a one-click manual operation roughly twice a year worst case.

### 2.7 Deadman switch

`healthchecks.io` free tier: 20 checks, multi-channel alerts on missed pings. Verified live at `https://hc-ping.com/`. Free tier covers our checks with abundant headroom.

## 3. Architectural Decision

### Chosen architecture

**A public GitHub repository, served as JSON files through the jsDelivr CDN, refreshed business-daily by a GitHub Actions workflow, with a healthchecks.io deadman switch.**

```
NASS bulk file (gz)              GitHub Actions runner (M-F 07:17 UTC)
  qs.crops_YYYYMMDD.txt.gz  ───► (date-probe URL · stream-decompress · filter
                                  · group · validate · diff · git rm absent)
                                       │
                                       │  git commit + tag snapshot-YYYY-MM-DD
                                       ▼
                                 GitHub repository
                                 data/v1/states/{fips}/counties/{code}/{commodity}.json
                                 data/v1/manifest.json (carries snapshot_ref = SHA)
                                       │
                                       │  jsDelivr fronts public GitHub raw
                                       ▼
                       cdn.jsdelivr.net/gh/{owner}/{repo}@{sha or main}/data/v1/...
                                       │
                                       ▼
                       Application server (Node, Python, Go, etc.)
                       1) GET manifest.json @main (fresh)
                       2) read snapshot_ref (SHA)
                       3) fetch shards @{sha} (immutable)
                       in-memory cache + lookup by (state, county, crop, year)
```

### Why this and not the alternatives

- **Truly $0/month for years 2 through 5.** No domain registration required; jsDelivr provides the public hostname. R2 with custom domain is the upgrade path if a domain is ever available.
- **ToS-clean for an open-data cache**, unlike Vercel Hobby (non-commercial only) or Netlify (forbids "remote storage server" use).
- **Survives 100× read growth.** jsDelivr serves from Cloudflare and Fastly edge nodes with `cache-control: public, max-age=604800, s-maxage=43200`. Shards pinned to a commit SHA also get `immutable, max-age=31536000` for one full year, so the hot path is one HEAD-cache-hit even at scale.
- **Survives a 1 GB ETL.** GitHub-hosted runners give 4 vCPU / 16 GB RAM / 14 GB SSD. Streaming gzip + csv keeps memory footprint at buffer size, not file size, so the 14 GB disk is the binding constraint, not RAM.
- **Survives long-term abandonment.** A public GitHub repo with periodic commits is one of the most durable storage substrates available without payment. Even if jsDelivr were to disappear, every snapshot is fetchable directly from `raw.githubusercontent.com` (rate-limited but functional) and locally as `git clone`.

**GitHub-as-data-store caveat.** GitHub's recommended substrate is code, not generated datasets. This design is generated-data-in-Git. It works because: shards are small (median ~10 KB), per-commit churn is small (~3 MB across ~100–500 changed files), and Git deduplicates unchanged blobs. The 14 GB Actions runner disk is the binding constraint and is monitored (see Section 6).

### Upgrade path

If a custom domain becomes available, the architecture lifts cleanly to **Cloudflare R2 with a custom domain**. Data shape (Section 4) and refresh logic (Section 6) are reused; only the publish step changes from `git push` to `aws s3 sync` against R2's S3-compatible API. Consumer URL base is configurable; no shape change.

### Public vs private repo

The cache repository is **public**. Required for jsDelivr to serve it for free. Published data is public-domain U.S. government data — no privacy concern.

## 4. Data Shape

### 4.1 File layout

```
data/
  v1/
    manifest.json                          # top-level index, names snapshot_ref (SHA)
    schema.json                            # JSON Schema for series files
    states/
      {state_fips}/                        # 19 = Iowa
        manifest.json                      # county list for this state
        counties/
          {county_code}/                   # 169 = Story (3-digit, not full FIPS)
            {commodity_slug}.json          # corn.json, soybeans.json, ...
.refresh-state.json                        # internal: last_successful_date, etag, baselines
```

`{commodity_slug}` is generated by an explicit deterministic slug function:
1. Lowercase `COMMODITY_DESC`.
2. Replace any character not in `[a-z0-9]` with `-`.
3. Collapse runs of `-`.
4. Strip leading/trailing `-`.
5. Verify uniqueness across all distinct `COMMODITY_DESC` values seen this refresh; if a collision occurs, append `-N` deterministically.

The top-level manifest carries a `commodity_slug → COMMODITY_DESC` mapping so consumers can resolve back. Slug collisions trigger a validation warning.

Multiple class/util/practice/source variants live as separate `series` entries inside the same commodity file. SURVEY and CENSUS series for the same commodity coexist, distinguished by `source`.

### 4.2 Per-shard schema

```json
{
  "schema_version": 1,
  "refreshed_at": "2026-04-30T07:13:37Z",
  "snapshot_ref": "abc123def4567890...",
  "source": {
    "url": "https://www.nass.usda.gov/datasets/qs.crops_20260430.txt.gz",
    "last_modified": "2026-04-30T07:13:37Z",
    "etag": "42e117d6-650a8340824f6"
  },
  "state": { "fips": "19", "alpha": "IA", "name": "IOWA" },
  "county": { "code": "169", "ansi": "169", "name": "STORY", "full_fips": "19169" },
  "commodity": "CORN",
  "commodity_slug": "corn",
  "series": [
    {
      "source": "SURVEY",
      "class": "ALL CLASSES",
      "prodn_practice": "ALL PRODUCTION PRACTICES",
      "util_practice": "GRAIN",
      "unit": "BU / ACRE",
      "short_desc": "CORN, GRAIN - YIELD, MEASURED IN BU / ACRE",
      "values": { "1911": 36.0, "...": "...", "2024": 218.9 },
      "suppressed": { "1980": "D" },
      "raw": {}
    },
    {
      "source": "CENSUS",
      "class": "ALL CLASSES",
      "prodn_practice": "ALL PRODUCTION PRACTICES",
      "util_practice": "GRAIN",
      "unit": "BU / ACRE",
      "short_desc": "CORN, GRAIN - YIELD, MEASURED IN BU / ACRE",
      "values": { "2017": 215.4, "2022": 219.8 },
      "suppressed": {}
    },
    {
      "source": "SURVEY",
      "class": "ALL CLASSES",
      "prodn_practice": "ALL PRODUCTION PRACTICES",
      "util_practice": "SILAGE",
      "unit": "TONS / ACRE",
      "short_desc": "CORN, SILAGE - YIELD, MEASURED IN TONS / ACRE",
      "values": { "2024": 21.5 },
      "suppressed": {}
    }
  ]
}
```

Numeric values land in `values`. Suppressed cells land in `suppressed` keyed by year with the bare letter code. Unparseable cells (rare; whitespace/comma normalization fails) land in `raw` keyed by year for forensic audit. `short_desc` carried per series for debugging when nearly-identical series need disambiguation.

Consumer convention (documented in README): prefer SURVEY series for annual time-series queries; fall back to CENSUS series if SURVEY is absent for that `(county, commodity, year)`.

### 4.3 Top-level manifest

```json
{
  "schema_version": 1,
  "product_name": "NASS county crop yields",
  "refreshed_at": "2026-04-30T07:13:37Z",
  "snapshot_ref": "abc123def4567890...",
  "source": {
    "url": "https://www.nass.usda.gov/datasets/qs.crops_20260430.txt.gz",
    "last_modified": "2026-04-30T07:13:37Z",
    "etag": "42e117d6-650a8340824f6",
    "freshness_lag_days": 0
  },
  "row_counts": {
    "bulk_total": 22011453,
    "after_county_yield_filter": 2843177,
    "files_written": 12384,
    "files_deleted": 0
  },
  "header_observed": ["SOURCE_DESC", "SECTOR_DESC", "..."],
  "commodity_slugs": {
    "corn": "CORN",
    "soybeans": "SOYBEANS",
    "wheat": "WHEAT"
  },
  "states": [
    { "fips": "01", "alpha": "AL", "name": "ALABAMA", "county_count": 67, "commodity_count": 14, "shard_count": 412 },
    { "fips": "02", "alpha": "AK", "name": "ALASKA", "county_count": 4, "commodity_count": 3, "shard_count": 9 }
  ]
}
```

**Forensic trail**: `header_observed` records the actual column names from this refresh's bulk file, so any column addition/rename by NASS is captured even when the tolerant reader doesn't abort. `freshness_lag_days` reports the gap between refresh attempt and the bulk file's `Last-Modified` (used for alerting).

Per-state manifest at `data/v1/states/{fips}/manifest.json` lists counties and their commodities.

## 5. Partitioning Rationale

### Constraints

- jsDelivr GitHub-path single-file cap: **20 MiB** (corrected from earlier 50 MiB figure).
- GitHub recommended repo size: **10 GB**; **14 GB Actions runner disk** is the binding operational ceiling.
- Sub-second cold latency target: ~200 KB transfer over a CDN finishes within budget.
- Filtered row count: ~2–4 million rows for county yields across major commodities.

### Sharding option comparison

| Shape | Files | Avg size | Cold-miss transfer | Verdict |
|---|---|---|---|---|
| One JSON | 1 | ~250 MB | catastrophic | breaks every cap |
| Per state | ~50 | ~5 MB | seconds | misses sub-second target |
| Per state + commodity | ~500 | ~500 KB | ~500 ms | acceptable |
| **Per state + county + commodity** | **~12k–30k** | **~5–15 KB** | **<100 ms** | **chosen** |
| Per state + county + commodity + year | ~2–4M | ~50 B | tiny | overhead-dominated, breaks repo |

### Chosen partitioning: `(state, county, commodity)`

- Read key matches the user's `(state_fips, county_code, commodity, year)` lookup, with year selected client-side from a small dict.
- Cold-cache miss: ~5–15 KB at jsDelivr edge → comfortably sub-second.
- Warm-cache miss: ~75 ms measured.
- Realistic file count is in the low tens of thousands; counties only have files for the commodities they produce.

### Repo growth (no squash strategy)

Snapshot at ~12k files × ~10 KB ≈ ~120 MB on disk. Per-refresh diff is ~3 MB across ~100–500 changed files (most yield numbers, once published, never change). Business-daily for 5 years = ~1300 commits × ~3 MB = ~4 GB diffs + 120 MB base ≈ ~4 GB. Sits comfortably below GitHub's 10 GB recommendation and the 14 GB runner-disk ceiling.

The earlier "annual force-push squash" mitigation has been removed: the math doesn't justify it, and force-push has real costs (broken `git clone` for downstream users, tag-reachability surprises, jsDelivr cache behavior under rewritten history). If the repo ever crosses 4 GB, revisit.

The refresh workflow runs `git rm` for any shard present in the prior tree but absent in staging, so removed coverage doesn't leave stale data behind.

## 6. Refresh Design

### 6.1 Schedule

`.github/workflows/refresh.yml`:

```yaml
on:
  schedule:
    - cron: "17 7 * * 1-5"     # M-F 07:17 UTC, off-peak per GH cron load notes
  workflow_dispatch: {}
permissions:
  contents: write              # commit + push + tag
  issues: write                # gh issue create on validation failure
  pull-requests: write         # if PR-time E2E ever opens an issue
concurrency:
  group: refresh
  cancel-in-progress: false    # never overlap two refreshes
```

Off-the-hour minute and `1-5` weekday mask deliberately match NASS publication and dodge GitHub's documented top-of-hour cron load shedding.

### 6.2 Pipeline (Python stdlib; `scripts/refresh.py`)

1. **Discover** today's bulk URL via deterministic date-probe:
   - Read `.refresh-state.json` for `last_successful_date`.
   - Probe forward from `max(today, last_successful_date + 1 day)` walking forward day-by-day with `curl -I`.
   - If no fresh file in the forward direction, walk backward from today to `today - 14 days`.
   - On match: capture `Last-Modified`, `Content-Length`, `ETag`. If the matched URL equals the previously-stored URL and the ETag matches, exit cleanly with no work (same-day rerun).
   - If walk completes with no 200 within 14 days, exit non-zero. `gh issue create` is invoked from the workflow; healthchecks alerts after the 2-day grace window.
2. **Download** the bulk file via streaming HTTP. Retry with exponential backoff (3 attempts: 30 s / 2 m / 8 m). After 3 failures, exit non-zero.
3. **Tolerant header read.** Read the first line of the gzip stream as a TSV header. Build a `name → index` map. Required columns (the ones we filter or emit on) are: `SOURCE_DESC, COMMODITY_DESC, CLASS_DESC, PRODN_PRACTICE_DESC, UTIL_PRACTICE_DESC, STATISTICCAT_DESC, UNIT_DESC, SHORT_DESC, DOMAIN_DESC, DOMAINCAT_DESC, AGG_LEVEL_DESC, STATE_FIPS_CODE, STATE_ALPHA, STATE_NAME, COUNTY_CODE, COUNTY_ANSI, COUNTY_NAME, YEAR, FREQ_DESC, REFERENCE_PERIOD_DESC, VALUE`. Abort with a named error listing missing columns. Tolerate any number of extra columns. **Always** record the full observed header into the snapshot manifest's `header_observed` field for forensic trail.
4. **Stream-decompress + filter** with stdlib `gzip` + `csv.reader` (line-by-line; never holds more than buffer size in memory). Filter at row level to:
   ```
   AGG_LEVEL_DESC == 'COUNTY'
   STATISTICCAT_DESC == 'YIELD'
   FREQ_DESC == 'ANNUAL'
   REFERENCE_PERIOD_DESC == 'YEAR'
   DOMAIN_DESC == 'TOTAL'
   DOMAINCAT_DESC == 'NOT SPECIFIED'
   SOURCE_DESC IN ('SURVEY', 'CENSUS')
   ```
   `VALUE` parsing: strip whitespace; if matches `^\([A-Z]+\)$`, route to `suppressed`; remove embedded thousand-separator commas; cast to float; on cast failure, route to `raw` with original string preserved.
5. **Group** filtered rows by `(STATE_FIPS_CODE, COUNTY_CODE, COMMODITY_DESC, CLASS_DESC, PRODN_PRACTICE_DESC, UTIL_PRACTICE_DESC, UNIT_DESC, SOURCE_DESC, SHORT_DESC)` accumulating `{year: value or suppression_code or raw}`. Drop the `SHORT_DESC` from the group key for output (kept as series field), but log warnings if two distinct `SHORT_DESC` values land in the same series — indicates the key is incomplete.
6. **Validate** before publishing:
   - Required columns present (already enforced in step 3).
   - `bulk_total` row count is non-zero. On bootstrap (no `.refresh-state.json`), accept any non-zero count. Otherwise, within ±10% of `last_filtered_row_count.bulk_total`.
   - `after_county_yield_filter` count is non-zero. Bootstrap-tolerant; otherwise within ±10%.
   - **Per-state file-count delta**: each state's `shard_count` is within ±15% of the prior snapshot's per-state count. Catches localized bugs that pass the global ±10% check.
   - **Baseline lookups** drawn from prior snapshot (`.refresh-state.json.sample_lookups`): 10 random `(state, county, commodity, source, year)` tuples seen in the prior good snapshot must still resolve to a numeric value within ±25% (to allow legitimate revisions). Bootstrap mode skips this check and seeds the next run's baseline.
   - All emitted JSON files are syntactically valid.
   - Slug collision check: every distinct `COMMODITY_DESC` produces a unique slug.
   Any failure aborts before write. Exit non-zero. Workflow runs `gh issue create` with the validation log excerpt and the offending sample, so the failure is loud.
7. **Emit** to staging directory `/tmp/staging/data/v1/...`. Compare to working tree; only files whose contents changed are copied over the existing repo data. **Files present in the working tree but absent in staging are `git rm`'d** (handles dropped coverage).
8. **Publish** atomically:
   - `git add data/v1/ .refresh-state.json`
   - Update top-level manifest's `snapshot_ref` to the about-to-be-created commit SHA. Compute SHA, write into manifest, re-add manifest, commit (rewriting works because we're computing the SHA from staged content first via `git commit-tree`).
   - Or simpler: commit, capture the resulting SHA, amend the manifest with the SHA, and force-push the amended commit. The amended commit's SHA is stable thereafter. Or: use a sentinel SHA in the file (`"PENDING"`), commit, then post-commit hook updates the manifest with the real SHA in a follow-up commit. **Implementation note** — both approaches work; the follow-up-commit approach is simpler and the small lag is acceptable since consumers fetching `@main/manifest.json` after the second commit will see the correct SHA.
   - Tag `snapshot-YYYY-MM-DD`. (Tag protection rule on the repo prevents force-moves of `snapshot-*` tags.)
   - `git push origin main --tags`.
9. **Heartbeat** by `curl https://hc-ping.com/<uuid>` after a successful publish. healthchecks.io is configured with **2-day grace window** and **alerts on 3 consecutive missed business-day pings**, so single GH cron drops don't page.

### 6.3 Concurrency and failure handling

- Workflow `concurrency` lock prevents overlap.
- Network retries with backoff during download.
- If the discovered URL has `Last-Modified` older than 7 days, the script logs `freshness_lag_days` in the snapshot manifest but still publishes (best-effort current).
- Validation failure: refresh exits non-zero, GitHub Actions sends failure email, workflow runs `gh issue create --title "Refresh validation failed YYYY-MM-DD" --body "..."` so failures are loud, healthchecks fires after grace window.
- Inactivity disable (60-day rule): healthchecks alerts on 3-business-day streak. Recovery is one click on the workflow page (manual workflow_dispatch). The earlier `last_run_at.txt` keep-alive proposal was dropped: it's circular — a disabled workflow can't run the keep-alive.

## 7. Read Path

### 7.1 Public consumer URL

Two-step pattern (recommended for cross-shard consistency):

```
1. GET https://cdn.jsdelivr.net/gh/{owner}/{repo}@main/data/v1/manifest.json
   → parse, read `snapshot_ref` (commit SHA)
2. GET https://cdn.jsdelivr.net/gh/{owner}/{repo}@{snapshot_ref}/data/v1/states/{state_fips}/counties/{county_code}/{commodity_slug}.json
   → parse, lookup series + year
```

The shard URL pinned to a SHA gets `cache-control: max-age=31536000, immutable` from jsDelivr (verified). Hot-path cache behavior is excellent.

Direct `@main` shard fetch is also supported for consumers who want "always latest, drift OK" semantics:

```
GET https://cdn.jsdelivr.net/gh/{owner}/{repo}@main/data/v1/states/{state_fips}/counties/{county_code}/{commodity_slug}.json
```

The trade-off is documented in the README: with `@main`, the manifest and shards may be served from different cache generations during the ~12 hour s-maxage window after each refresh.

Manifest endpoints (always `@main`):

```
https://cdn.jsdelivr.net/gh/{owner}/{repo}@main/data/v1/manifest.json
https://cdn.jsdelivr.net/gh/{owner}/{repo}@main/data/v1/states/{fips}/manifest.json
```

### 7.2 Measured latency (this Windows machine, residential link)

Sample target: `https://cdn.jsdelivr.net/gh/torvalds/linux@v6.14/Makefile` (70.5 KB).

```
3.432453s    # cold first hit (TLS + DNS + jsDelivr cache fill)
0.078429s    # warm
0.074143s    # warm
```

For our 5–15 KB shards, warm-cache fetches finish in tens of milliseconds. Cold-first-ever fetches against a brand-new edge complete in low single-digit seconds. Sub-second is met for every read after the first one to a given edge, per file.

### 7.3 Consumer pattern

A server-side caller in any language:
1. On boot or cache expiry, GET `manifest.json` once. Parse `snapshot_ref` and the `commodity_slugs` map.
2. On lookup, GET the per-county-crop file at `@{snapshot_ref}` (immutable, gets aggressive jsDelivr edge cache). Parse JSON. Cache in process memory by URL (TTL ~1 hour for the manifest; effectively infinite for SHA-pinned shards).
3. Index the in-memory dict by `(source, util_practice, unit, year)` to satisfy lookups instantly.

The application server's local cache turns repeat lookups into nanoseconds; jsDelivr's edge cache turns first-ever-on-this-edge lookups into tens of milliseconds.

## 8. Failure-Mode Table

| Scenario | What breaks | What the consumer sees | Mitigation |
|---|---|---|---|
| NASS bulk file is missing on a given day | Date-probe walks the 14-day window and exits non-zero | Last successful snapshot stays live; `manifest.refreshed_at` lags | Workflow runs `gh issue create`. Healthchecks alerts on 3-business-day streak. Manual `workflow_dispatch` retries on demand. |
| NASS schema change: new column added | Tolerant reader builds name→index map, succeeds; new column logged in `header_observed` | No effect | Manifest carries forensic record. Future refresh keeps working. |
| NASS schema change: depended-on column renamed/removed | Tolerant reader detects missing required column; aborts | Last successful snapshot stays live | Refresh exits non-zero with the missing column named. Email + issue + healthchecks fire. Manual diagnosis required. |
| Suppression code (D), (NA), (Z) in `VALUE` | Routed to `suppressed.{year}` | Consumer sees `value=null` and `suppressed.{year} = "D"` | Documented; consumer chooses to ignore, surface, or fall back. |
| Unparseable VALUE (whitespace + comma + symbol) | Whitespace and commas normalized; if cast still fails, routed to `raw.{year}` with original string | Consumer sees raw string preserved | Forensic audit possible without breaking the pipeline. |
| Unit variation across years for same commodity | Each `(class, util, prodn, unit, source)` tuple is a separate series | n/a | Consumer picks the unit they want. |
| Connecticut FIPS reorganized (planning regions) | Cache reflects whatever NASS publishes. Per-state CT manifest enumerates current codes. | Consumer's hard-coded legacy `09001` may not resolve once NASS transitions | Document caveat in README; recommend resolving county codes from manifest, not hard-coding. |
| Cron silently skipped by GitHub | No publish that day | `manifest.refreshed_at` lags | Healthchecks 2-day grace; alerts on 3-business-day streak; workflow_dispatch is one-click recovery. |
| Workflow auto-disabled after 60 days inactivity | Cron stops firing entirely | Same as above | Healthchecks alert. Manual re-enable on the workflow page. Recovery procedure documented in README. |
| Rate limit imposed by jsDelivr | Reads start 4xx-ing | App-side errors | jsDelivr's free tier covers tens of thousands/day inside published norms. R2-with-domain upgrade path is the documented escalation. |
| Consumer in high-latency region | Cold-miss reads are slow | Slow first lookup per `(edge, file)` pair | jsDelivr's CF + Fastly edges cover most populated regions. Server-side caching collapses the long tail. SHA-pinning + 1-year immutable cache compounds the win on warm hits. |
| Previously-published value revised by NASS | Old SHA snapshot has stale value; new SHA snapshot has corrected value | `@main`-followed consumers converge within 12 h. SHA-pinning consumers see the new value when they next read manifest. | Documented behavior. SHA-pinning consumers wanting bit-stable answers don't update their manifest cache until they want to. |
| Shard removed in a snapshot (county loses coverage) | Refresh runs `git rm` on absent files | Consumer GET to old URL returns 404 | Per-state manifest reflects current shards. README documents always resolving from manifest, never assuming a URL is stable across snapshots. |
| Tag force-moved | Snapshot tags should be immutable but tags are conventions | Consumer pinned to a tag could observe content drift | Tag protection rule prevents `snapshot-*` tags from being force-moved. Manifest's `snapshot_ref` (SHA) is the canonical immutable reference; tag is informational. |
| Slug collision (two distinct COMMODITY_DESC → same slug) | Validator catches it before publish | n/a (pre-publish) | Refresh exits non-zero with collision details; `gh issue create` fires. Manual disambiguation rule added. |
| Bulk file > 14 GB Actions runner disk | Download fails or post-decompress staging fails | Last successful snapshot stays live | Streaming pipeline never holds the whole file in memory; staging output is ~250 MB. The 14 GB ceiling is currently ~5x our usage. Monitor `df -h` in workflow logs; alert if usage exceeds 80%. |
| jsDelivr changes terms / disappears | Reads start failing | App-side errors | Switch consumers to `raw.githubusercontent.com` (rate-limited but functional) or migrate to R2 upgrade path. Data is also `git clone`-able by anyone. |

## 9. Verification Plan

1. **Unit tests** (Python, fixture-based): tolerant header reader; suppression-code routing; whitespace/comma normalization; raw-value escape; row-level filter; group key uniqueness; slug function and collision detection; per-state file-count delta validator; bootstrap-mode validator path.
2. **Integration test**: synthetic 50-row TSV at `tests/fixtures/sample.tsv` → full pipeline → expected JSON tree at `tests/fixtures/expected/`. Asserts byte-equal output.
3. **End-to-end ground-truth**: pick five canonical lookups (Iowa Story corn 2023 SURVEY, Iowa Story corn 2017 CENSUS, Texas Lubbock cotton 2023, Vermont Lamoille corn silage 2023, plus one CT county post-2024 to verify FIPS scheme). Cross-check each against the live NASS Quick Stats web app and assert numeric match.
4. **Suppression behavior**: identify a county-crop-year known to be `(D)`-suppressed. Verify it lands in `suppressed`, not `values`.
5. **Connecticut bootstrap**: after the first refresh fetches a 2024 ANNUAL SURVEY row, document in the README which FIPS codes NASS uses for CT. Update the per-state manifest description.
6. **Refresh latency**: time the full refresh end-to-end on the GitHub runner. Target under 30 minutes; alert if it exceeds 60 minutes.
7. **Read latency**: publish the cache, then `curl -w "%{time_total}s\n"` from a Linux VM in an unrelated region against ten random per-county-crop URLs both cold (force a fresh edge) and warm. Assert warm p95 < 200 ms, cold p95 < 3 s.
8. **Failure injection**: simulate (a) corrupted bulk file with a renamed depended-on column, (b) NASS file gap > 14 days, (c) missing healthchecks ping. Confirm cache stays live, alerts fire, GitHub issue opens.
9. **Long-term durability check**: separate `.github/workflows/verify.yml` running daily fetches the published `manifest.json`, asserts `refreshed_at` within last 7 business days, and pings a second healthchecks URL on success. Independent timer; not a hard guarantee against simultaneous GitHub-side incidents but reduces single-cron-failure blast radius.
10. **Slug collision regression**: deliberate test with two synthetic commodities that would collide under naïve slugging; verify the disambiguation produces deterministic suffixes.
11. **Row deletion regression**: synthetic prior snapshot with file `data/v1/states/19/counties/169/zucchini.json`; new staging without it; verify the refresh runs `git rm` and the file is gone after publish.

## File and Infrastructure Inventory

This is a new repository. Files to create:

- `.github/workflows/refresh.yml` — business-daily M-F cron + manual dispatch, with explicit `permissions:` block (`contents: write`, `issues: write`, `pull-requests: write`) and `concurrency:` lock.
- `.github/workflows/verify.yml` — daily freshness check; pings second healthchecks URL on success.
- `.github/workflows/e2e.yml` — `workflow_dispatch:` plus `pull_request: paths: [scripts/refresh.py, scripts/validate.py, scripts/discover_url.py]`. Runs full pipeline against real NASS only on ETL-touching PRs.
- `.github/tag-protection.md` — runbook for setting up tag protection rule on `snapshot-*` (one-time manual repo config).
- `scripts/refresh.py` — orchestrator: discover → download → parse → group → validate → emit → publish → ping. Python stdlib only (`gzip`, `csv`, `urllib`, `json`, `pathlib`, `subprocess`, `re`, `hashlib`).
- `scripts/discover_url.py` — date-probe URL discovery. Reads/writes `.refresh-state.json`.
- `scripts/parser.py` — tolerant header reader; row filter; suppression-code routing; whitespace/comma normalization.
- `scripts/grouper.py` — group + slug + collision detection.
- `scripts/emitter.py` — write to staging; diff-detect; `git rm` absent files.
- `scripts/validate.py` — schema check; row-count bands; per-state delta; baseline-lookup spot-checks; bootstrap-mode handling.
- `scripts/manifest.py` — generate top-level + per-state manifests; populate `snapshot_ref` post-commit.
- `data/v1/` — published JSON tree (committed by workflow, not by hand).
- `.refresh-state.json` — internal state: `last_successful_date`, `last_url`, `last_etag`, `last_modified`, `last_filtered_row_count`, `last_per_state_counts`, `sample_lookups` (10 baseline (state, county, commodity, source, year, value) tuples for next-run validation).
- `tests/fixtures/sample.tsv` — 50-row hand-crafted bulk fragment.
- `tests/fixtures/expected/` — expected JSON tree from the fixture.
- `tests/test_*.py` — unit + integration tests.
- `README.md` — consumer docs: URL pattern, two-step pinning flow, schema, caveats (CT, suppression, source preference, recovery from 60-day disable).
- `LICENSE` — Apache-2.0 or MIT for the scripts. Data is public-domain U.S. government.
- `TODOS.md` — deferred work (see [Section 10](#10-todos)).

Repository secrets:
- `HEALTHCHECKS_PING_URL` — refresh deadman.
- `HEALTHCHECKS_VERIFY_URL` — daily verify deadman.

One-time repo configuration (manual, post-creation):
- Tag protection rule: prevent force-move of `snapshot-*` tags.
- Branch protection on `main`: do **not** require PR review (workflow needs to push directly), but enable status-check requirements for the verify workflow.

Domain ownership not required.

## 10. TODOs

`TODOS.md` to be created at repo root with this entry:

```markdown
## v2: extend ingestion to non-crop yields

**What**: extend refresh.py to also download and process `qs.animals_products_*.txt.gz`, emitting per-county-commodity JSON for milk-per-cow, honey-per-colony, aquaculture yields, and other animal-product yields with the same shape.

**Why**: the v1 cache is "NASS county CROP yields" — it explicitly excludes a non-trivial slice of NASS's county yield data that consumers may eventually want. Adding it later is a small extension because the architecture already generalizes.

**Pros of doing it**:
- "NASS county yields" matches what consumers expect from the product name.
- Data shape already supports it (commodity name doesn't care if it's CORN or MILK).
- Architecture is unchanged; just one more bulk file in the refresh pipeline.

**Cons / cost**:
- ~462 MB additional bulk download per refresh.
- ~30% more output JSON (animal yields are sparser per county than crops but cover different metrics).
- Extra ingestion path means one more failure surface during refresh.

**Context for the future**:
- Bulk URL: `https://www.nass.usda.gov/datasets/qs.animals_products_YYYYMMDD.txt.gz`.
- Same `qs.<sector>_YYYYMMDD.txt.gz` URL pattern as crops; date-probe logic already handles it.
- Filter rules likely identical: `AGG_LEVEL_DESC = COUNTY`, `STATISTICCAT_DESC = YIELD`, `DOMAIN_DESC = TOTAL`, etc. **Verify with a small head-sample before committing.**
- Output goes into the same `data/v1/states/{fips}/counties/{code}/{commodity_slug}.json` tree; new commodity slugs (`milk`, `honey`, etc.) appear automatically.
- Once v2 lands, rename product to "NASS county yields" in manifest and README.

**Depends on / blocked by**: nothing. v1 must ship first to validate the architecture.
```

## 11. NOT in scope (for v1)

- **Non-crop yields** (animals_products, etc.) — see TODOS.md.
- **Quick Stats fields** beyond yield (e.g., production, area harvested, price received). Could be added as parallel statistics in the same shard files, but not for v1.
- **Custom domain / R2 migration**. Documented as upgrade path; not v1.
- **Consumer client libraries** (TypeScript, Python, Go SDKs). Consumers integrate via plain `fetch` + `JSON.parse`. SDKs come if a real consumer asks.
- **Realtime updates / webhooks**. Cache cadence is business-daily; no push notifications. If a consumer needs realtime, they should use the NASS Quick Stats API directly.
- **Data revision history beyond the latest snapshot**. Consumers who want historical revisions can pin specific `snapshot-YYYY-MM-DD` SHAs and walk the tag history. Building a revision-history API on top of the cache is out of scope.
- **Connecticut FIPS aliasing layer** that maps legacy `09001` to new planning-region codes. The cache reflects what NASS publishes; consumer-side mapping is consumer-side concern.
- **Authentication or API keys**. Reads are unauthenticated public.

## 12. What already exists (and is reused, not rebuilt)

- **NASS bulk file** — free, public, primary-source. Reused as-is, never reformatted upstream.
- **jsDelivr CDN** — free, ToS-clean for open-source data. Reused as front-end caching layer with one-year immutable cache for SHA-pinned content.
- **GitHub Actions** — free for public repos. Reused as the cron + ETL runner. Native `schedule:` cron, `workflow_dispatch:`, `concurrency:`, `permissions:` all stdlib.
- **Python stdlib `gzip` + `csv` + `urllib` + `pathlib` + `json`** — zero-dep streaming ETL. No external libraries needed.
- **healthchecks.io** — free deadman switch. Industry-standard cron monitoring.
- **GitHub repo as durable storage substrate** — git-deduplicated blob storage, `git clone` as fallback retrieval, tag-based snapshots. Used outside its primary intent (code) but well within its capabilities at our shard sizes.

## 13. Worktree parallelization strategy

Implementation steps and modules:

| Step | Modules touched | Depends on |
|------|-----------------|------------|
| Discover URL + state file | `scripts/discover_url.py`, `tests/test_discover.py` | — |
| Parser (header, filter, suppression) | `scripts/parser.py`, `tests/fixtures/sample.tsv`, `tests/test_parser.py` | — |
| Grouper + slug | `scripts/grouper.py`, `tests/test_grouper.py` | parser |
| Emitter (staging, diff, git rm) | `scripts/emitter.py`, `tests/test_emitter.py` | grouper |
| Validator (schema, deltas, baselines) | `scripts/validate.py`, `tests/test_validator.py` | parser, grouper |
| Manifest generator | `scripts/manifest.py`, `tests/test_manifest.py` | grouper |
| Refresh orchestrator | `scripts/refresh.py`, `tests/test_integration.py` | all of the above |
| Workflow YAML + secrets | `.github/workflows/*.yml` | refresh |
| README + LICENSE + TODOS | `README.md`, `LICENSE`, `TODOS.md` | (mostly) refresh |

Parallel lanes:

- **Lane A**: discover_url + tests (independent).
- **Lane B**: parser + fixtures + tests (independent).
- **Lane C** (after B): grouper + slug + tests.
- **Lane D** (after B and C): validator.
- **Lane E** (after C): emitter, manifest.
- **Lane F** (after A + B + C + D + E): refresh.py orchestrator + integration tests.
- **Lane G** (after F): workflow YAML, README, LICENSE, TODOS.

**Execution order**: launch A and B in parallel. Once B lands, launch C and (in parallel) the documentation skeleton. Once C lands, launch D and E in parallel. Once all of A, B, C, D, E land, F. Then G.

**Conflict flags**: Lanes C, D, E all touch `tests/fixtures/expected/` if implemented sloppily. Coordinate via a shared fixture-shape spec early — or have each lane add its own fixture file rather than mutating a shared one.

## 14. Open questions for the user before implementation

1. **Repo name and owner** — under which GitHub account does the cache repo live? (e.g. `usda-nass-county-yields`, `nass-yields-cache`, etc.)
2. ~~Commodity scope~~ — resolved: every commodity NASS publishes at county-yield level appears automatically (data-driven slug generation).
3. ~~Census of Ag handling~~ — resolved: include CENSUS as separate series alongside SURVEY (D7).
4. ~~R2 upgrade timing~~ — resolved: jsDelivr is v1; R2 is documented upgrade path if a domain becomes available.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | not run |
| Codex Review | `/codex review` | Independent 2nd opinion | 1 | issues_found | 26 raw findings, 17 folded inline as plan fixes, 4 surfaced as cross-model tension (D6–D9) |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 9 issues found, 9 resolved (D1–D9), 0 critical gaps, full coverage diagram produced |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | not run (no UI surface) |
| DX Review | `/plan-devex-review` | Developer experience | 0 | — | not run |

- **CODEX**: outside-voice review caught real misses including a wrong jsDelivr file-cap figure (20 MB, not 50 MB), a circular last_run_at.txt mechanism, missing DOMAIN_DESC filter, slug-collision risk, row-deletion gap, and the cross-shard cache-coherency hole at `@main`. All folded into the plan.
- **CROSS-MODEL**: D6 (manifest pinning), D7 (CENSUS scope), D8 (cadence), D9 (v2 scope) reflect tension Claude review missed and Codex caught. User resolved each explicitly.
- **UNRESOLVED**: 0.
- **VERDICT**: ENG CLEARED — ready to implement when user approves repo name/owner.

---

## Completion Summary

- **Step 0: Scope challenge** — Greenfield; 9 files, 0 services, 0 new classes. No complexity-reduction trigger. Two Layer-1 opportunities flagged (date-probe vs HTML scrape; tolerant reader).
- **Architecture review** — 4 issues. D1 (URL discovery → date-probe + .last_url forward-walking), D2 (drop premature force-push squash), plus inline folds (ETag persistence, CT FIPS docs-only).
- **Code quality review** — 1 issue. D3 (tolerant reader + forensic header logging).
- **Test review** — coverage diagram produced (30 paths, all greenfield gaps to be filled by initial implementation), D4 (path-filtered E2E + gh issue create on validation failure), regression tests defined for slug collision and row deletion.
- **Performance review** — 0 gating issues; concerns addressed by ETag short-circuit and streaming pipeline already in design.
- **Outside voice (Codex)** — ran successfully, returned 26 substantive findings; 17 folded inline (e.g., explicit Action permissions, 14 GB runner-disk ceiling, DOMAIN_DESC filter, slug collision, row deletion, bootstrap mode, per-state delta validation, baseline-row sanity checks); 4 surfaced as user-gated tensions (D6–D9); rest were re-litigations of D1–D4 already accepted.
- **NOT in scope** — Section 11 written.
- **What already exists** — Section 12 written.
- **TODOs** — Section 10 written; one v2 entry deferred.
- **Failure modes** — table updated with 14 new scenarios; 0 critical gaps flagged.
- **Parallelization** — 7 lanes, 2-pair early parallelization (A+B), then dependency-driven cascade.
- **Lake Score** — 8/9 user decisions chose the more complete option (D5 outside voice run, D6 SHA pinning, D7 CENSUS included, D8 daily cadence, etc.); D2 chose simpler-now to avoid premature optimization.

## Unresolved decisions

None. All 9 AskUserQuestions answered with explicit user choice; all Codex findings either folded inline or resolved through D6–D9.
