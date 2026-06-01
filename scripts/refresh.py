#!/usr/bin/env python3
"""NASS county crop yields refresh.

Downloads the latest qs.crops_*.txt.gz, filters to county SURVEY rows for
allowlisted commodities across five statistics (yield, production, area
harvested, area planted, area planted net), emits a v3 sharded JSON tree
(index, per-state meta, per-(county, crop) multi-statistic point leaves,
per-(state, crop) yield-only rollups, audit) under data/, and prunes leaves
no longer in the current refresh.

Validation gates: required columns present (Gate 1); filtered leaf-row count
within +/-10% of last successful run, per-family baseline, skipped on
bootstrap (Gate 2); missing-canonical-YIELD ratio under tolerance (Gate 3).
Inline hard guards: slug collision; per-leaf prune of stale files; ambiguous
canonical rule (more than one series matching a (crop, statistic) rule
aborts).
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
DATA_DIR = REPO_ROOT / "data"
STATE_FILE = REPO_ROOT / ".refresh-state.json"
NASS_BASE = "https://www.nass.usda.gov/datasets"
SECTOR = "crops"

COMMODITY_ALLOWLIST = {"CORN", "SOYBEANS", "WHEAT"}
STATISTIC_ALLOWLIST = {
    "YIELD", "PRODUCTION", "AREA HARVESTED", "AREA PLANTED", "AREA PLANTED, NET",
}

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

REQUIRED_COLS = [
    "SOURCE_DESC", "COMMODITY_DESC", "CLASS_DESC",
    "PRODN_PRACTICE_DESC", "UTIL_PRACTICE_DESC",
    "STATISTICCAT_DESC", "UNIT_DESC", "SHORT_DESC",
    "DOMAIN_DESC", "DOMAINCAT_DESC", "AGG_LEVEL_DESC",
    "STATE_FIPS_CODE", "STATE_ALPHA", "STATE_NAME",
    "COUNTY_CODE", "COUNTY_ANSI", "COUNTY_NAME",
    "YEAR", "FREQ_DESC", "REFERENCE_PERIOD_DESC", "VALUE", "CV_%",
]

PROBE_WINDOW_DAYS = 14
ROW_COUNT_TOLERANCE = 0.10
CANONICAL_MISSING_TOLERANCE = 0.05  # Gate 3 ratio ceiling for missing-canonical pairs
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


def is_caught_up(last_known: Optional[date], today: date) -> bool:
    """True if we already processed today's (or later) NASS publication.

    Distinguishes the benign "nothing to do" case from the alert-worthy
    "NASS missing for 14 days" case so a same-day workflow_dispatch
    rerun returns exit 0 instead of exit 1.
    """
    return last_known is not None and last_known >= today


def discover(last_known: Optional[date], today: date, inclusive: bool = False) -> Optional[dict]:
    """Find the most recent NASS bulk file URL within the probe window.

    Walks newest-first within [max(last_known + offset, today - 14), today],
    where offset is 1 day normally (skip the already-processed publication) or
    0 days when inclusive=True. Inclusive mode is used during bootstrap re-emit:
    when local artifacts are missing but NASS has not published anything newer,
    we must re-find the already-processed file (same date, same ETag) and
    re-emit from it. Returns dict with
    date/url/etag/last_modified/content_length/lag_days, or None if no file
    exists in the window.
    """
    earliest = today - timedelta(days=PROBE_WINDOW_DAYS)
    if last_known:
        offset = timedelta(days=0) if inclusive else timedelta(days=1)
        earliest = max(earliest, last_known + offset)
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
                or row[col_idx["STATISTICCAT_DESC"]] not in STATISTIC_ALLOWLIST
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
        # Tripwire: .zfill pads but does not truncate. NASS publishing a
        # malformed code would otherwise propagate into a malformed file
        # path and silently corrupt the tree.
        if not (len(fips) == 2 and fips.isdigit()):
            raise SystemExit(f"Malformed STATE_FIPS_CODE: {fips!r}")
        if not (len(county_code) == 3 and county_code.isdigit()):
            raise SystemExit(f"Malformed COUNTY_CODE: {county_code!r}")
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

        year = row["YEAR"]
        value, code, raw_str = parse_value(row["VALUE"])
        if value is not None:
            series["values"][year] = value
        elif code is not None:
            series["suppressed"][year] = code
        elif raw_str is not None:
            series["raw"][year] = raw_str

        # CV is metadata on a value; NASS publishes it alongside an estimate,
        # so cv[year] is assumed (not enforced) to imply a values[year].
        cv_value, _cv_code, _cv_raw = parse_value(row["CV_%"])
        if cv_value is not None:
            series["cv"][year] = cv_value
    return states


# ---------- io helpers ----------

def write_if_changed(path: Path, text: str) -> bool:
    """Atomic content-diff write. Returns True if a write happened.

    Single source of truth for the touched-only-write semantic shared by
    every emitter. Skipping no-op writes keeps weekly git diffs
    proportional to actual data change, not refresh count.
    """
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


# ---------- emit ----------

# Sort key per series, used by sort_series and prefixed by canonical match
# in mark_canonical. Matching the same tuple shape as group_by_state's
# series_key keeps the producer round-trip deterministic.
def _series_sort_key(s: dict) -> tuple:
    return (s["statistic"], s["class"], s["prodn_practice"], s["util_practice"], s["unit"], s["short_desc"])


def sort_series(states: dict[str, dict]) -> None:
    """Sort each commodity's series[] in place by canonical key.

    group_by_state appends in source-row encounter order. Without this
    sort, an upstream NASS row reorder rewrites every leaf despite no
    data change. Codex review #8.
    """
    for st in states.values():
        for cty in st["counties"].values():
            for com in cty["commodities"].values():
                com["series"].sort(key=_series_sort_key)


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


def _state_path(fips: str) -> Path:
    return DATA_DIR / "states" / fips


def _point_leaf_path(fips: str, county_code: str, slug: str) -> Path:
    return _state_path(fips) / "counties" / county_code / f"{slug}.json"


def _crop_rollup_path(fips: str, slug: str) -> Path:
    return _state_path(fips) / "crops" / f"{slug}.json"


def _state_meta_path(fips: str) -> Path:
    return _state_path(fips) / "meta.json"


def _index_path() -> Path:
    return DATA_DIR / "index.json"


def _audit_path() -> Path:
    return DATA_DIR / "_audit" / "latest.json"


def _sp_a_audit_path() -> Path:
    return DATA_DIR / "_audit" / "planting-windows.json"


def sp_a_bootstrap_needed() -> bool:
    """True when SP-A artifacts are absent and need a same-ETag bootstrap."""
    return not _sp_a_audit_path().exists()


def _dump_json(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def emit_index(
    states: dict[str, dict],
    discovery: dict,
    refreshed_at: str,
) -> tuple[Path, bool]:
    """Write data/index.json. Returns (path, written_bool)."""
    state_index = {}
    for fips in sorted(states):
        st = states[fips]
        crops = sorted({slug for cty in st["counties"].values() for slug in cty["commodities"]})
        state_index[fips] = {
            "alpha": st["state"]["alpha"],
            "name": st["state"]["name"],
            "crops": crops,
            "county_count": len(st["counties"]),
        }
    payload = {
        "schema_version": 3,
        "product_name": "NASS county crop yields",
        "refreshed_at": refreshed_at,
        "source": {
            "url": discovery["url"],
            "last_modified": discovery["last_modified"],
            "etag": discovery["etag"],
            "publication_date": discovery["date"],
            "freshness_lag_days": discovery["lag_days"],
        },
        "states": state_index,
    }
    path = _index_path()
    return path, write_if_changed(path, _dump_json(payload))


def emit_state_meta(states: dict[str, dict]) -> tuple[set[Path], int]:
    """Write data/states/{fips}/meta.json per state. Returns (paths_written_to_set, written_count)."""
    paths: set[Path] = set()
    written = 0
    for fips in sorted(states):
        st = states[fips]
        counties = {}
        for code in sorted(st["counties"]):
            cty = st["counties"][code]
            counties[code] = {
                "name": cty["name"],
                "crops": sorted(cty["commodities"]),
            }
        payload = {
            "schema_version": 3,
            "state": st["state"],
            "counties": counties,
        }
        path = _state_meta_path(fips)
        paths.add(path)
        if write_if_changed(path, _dump_json(payload)):
            written += 1
    return paths, written


def emit_point_leaves(states: dict[str, dict]) -> tuple[set[Path], int]:
    """Write data/states/{fips}/counties/{code}/{slug}.json per (county, crop)."""
    paths: set[Path] = set()
    written = 0
    for fips in sorted(states):
        st = states[fips]
        for code in sorted(st["counties"]):
            cty = st["counties"][code]
            for slug in sorted(cty["commodities"]):
                com = cty["commodities"][slug]
                payload = {
                    "schema_version": 3,
                    "state": st["state"],
                    "county": {"code": code, "name": cty["name"]},
                    "commodity": {"slug": slug, "desc": com["commodity_desc"]},
                    "series": com["series"],
                }
                _assert_leaf_shape(payload)
                path = _point_leaf_path(fips, code, slug)
                paths.add(path)
                if write_if_changed(path, _dump_json(payload)):
                    written += 1
    return paths, written


def emit_crop_rollups(states: dict[str, dict]) -> tuple[set[Path], int]:
    """Write data/states/{fips}/crops/{slug}.json per (state, crop)."""
    paths: set[Path] = set()
    written = 0
    for fips in sorted(states):
        st = states[fips]
        # Group counties by crop slug for this state.
        per_crop: dict[str, dict[str, dict]] = {}
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
        for slug, bundle in per_crop.items():
            payload = {
                "schema_version": 3,
                "state": st["state"],
                "commodity": {"slug": slug, "desc": bundle["desc"]},
                "counties": bundle["counties"],
            }
            path = _crop_rollup_path(fips, slug)
            paths.add(path)
            if write_if_changed(path, _dump_json(payload)):
                written += 1
    return paths, written


def emit_audit(
    header_observed: list[str],
    refreshed_at: str,
    source_publication_date: str,
) -> tuple[Path, bool]:
    """Write data/_audit/latest.json (maintainer audit, deduped header)."""
    payload = {
        "schema_version": 3,
        "refreshed_at": refreshed_at,
        "source_publication_date": source_publication_date,
        "header_observed": header_observed,
    }
    path = _audit_path()
    return path, write_if_changed(path, _dump_json(payload))


def prune_stale(expected_files: set[Path]) -> int:
    """Delete any .json under data/ that is not in expected_files.

    Inline guard: a state (or county, or crop) no longer present in the
    current refresh tree must be removed so the next git commit captures
    the deletion.
    """
    deleted = 0
    if not DATA_DIR.exists():
        return 0
    for existing in DATA_DIR.rglob("*.json"):
        if existing not in expected_files:
            existing.unlink()
            deleted += 1
    return deleted


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


def validate_canonical_coverage(missing_count: int, total_pairs: int) -> None:
    """Gate 3: abort if too many (county, crop) pairs lack a canonical YIELD.

    A spike means NASS structurally dropped the canonical YIELD variant for a
    crop, which would silently degrade every consumer point lookup. Empirical
    floor across published data is ~0.3%; 5% gives ~16x headroom for real drift
    while still catching a structural regression.
    """
    if total_pairs == 0:
        return  # validate() above already aborts on zero-rows upstream
    ratio = missing_count / total_pairs
    if ratio > CANONICAL_MISSING_TOLERANCE:
        raise SystemExit(
            f"Missing-canonical ratio {ratio:.1%} exceeds tolerance "
            f"{CANONICAL_MISSING_TOLERANCE:.0%} ({missing_count}/{total_pairs} "
            f"pairs lack a canonical series). Aborting."
        )


def _assert_leaf_shape(leaf: dict) -> None:
    """Stdlib structural check matching data/_schema/leaf.json.

    Runs at emit time and in tests. Raises SystemExit on shape drift so
    a producer regression fails fast at the workflow level instead of
    silently corrupting the CDN. Stdlib-only (no jsonschema dep) keeps
    the project's zero-deps stance.
    """
    expected_top = {"schema_version", "state", "county", "commodity", "series"}
    if set(leaf) != expected_top:
        raise SystemExit(
            f"Leaf top-level keys mismatch: got {sorted(set(leaf))}, "
            f"expected {sorted(expected_top)}"
        )
    if leaf["schema_version"] != 3:
        raise SystemExit(f"Leaf schema_version not 3: {leaf['schema_version']!r}")
    if set(leaf["state"]) != {"fips", "alpha", "name"}:
        raise SystemExit(f"Leaf state keys mismatch: {sorted(set(leaf['state']))}")
    if set(leaf["county"]) != {"code", "name"}:
        raise SystemExit(f"Leaf county keys mismatch: {sorted(set(leaf['county']))}")
    if set(leaf["commodity"]) != {"slug", "desc"}:
        raise SystemExit(f"Leaf commodity keys mismatch: {sorted(set(leaf['commodity']))}")
    required_series = {
        "statistic", "class", "prodn_practice", "util_practice", "unit", "short_desc",
        "values", "cv", "suppressed", "raw",
    }
    optional_series = {"canonical"}
    for s in leaf["series"]:
        keys = set(s)
        missing = required_series - keys
        if missing:
            raise SystemExit(f"Series missing keys: {sorted(missing)}")
        extra = keys - required_series - optional_series
        if extra:
            raise SystemExit(f"Series has unexpected keys: {sorted(extra)}")


# ---------- state file ----------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


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

    # Bootstrap guard, computed BEFORE the early returns: if data/index.json or
    # any family audit sentinel is missing, force a re-emit even when we are
    # caught up or the source ETag matches the last run. Without computing this
    # first, the is_caught_up / ETag-match shortcuts would return before the
    # re-emit path, and a same-publication redeploy (after a schema bump or a
    # new family landing) would never materialize the absent artifacts.
    # Foundation's sentinel set is index + SP-A planting-windows; later phases
    # add their own family audits here when those emitters exist.
    bootstrap_needed = (
        not _index_path().exists()
        or sp_a_bootstrap_needed()
        or _prices_bootstrap_needed()
    )

    if is_caught_up(last_known, today) and not bootstrap_needed:
        print(f"Already caught up (last_known={last_known} >= today={today}); nothing to do.")
        ping_healthchecks()
        return 0
    # In bootstrap mode, probe inclusively so discover re-finds the
    # already-processed publication (earliest = last_known, not last_known + 1).
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

    print("Streaming + filtering...")
    header, total_rows, kept_rows = stream_filter(download_path)
    print(f"Total rows: {total_rows}; kept: {len(kept_rows)}")

    validate(total_rows, len(kept_rows), leaf_baseline(state))

    print("Grouping by state...")
    states = group_by_state(kept_rows)

    refreshed_at = utc_now_iso()

    # Emit pipeline. Sort first so leaf bytes stay stable across NASS row
    # reorders, then mark canonical, then emit each artifact family, then
    # prune stale leaves so removed (state, county, crop) tuples disappear
    # from the published tree.
    sort_series(states)
    missing_canonical, missing_samples = mark_canonical(states)
    if missing_canonical:
        sample_str = ", ".join(f"{f}/{c}/{s}" for f, c, s in missing_samples)
        print(
            f"WARN: {missing_canonical} (county, crop) pairs lack a canonical series. "
            f"First {len(missing_samples)}: {sample_str}",
            file=sys.stderr,
        )
    total_pairs = sum(
        len(cty["commodities"])
        for st in states.values()
        for cty in st["counties"].values()
    )
    validate_canonical_coverage(missing_canonical, total_pairs)

    expected: set[Path] = set()
    idx_path, idx_w = emit_index(states, discovery, refreshed_at)
    expected.add(idx_path)
    meta_paths, meta_w = emit_state_meta(states)
    expected |= meta_paths
    leaf_paths, leaf_w = emit_point_leaves(states)
    expected |= leaf_paths
    rollup_paths, rollup_w = emit_crop_rollups(states)
    expected |= rollup_paths
    audit_path, audit_w = emit_audit(header, refreshed_at, discovery["date"])
    expected.add(audit_path)
    # SP-A: second pass over the same downloaded gz. Must run before the
    # global prune and contribute its paths to `expected` so prune_stale
    # does not delete the planting-window tree.
    import planting_windows  # lazy: avoids a circular import at module load
    sp_a = planting_windows.run_planting_windows(
        download_path, discovery, refreshed_at
    )
    expected |= sp_a.paths
    # SP-B: state price-received second pass. Same contract as SP-A: its paths
    # join `expected` before the global prune so the price tree survives.
    import prices  # lazy: avoids a circular import at module load
    price_result = prices.run_prices(
        download_path, discovery, refreshed_at, family_baseline(state, "prices")
    )
    expected |= price_result.paths
    deleted = prune_stale(expected)
    print(
        f"emit: index={int(idx_w)} meta={meta_w} leaves={leaf_w} "
        f"rollups={rollup_w} audit={int(audit_w)} pruned={deleted} "
        f"missing_canonical={missing_canonical}"
    )

    save_state({
        "last_successful_date": discovery["date"],
        "last_url": discovery["url"],
        "last_etag": discovery["etag"],
        "last_modified": discovery["last_modified"],
        "last_filtered_row_count": {"leaf": len(kept_rows), "prices": price_result.kept_count},
        "last_total_row_count": total_rows,
        "last_run_at": refreshed_at,
        "last_missing_canonical_count": missing_canonical,
        "last_missing_canonical_at": refreshed_at,
        "last_sp_a_shard_count": sp_a.shard_count,
        "last_price_shard_count": price_result.shard_count,
    })

    ping_healthchecks()
    return 0


if __name__ == "__main__":
    sys.exit(main())
