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
