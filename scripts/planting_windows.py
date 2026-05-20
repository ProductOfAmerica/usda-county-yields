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
