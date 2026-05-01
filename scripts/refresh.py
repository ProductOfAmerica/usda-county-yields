#!/usr/bin/env python3
"""NASS county crop yields refresh.

Downloads the latest qs.crops_*.txt.gz, filters to county SURVEY yields for
allowlisted commodities, emits one JSON per state, prunes absent files.

Two validation gates: required columns present + filtered row count within
+/-10% of last successful run (skipped on bootstrap).
Two inline guards: slug collision check; git rm absent files.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "v1" / "states"
STATE_FILE = REPO_ROOT / ".refresh-state.json"
NASS_BASE = "https://www.nass.usda.gov/datasets"
SECTOR = "crops"

COMMODITY_ALLOWLIST = {"CORN", "SOYBEANS", "WHEAT"}

REQUIRED_COLS = [
    "SOURCE_DESC", "COMMODITY_DESC", "CLASS_DESC",
    "PRODN_PRACTICE_DESC", "UTIL_PRACTICE_DESC",
    "STATISTICCAT_DESC", "UNIT_DESC", "SHORT_DESC",
    "DOMAIN_DESC", "DOMAINCAT_DESC", "AGG_LEVEL_DESC",
    "STATE_FIPS_CODE", "STATE_ALPHA", "STATE_NAME",
    "COUNTY_CODE", "COUNTY_ANSI", "COUNTY_NAME",
    "YEAR", "FREQ_DESC", "REFERENCE_PERIOD_DESC", "VALUE",
]

PROBE_WINDOW_DAYS = 14
ROW_COUNT_TOLERANCE = 0.10
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_BACKOFF_SECONDS = (30, 120)  # waits before attempts 2 and 3
SUPPRESSION_RE = re.compile(r"^\(([A-Z]+)\)$")
SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")
SLUG_DASHES = re.compile(r"-+")


# ---------- date probe ----------

def url_for_date(d: date) -> str:
    return f"{NASS_BASE}/qs.{SECTOR}_{d.strftime('%Y%m%d')}.txt.gz"


def head_request(url: str) -> Optional[dict]:
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {
                "status": resp.status,
                "etag": resp.headers.get("ETag"),
                "last_modified": resp.headers.get("Last-Modified"),
                "content_length": int(resp.headers.get("Content-Length", "0")),
            }
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def discover(last_known: Optional[date], today: date) -> Optional[dict]:
    """Find the most recent NASS bulk file URL within the probe window.

    Walks newest-first within [max(last_known + 1, today - 14), today].
    Returns dict with date/url/etag/last_modified/content_length/lag_days,
    or None if no fresh file exists in the window.
    """
    earliest = today - timedelta(days=PROBE_WINDOW_DAYS)
    if last_known:
        earliest = max(earliest, last_known + timedelta(days=1))
    if earliest > today:
        return None
    d = today
    while d >= earliest:
        url = url_for_date(d)
        head = head_request(url)
        if head and head["status"] == 200:
            return {
                "date": d.isoformat(),
                "url": url,
                "etag": head["etag"],
                "last_modified": head["last_modified"],
                "content_length": head["content_length"],
                "lag_days": (today - d).days,
            }
        d -= timedelta(days=1)
    return None


# ---------- value parsing ----------

def slugify(name: str) -> str:
    s = SLUG_NON_ALNUM.sub("-", name.lower())
    return SLUG_DASHES.sub("-", s).strip("-")


def parse_value(raw: str) -> tuple[Optional[float], Optional[str], Optional[str]]:
    """Return (numeric, suppression_code, raw_string).

    Routes parenthesized codes to suppression. Strips whitespace and
    thousand-separator commas, then casts. Falls back to raw string for
    anything else.
    """
    s = raw.strip()
    if not s:
        return None, None, None
    m = SUPPRESSION_RE.match(s)
    if m:
        return None, m.group(1), None
    try:
        return float(s.replace(",", "")), None, None
    except ValueError:
        return None, None, s


# ---------- streaming filter ----------

def _parse_filter(reader: Iterable[list[str]]) -> tuple[list[str], int, list[dict]]:
    """Pull rows out of a csv reader, apply the row-level filter.

    Raises SystemExit on missing required columns (Gate 1).
    """
    header = next(iter(reader))
    col_idx = {name: i for i, name in enumerate(header)}
    missing = [c for c in REQUIRED_COLS if c not in col_idx]
    if missing:
        raise SystemExit(f"Required columns missing from NASS bulk file: {missing}")
    keep_cols = REQUIRED_COLS

    total = 0
    kept: list[dict] = []
    for row in reader:
        total += 1
        try:
            if (row[col_idx["AGG_LEVEL_DESC"]] != "COUNTY"
                or row[col_idx["STATISTICCAT_DESC"]] != "YIELD"
                or row[col_idx["FREQ_DESC"]] != "ANNUAL"
                or row[col_idx["REFERENCE_PERIOD_DESC"]] != "YEAR"
                or row[col_idx["DOMAIN_DESC"]] != "TOTAL"
                or row[col_idx["DOMAINCAT_DESC"]] != "NOT SPECIFIED"
                or row[col_idx["SOURCE_DESC"]] != "SURVEY"
                or row[col_idx["COMMODITY_DESC"]] not in COMMODITY_ALLOWLIST):
                continue
            kept.append({k: row[col_idx[k]] for k in keep_cols})
        except IndexError:
            continue
    return header, total, kept


def stream_filter(gz_path: Path) -> tuple[list[str], int, list[dict]]:
    with gzip.open(gz_path, "rt", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        return _parse_filter(reader)


# ---------- group ----------

def group_by_state(rows: list[dict]) -> dict[str, dict]:
    """Group filtered rows into per-state nested structure.

    Raises SystemExit on slug collision (inline guard 1).
    """
    states: dict[str, dict] = {}
    seen_slugs: dict[str, str] = {}  # slug -> commodity_desc, for collision detection

    for row in rows:
        fips = row["STATE_FIPS_CODE"].zfill(2)
        county_code = row["COUNTY_CODE"].zfill(3)
        commodity = row["COMMODITY_DESC"]
        slug = slugify(commodity)
        if slug in seen_slugs and seen_slugs[slug] != commodity:
            raise SystemExit(
                f"Slug collision: {slug!r} maps to both "
                f"{seen_slugs[slug]!r} and {commodity!r}"
            )
        seen_slugs[slug] = commodity

        st = states.setdefault(fips, {
            "state": {
                "fips": fips,
                "alpha": row["STATE_ALPHA"],
                "name": row["STATE_NAME"],
            },
            "counties": {},
        })
        cty = st["counties"].setdefault(county_code, {
            "name": row["COUNTY_NAME"],
            "ansi": row["COUNTY_ANSI"],
            "commodities": {},
        })
        com = cty["commodities"].setdefault(slug, {
            "commodity_desc": commodity,
            "series": [],
        })
        series_key = (
            row["CLASS_DESC"],
            row["PRODN_PRACTICE_DESC"],
            row["UTIL_PRACTICE_DESC"],
            row["UNIT_DESC"],
            row["SHORT_DESC"],
        )
        series = next(
            (s for s in com["series"]
             if (s["class"], s["prodn_practice"], s["util_practice"], s["unit"], s["short_desc"]) == series_key),
            None,
        )
        if series is None:
            series = {
                "class": row["CLASS_DESC"],
                "prodn_practice": row["PRODN_PRACTICE_DESC"],
                "util_practice": row["UTIL_PRACTICE_DESC"],
                "unit": row["UNIT_DESC"],
                "short_desc": row["SHORT_DESC"],
                "values": {},
                "suppressed": {},
                "raw": {},
            }
            com["series"].append(series)

        year = row["YEAR"]
        value, code, raw_str = parse_value(row["VALUE"])
        if value is not None:
            series["values"][year] = value
        elif code is not None:
            series["suppressed"][year] = code
        elif raw_str is not None:
            series["raw"][year] = raw_str
    return states


# ---------- emit ----------

def emit_state_files(
    states: dict[str, dict],
    discovery: dict,
    header_observed: list[str],
    refreshed_at: str,
) -> tuple[int, int]:
    """Write per-state JSON; git rm absent files. Returns (written, deleted)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    new_files: set[Path] = set()
    for fips, payload in sorted(states.items()):
        out_path = DATA_DIR / f"{fips}.json"
        envelope = {
            "schema_version": 1,
            "product_name": "NASS county crop yields (v1)",
            "refreshed_at": refreshed_at,
            "source": {
                "url": discovery["url"],
                "last_modified": discovery["last_modified"],
                "etag": discovery["etag"],
                "publication_date": discovery["date"],
                "freshness_lag_days": discovery["lag_days"],
            },
            "header_observed": header_observed,
            **payload,
        }
        new_text = json.dumps(envelope, indent=2, sort_keys=True) + "\n"
        new_files.add(out_path)
        if not out_path.exists() or out_path.read_text(encoding="utf-8") != new_text:
            out_path.write_text(new_text, encoding="utf-8")
            written += 1

    # Inline guard 2: prune absent files
    deleted = 0
    for existing in DATA_DIR.glob("*.json"):
        if existing not in new_files:
            existing.unlink()
            deleted += 1
    return written, deleted


