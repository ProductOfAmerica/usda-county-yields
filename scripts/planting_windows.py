#!/usr/bin/env python3
"""SP-A: NASS Crop Progress usual planting/harvesting window producer.

Second pass over the same qs.crops_*.txt.gz the yields refresh already
downloads. Filters STATE-level SURVEY PROGRESS rows, reconstructs NASS's
documented percentile window (begin 5%, most-active 15-85%, end 95%,
20-year basis) per (state, crop), and emits sharded JSON + schema +
audit + coverage. Stdlib only. See FIE-41 spec.
"""
from __future__ import annotations

import csv
import gzip
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Optional

import refresh  # lazy-safe: refresh.py imports this module only inside main()

THRESHOLDS = (5.0, 15.0, 85.0, 95.0)
THRESHOLD_KEYS = ("begin", "mostActiveStart", "mostActiveEnd", "end")
MIN_USABLE_YEARS = 20
WINTER_WHEAT_PLANT_ORDINAL_MIN = 0
WINTER_WHEAT_PLANT_ORDINAL_MAX = 260
PLAIN_DOY_MIN = 1
PLAIN_DOY_MAX = 366
REF_YEAR = 2001  # fixed non-leap reference year for ordinal -> MM-DD

# crop_slug -> (commodity_desc, class_desc or None)
CROP_FILTERS: dict[str, tuple[str, Optional[str]]] = {
    "corn": ("CORN", None),
    "soybeans": ("SOYBEANS", None),
    "winter-wheat": ("WHEAT", "WINTER"),
    "spring-wheat": ("WHEAT", "SPRING"),
}
UNIT_OP = {"PCT PLANTED": "plant", "PCT HARVESTED": "harvest"}

REQUIRED_PW_COLS = [
    "SOURCE_DESC", "COMMODITY_DESC", "CLASS_DESC",
    "STATISTICCAT_DESC", "UNIT_DESC", "AGG_LEVEL_DESC",
    "STATE_FIPS_CODE", "STATE_ALPHA", "STATE_NAME",
    "YEAR", "WEEK_ENDING", "VALUE",
]

_MMDD_RE = re.compile(r"^[0-9]{2}-[0-9]{2}$")


@dataclass(frozen=True)
class PlantingWindowRunResult:
    paths: set[Path]
    shard_count: int


def _data_dir() -> Path:
    # Indirect so tests can monkeypatch refresh.DATA_DIR (shared root).
    return refresh.DATA_DIR


def _assert_planting_window_shape(shard: dict) -> None:
    """Stdlib structural check matching data/_schema/planting-window.json.

    Raises SystemExit on drift so a producer regression fails fast instead
    of silently corrupting the CDN. No jsonschema dep (repo zero-deps).
    """
    top = {
        "stateFips", "stateAlpha", "crop", "plant", "harvest",
        "method", "definition", "sourceYears",
    }
    if set(shard) != top:
        raise SystemExit(
            f"Window shard top-level keys mismatch: got {sorted(shard)}, "
            f"expected {sorted(top)}"
        )
    if shard["method"] != "nass-crop-progress-percentile":
        raise SystemExit(f"Window shard bad method: {shard['method']!r}")
    if shard["definition"] != "usual-window":
        raise SystemExit(f"Window shard bad definition: {shard['definition']!r}")
    if shard["crop"] not in CROP_FILTERS:
        raise SystemExit(f"Window shard bad crop: {shard['crop']!r}")
    for blk in ("plant", "harvest"):
        b = shard[blk]
        if set(b) != set(THRESHOLD_KEYS):
            raise SystemExit(f"Window shard {blk} keys mismatch: {sorted(b)}")
        for k in THRESHOLD_KEYS:
            if not (isinstance(b[k], str) and _MMDD_RE.match(b[k])):
                raise SystemExit(
                    f"Window shard {blk}.{k} not MM-DD: {b[k]!r}"
                )
    sy = shard["sourceYears"]
    if set(sy) != {"from", "to"} or not all(isinstance(sy[k], int) for k in sy):
        raise SystemExit(f"Window shard sourceYears bad: {sy!r}")


