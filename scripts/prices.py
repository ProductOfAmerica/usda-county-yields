#!/usr/bin/env python3
"""SP-B: NASS state price-received producer.

Second pass over the same qs.crops_*.txt.gz the yields refresh downloads.
Filters STATE-level SURVEY PRICE RECEIVED rows in $ / BU for corn/soybeans/
wheat, groups per (state, crop) into a marketing-year series and a monthly
series (one of each per class), marks the ALL CLASSES marketing-year series
canonical, and emits sharded JSON + schema + audit. Stdlib only. See spec
section 4.4.

Two period shapes are kept: ANNUAL/MARKETING YEAR (the recap-grade price,
keyed by year) and MONTHLY/<month token> (keyed by YYYY-MM). Calendar-year
annual prices (ANNUAL/YEAR) and the rare MONTHLY/MARKETING YEAR rows are
deliberately excluded; the audit records the kept shapes.
"""
from __future__ import annotations

import csv
import gzip
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import refresh  # lazy-safe: refresh imports this module only inside main()

PRICE_UNIT = "$ / BU"
MONTH_NUM = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05", "JUN": "06",
    "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
}
# Canonical price series per (state, crop): the classless aggregate marketing
# year. Verified live: the wheat aggregate is class "ALL CLASSES" (the token
# "WHEAT" has zero rows); corn/soy carry only "ALL CLASSES".
CANONICAL_PRICE_CLASS = "ALL CLASSES"
CANONICAL_PRICE_PERIOD = "MARKETING YEAR"

REQUIRED_PRICE_COLS = [
    "SOURCE_DESC", "COMMODITY_DESC", "CLASS_DESC", "STATISTICCAT_DESC",
    "UNIT_DESC", "AGG_LEVEL_DESC", "STATE_FIPS_CODE", "STATE_ALPHA",
    "STATE_NAME", "YEAR", "FREQ_DESC", "REFERENCE_PERIOD_DESC", "VALUE",
]


@dataclass(frozen=True)
class PriceRunResult:
    paths: set
    shard_count: int
    kept_count: int


def _kept_period(freq: str, ref: str) -> Optional[str]:
    """Canonical period label for a kept row, or None to drop.

    Two shapes kept: ANNUAL/MARKETING YEAR and MONTHLY/<month token>.
    """
    if freq == "ANNUAL" and ref == "MARKETING YEAR":
        return "MARKETING YEAR"
    if freq == "MONTHLY" and ref in MONTH_NUM:
        return "MONTHLY"
    return None


def filter_prices(reader: Iterable[list[str]]) -> tuple[int, list[dict]]:
    """Second-pass filter over the bulk gz csv reader. Returns (total, kept).

    Raises SystemExit on missing required columns (Gate 1). Strict equality on
    STATISTICCAT_DESC == "PRICE RECEIVED" cleanly excludes the AFTER REPORT,
    PRIOR TO CLOSING, PARITY, and 10-YEAR-AVG variants.
    """
    header = next(iter(reader))
    col = {name: i for i, name in enumerate(header)}
    missing = [c for c in REQUIRED_PRICE_COLS if c not in col]
    if missing:
        raise SystemExit(f"Required columns missing from NASS bulk file (PRICES): {missing}")
    total = 0
    kept: list[dict] = []
    for row in reader:
        total += 1
        try:
            if (row[col["SOURCE_DESC"]] != "SURVEY"
                    or row[col["STATISTICCAT_DESC"]] != "PRICE RECEIVED"
                    or row[col["UNIT_DESC"]] != PRICE_UNIT
                    or row[col["AGG_LEVEL_DESC"]] != "STATE"
                    or row[col["COMMODITY_DESC"]] not in refresh.COMMODITY_ALLOWLIST):
                continue
            period = _kept_period(row[col["FREQ_DESC"]], row[col["REFERENCE_PERIOD_DESC"]])
            if period is None:
                continue
            kept.append({
                "period": period,
                "commodity": row[col["COMMODITY_DESC"]],
                "class": row[col["CLASS_DESC"]],
                "state_fips": row[col["STATE_FIPS_CODE"]].zfill(2),
                "state_alpha": row[col["STATE_ALPHA"]],
                "state_name": row[col["STATE_NAME"]],
                "year": row[col["YEAR"]],
                "ref": row[col["REFERENCE_PERIOD_DESC"]],
                "value": row[col["VALUE"]],
            })
        except IndexError:
            continue
    return total, kept