# ---------- validate ----------

def validate(total_rows: int, kept_rows: int, last_filtered_count: Optional[int]) -> None:
    """Gate 2: row-count sanity + +/-10% band vs prior run (bootstrap-tolerant)."""
    if total_rows == 0:
        raise SystemExit("Bulk file produced 0 rows total. Aborting.")
    if kept_rows == 0:
        raise SystemExit("After filtering, 0 rows remained. Aborting.")
    if last_filtered_count is None:
        return  # bootstrap
    delta = abs(kept_rows - last_filtered_count) / last_filtered_count
    if delta > ROW_COUNT_TOLERANCE:
        raise SystemExit(
            f"Filtered row count {kept_rows} differs from baseline "
            f"{last_filtered_count} by {delta:.1%} (>{ROW_COUNT_TOLERANCE:.0%}). "
            "Aborting."
        )


# ---------- state file ----------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# ---------- download ----------

def download_with_retry(url: str, dest: Path) -> None:
    # Single bad packet during a 1 GB download shouldn't page healthchecks.
    last_exc = None
    for attempt in range(DOWNLOAD_ATTEMPTS):
        try:
            urllib.request.urlretrieve(url, dest)
            return
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_exc = e
            print(f"Download {attempt + 1}/{DOWNLOAD_ATTEMPTS} failed: {e}", file=sys.stderr)
            if attempt + 1 < DOWNLOAD_ATTEMPTS:
                time.sleep(DOWNLOAD_BACKOFF_SECONDS[attempt])
    raise SystemExit(f"Download failed after {DOWNLOAD_ATTEMPTS} attempts: {last_exc}")