def parse_pct(raw: str) -> Optional[float]:
    """Numeric percent, or None for blank/suppressed/non-numeric."""
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None  # "(D)", "(NA)", etc.; not a usable observation


def _slug_for(commodity: str, class_desc: str) -> Optional[str]:
    for slug, (com, cls) in CROP_FILTERS.items():
        if commodity == com and (cls is None or class_desc == cls):
            return slug
    return None


def filter_progress(reader: Iterable[list[str]]) -> tuple[int, list[dict]]:
    """Second-pass filter over the bulk gz csv reader.

    Returns (total_rows_scanned, kept) where kept rows are dicts with
    crop_slug + op resolved. Raises SystemExit on missing required
    columns (Gate 1, mirrors refresh._parse_filter).
    """
    header = next(iter(reader))
    col = {name: i for i, name in enumerate(header)}
    missing = [c for c in REQUIRED_PW_COLS if c not in col]
    if missing:
        raise SystemExit(
            f"Required columns missing from NASS bulk file (PROGRESS): {missing}"
        )
    total = 0
    kept: list[dict] = []
    for row in reader:
        total += 1
        try:
            if (
                row[col["SOURCE_DESC"]] != "SURVEY"
                or row[col["STATISTICCAT_DESC"]] != "PROGRESS"
                or row[col["AGG_LEVEL_DESC"]] != "STATE"
            ):
                continue
            unit = row[col["UNIT_DESC"]]
            op = UNIT_OP.get(unit)
            if op is None:
                continue
            slug = _slug_for(row[col["COMMODITY_DESC"]], row[col["CLASS_DESC"]])
            if slug is None:
                continue
            kept.append({
                "crop_slug": slug,
                "op": op,
                "state_fips": row[col["STATE_FIPS_CODE"]].zfill(2),
                "state_alpha": row[col["STATE_ALPHA"]],
                "state_name": row[col["STATE_NAME"]],
                "year": int(row[col["YEAR"]]),
                "week_ending": row[col["WEEK_ENDING"]].strip(),
                "value": row[col["VALUE"]],
            })
        except (IndexError, ValueError):
            continue
    return total, kept


def group_progress(kept: list[dict]) -> dict:
    """Nest filtered rows: (state_fips, slug) -> {op: {year: {readings}}}.

    Within (state, slug, op, year) readings are keyed by WEEK_ENDING in a
    dict so a duplicate week (NASS revision in the snapshot) is
    last-write-wins (mirrors refresh.group_by_state's values dict).
    Suppressed/blank values are dropped. `readings` is the sorted
    (week_ending, pct) list.
    """
    g: dict = {}
    for r in kept:
        key = (r["state_fips"], r["crop_slug"])
        node = g.setdefault(key, {
            "state_fips": r["state_fips"],
            "state_alpha": r["state_alpha"],
            "state_name": r["state_name"],
            "plant": {},
            "harvest": {},
        })
        pct = parse_pct(r["value"])
        if pct is None:
            continue
        year_map = node[r["op"]].setdefault(r["year"], {"_by_we": {}})
        year_map["_by_we"][r["week_ending"]] = pct
    for node in g.values():
        for op in ("plant", "harvest"):
            for ym in node[op].values():
                ym["readings"] = sorted(ym.pop("_by_we").items())
    return g


def _anchor(slug: str, op: str, year: int) -> Optional[date]:
    """Seasonal anchor; None means plain day-of-year within `year`."""
    if slug == "winter-wheat" and op == "plant":
        return date(year - 1, 8, 1)
    return None


def day_ordinal(slug: str, op: str, year: int, week_ending: str) -> Optional[int]:
    """Integer day-ordinal for a WEEK_ENDING date, or None if out of span.

    Plain crops/operations: 1-based day-of-year in calendar `year`.
    winter-wheat plant: days since Aug 1 of `year`-1 (forward; a January
    crossing maps to ~150, never wraps a calendar boundary).
    """
    y, m, d = (int(x) for x in week_ending.split("-"))
    we = date(y, m, d)
    anchor = _anchor(slug, op, year)
    if anchor is None:
        if we.year != year:
            return None
        ordn = (we - date(year, 1, 1)).days + 1
        if ordn < PLAIN_DOY_MIN or ordn > PLAIN_DOY_MAX:
            return None
    else:
        ordn = (we - anchor).days
        if (
            ordn < WINTER_WHEAT_PLANT_ORDINAL_MIN
            or ordn > WINTER_WHEAT_PLANT_ORDINAL_MAX
        ):
            return None
    return ordn


def year_crossings(
    slug: str, op: str, year: int, readings: list[tuple[str, float]]
) -> Optional[dict]:
    """Per-year fractional ordinal of each threshold crossing.

    Returns {begin,mostActiveStart,mostActiveEnd,end: float} or None if
    the year is not usable (non-monotone, first reading >5% i.e.
    left-censored, never reaches 95%, or an out-of-span ordinal).
    No synthetic point: NASS publishes a real leading 0%/1% reading, so
    every threshold straddles two real observations for a usable year.
    """
    if len(readings) < 2:
        return None
    pts: list[tuple[int, float]] = []
    last_pct = None
    for we, pct in readings:
        if last_pct is not None and pct < last_pct - 1e-9:
            return None
        last_pct = pct
        ordn = day_ordinal(slug, op, year, we)
        if ordn is None:
            return None
        pts.append((ordn, pct))
    pts.sort()
    if pts[0][1] > 5.0:
        return None
    if pts[-1][1] < 95.0:
        return None
    out: dict = {}
    for t, key in zip(THRESHOLDS, THRESHOLD_KEYS):
        crossing = None
        for i, (o, p) in enumerate(pts):
            if p == t:
                crossing = float(o)
                break
            if p > t:
                o0, p0 = pts[i - 1]
                frac = (t - p0) / (p - p0)
                crossing = o0 + frac * (o - o0)
                break
        if crossing is None:
            return None
        out[key] = crossing
    return out


def ordinal_to_mmdd(slug: str, op: str, ordn: float) -> str:
    """Convert a fractional ordinal back to MM-DD via a fixed calendar."""
    n = int(ordn + 0.5)  # round half up (ordinals are positive)
    anchor = _anchor(slug, op, REF_YEAR)
    if anchor is None:
        d = date(REF_YEAR, 1, 1) + timedelta(days=n - 1)
    else:
        d = anchor + timedelta(days=n)
    return f"{d.month:02d}-{d.day:02d}"


def derive_block(
    slug: str, op: str, year_map: dict
) -> Optional[tuple[dict, list[int]]]:
    """Reduce one operation's per-year series to a window block.

    Returns ({begin,...: 'MM-DD'}, used_years_sorted) or None if fewer
    than 20 usable years. Uses the most recent 20 usable years; per
    threshold takes statistics.median of the per-year fractional ordinals.
    """
    per_year: dict[int, dict] = {}
    for yr in sorted(year_map):
        cr = year_crossings(slug, op, yr, year_map[yr]["readings"])
        if cr is not None:
            per_year[yr] = cr
    usable = sorted(per_year)
    if len(usable) < MIN_USABLE_YEARS:
        return None
    used = usable[-MIN_USABLE_YEARS:]
    block = {}
    for key in THRESHOLD_KEYS:
        med = statistics.median(per_year[y][key] for y in used)
        block[key] = ordinal_to_mmdd(slug, op, med)
    return block, used


def _usable_year_count(slug: str, op: str, year_map: dict) -> int:
    return sum(
        1
        for y, payload in year_map.items()
        if year_crossings(slug, op, y, payload["readings"])
    )