# ---------- healthchecks ----------

def ping_healthchecks() -> None:
    url = os.environ.get("HEALTHCHECKS_PING_URL")
    if not url:
        return
    try:
        urllib.request.urlopen(url, timeout=10).read()
    except Exception as e:
        print(f"WARN: healthchecks ping failed: {e}", file=sys.stderr)


# ---------- entrypoint ----------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def main(today: Optional[date] = None) -> int:
    today = today or date.today()
    state = load_state()
    last_known = (
        date.fromisoformat(state["last_successful_date"])
        if state.get("last_successful_date") else None
    )
    last_etag = state.get("last_etag")

    print(f"Last known publication: {last_known}; today: {today}")
    discovery = discover(last_known, today)
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

    if last_etag and discovery["etag"] == last_etag:
        print("ETag matches last successful run; nothing to do.")
        ping_healthchecks()
        return 0

    download_path = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / Path(discovery["url"]).name
    print(f"Downloading {discovery['url']} -> {download_path}")
    download_with_retry(discovery["url"], download_path)

    print("Streaming + filtering...")
    header, total_rows, kept_rows = stream_filter(download_path)
    print(f"Total rows: {total_rows}; kept: {len(kept_rows)}")

    validate(total_rows, len(kept_rows), state.get("last_filtered_row_count"))

    print("Grouping by state...")
    states = group_by_state(kept_rows)

    refreshed_at = utc_now_iso()
    written, deleted = emit_state_files(states, discovery, header, refreshed_at)
    print(f"Wrote {written} state files; deleted {deleted}")

    save_state({
        "last_successful_date": discovery["date"],
        "last_url": discovery["url"],
        "last_etag": discovery["etag"],
        "last_modified": discovery["last_modified"],
        "last_filtered_row_count": len(kept_rows),
        "last_total_row_count": total_rows,
        "last_run_at": refreshed_at,
    })

    ping_healthchecks()
    return 0


if __name__ == "__main__":
    sys.exit(main())