def build_shards(g: dict) -> tuple[dict, dict]:
    """Build PRESENT shards and coverage for every candidate seen."""
    shards: dict = {}
    coverage: dict = {}
    for (fips, slug), node in g.items():
        p = derive_block(slug, "plant", node["plant"])
        h = derive_block(slug, "harvest", node["harvest"])
        p_n = _usable_year_count(slug, "plant", node["plant"])
        h_n = _usable_year_count(slug, "harvest", node["harvest"])
        if p is not None and h is not None:
            p_block, p_used = p
            h_block, h_used = h
            used = sorted(set(p_used) | set(h_used))
            shards[(fips, slug)] = {
                "stateFips": fips,
                "stateAlpha": node["state_alpha"],
                "crop": slug,
                "plant": p_block,
                "harvest": h_block,
                "method": "nass-crop-progress-percentile",
                "definition": "usual-window",
                "sourceYears": {"from": min(used), "to": max(used)},
            }
            coverage[(fips, slug)] = {
                "status": "PRESENT",
                "plant_usable": p_n,
                "harvest_usable": h_n,
                "years": used,
            }
        else:
            short = []
            if p is None:
                short.append(f"plant({p_n}<{MIN_USABLE_YEARS})")
            if h is None:
                short.append(f"harvest({h_n}<{MIN_USABLE_YEARS})")
            coverage[(fips, slug)] = {
                "status": "OMITTED",
                "reason": "insufficient usable years: " + ", ".join(short),
                "plant_usable": p_n,
                "harvest_usable": h_n,
            }
    return shards, coverage


def _shard_path(fips: str, slug: str) -> Path:
    return _data_dir() / "planting-windows" / fips / f"{slug}.json"


def _pw_audit_path() -> Path:
    return _data_dir() / "_audit" / "planting-windows.json"


def _coverage_path() -> Path:
    return _data_dir() / "_audit" / "window-coverage.json"


def _schema_path() -> Path:
    return _data_dir() / "_schema" / "planting-window.json"


def emit_all(
    shards: dict, coverage: dict, discovery: dict, refreshed_at: str
) -> set[Path]:
    """Write PRESENT shards + audit + coverage and return protected paths."""
    paths: set[Path] = {_schema_path()}
    for (fips, slug), shard in sorted(shards.items()):
        _assert_planting_window_shape(shard)
        p = _shard_path(fips, slug)
        refresh.write_if_changed(p, refresh._dump_json(shard))
        paths.add(p)
    audit = {
        "product_name": "NASS state usual planting/harvesting windows",
        "refreshed_at": refreshed_at,
        "source": {
            "url": discovery["url"],
            "etag": discovery["etag"],
            "publication_date": discovery["date"],
        },
        "method": "nass-crop-progress-percentile",
        "thresholds": [5, 15, 85, 95],
        "min_usable_years": MIN_USABLE_YEARS,
        "anchors": {
            "corn/plant": "day-of-year",
            "corn/harvest": "day-of-year",
            "soybeans/plant": "day-of-year",
            "soybeans/harvest": "day-of-year",
            "spring-wheat/plant": "day-of-year",
            "spring-wheat/harvest": "day-of-year",
            "winter-wheat/plant": "days-since-Aug-1-of-(YEAR-1)",
            "winter-wheat/harvest": "day-of-year",
        },
        "years_used": {
            f"{f}/{s}": cov["years"]
            for (f, s), cov in sorted(coverage.items())
            if cov["status"] == "PRESENT"
        },
    }
    ap = _pw_audit_path()
    refresh.write_if_changed(ap, refresh._dump_json(audit))
    paths.add(ap)
    cov_payload = {
        "product_name": "NASS planting-window coverage",
        "refreshed_at": refreshed_at,
        "candidates": {
            f"{f}/{s}": cov for (f, s), cov in sorted(coverage.items())
        },
    }
    cp = _coverage_path()
    refresh.write_if_changed(cp, refresh._dump_json(cov_payload))
    paths.add(cp)
    return paths
