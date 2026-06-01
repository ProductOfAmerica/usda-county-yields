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
from dataclasses import dataclass, field
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
    # Grouped state price tree (state_fips -> {state, crops}), so SP-C derived
    # can join prices without re-parsing. Default keeps older constructors valid.
    price_states: dict = field(default_factory=dict)


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


def _slug(commodity: str) -> str:
    return refresh.slugify(commodity)


def group_prices(kept: list[dict]) -> dict:
    """Nest kept rows: state_fips -> {state, crops: {slug: {commodity_desc,
    series[]}}}. One series per (class, period); values keyed by YYYY (marketing
    year) or YYYY-MM (monthly). Suppressed values land in a parallel suppressed
    map. Duplicate (year[-month]) keys are last-write-wins (NASS revision in the
    snapshot), matching refresh.group_by_state's values-dict semantics.
    """
    states: dict = {}
    for r in kept:
        fips = r["state_fips"]
        slug = _slug(r["commodity"])
        st = states.setdefault(fips, {
            "state": {"fips": fips, "alpha": r["state_alpha"], "name": r["state_name"]},
            "crops": {},
        })
        com = st["crops"].setdefault(slug, {"commodity_desc": r["commodity"], "series": []})
        key = (r["class"], r["period"])
        series = next(
            (s for s in com["series"] if (s["class"], s["period"]) == key), None)
        if series is None:
            series = {"class": r["class"], "period": r["period"], "unit": PRICE_UNIT,
                      "values": {}, "suppressed": {}}
            com["series"].append(series)
        if r["period"] == "MONTHLY":
            vkey = f"{r['year']}-{MONTH_NUM[r['ref']]}"
        else:
            vkey = r["year"]
        value, code, _raw = refresh.parse_value(r["value"])
        if value is not None:
            series["values"][vkey] = value
            series["suppressed"].pop(vkey, None)
        elif code is not None:
            series["suppressed"][vkey] = code
            series["values"].pop(vkey, None)
    return states


def _series_sort_key(s: dict) -> tuple:
    return (s["class"], s["period"])


def sort_price_series(states: dict) -> None:
    """Sort each crop's series[] by (class, period) so shard bytes stay stable
    across NASS row reorders (mirrors refresh.sort_series)."""
    for st in states.values():
        for com in st["crops"].values():
            com["series"].sort(key=_series_sort_key)


def mark_price_canonical(states: dict) -> tuple[int, list[tuple[str, str]]]:
    """Mark the ALL CLASSES marketing-year series canonical per (state, crop).

    Returns (missing_count, samples): (state, crop) pairs that have price series
    but no ALL CLASSES marketing-year series. (class, period) is a unique
    grouping key, so at most one series matches; no ambiguity is possible.
    """
    missing = 0
    samples: list[tuple[str, str]] = []
    for fips, st in states.items():
        for slug, com in st["crops"].items():
            match = next(
                (s for s in com["series"]
                 if s["class"] == CANONICAL_PRICE_CLASS
                 and s["period"] == CANONICAL_PRICE_PERIOD),
                None)
            if match is not None:
                match["canonical"] = True
            elif com["series"]:
                missing += 1
                if len(samples) < 10:
                    samples.append((fips, slug))
    return missing, samples


def _assert_price_shape(shard: dict) -> None:
    """Stdlib structural check matching data/_schema/price.json. Raises
    SystemExit on drift so a producer regression fails fast (no jsonschema dep).
    """
    top = {"schema_version", "state", "commodity", "series"}
    if set(shard) != top:
        raise SystemExit(f"Price shard top-level keys mismatch: {sorted(set(shard))}")
    if shard["schema_version"] != 3:
        raise SystemExit(f"Price shard schema_version not 3: {shard['schema_version']!r}")
    required = {"class", "period", "unit", "values", "suppressed"}
    optional = {"canonical"}
    for s in shard["series"]:
        keys = set(s)
        if required - keys:
            raise SystemExit(f"Price series missing keys: {sorted(required - keys)}")
        if keys - required - optional:
            raise SystemExit(f"Price series unexpected keys: {sorted(keys - required - optional)}")


# ---------- paths ----------

def _data_dir() -> Path:
    return refresh.DATA_DIR


def _shard_path(fips: str, slug: str) -> Path:
    return _data_dir() / "prices" / "states" / fips / f"{slug}.json"


def _audit_path() -> Path:
    return _data_dir() / "_audit" / "prices.json"


def _schema_path() -> Path:
    return _data_dir() / "_schema" / "price.json"


def prices_bootstrap_needed() -> bool:
    """True when the price audit sentinel is absent (drives same-publication
    re-emit). Mirrors refresh.sp_a_bootstrap_needed."""
    return not _audit_path().exists()


# ---------- emit ----------

def emit_all(states: dict, discovery: dict, refreshed_at: str) -> set:
    """Write per-(state, crop) shards + audit. The audit is written
    UNCONDITIONALLY (even with zero shards) so the bootstrap sentinel always
    clears after a run. Returns the protected path set (schema + shards +
    audit) so refresh.prune_stale does not delete them.
    """
    paths: set = {_schema_path()}
    shard_count = 0
    for fips in sorted(states):
        st = states[fips]
        for slug in sorted(st["crops"]):
            com = st["crops"][slug]
            shard = {
                "schema_version": 3,
                "state": st["state"],
                "commodity": {"slug": slug, "desc": com["commodity_desc"]},
                "series": com["series"],
            }
            _assert_price_shape(shard)
            p = _shard_path(fips, slug)
            refresh.write_if_changed(p, refresh._dump_json(shard))
            paths.add(p)
            shard_count += 1
    audit = {
        "product_name": "NASS state prices received",
        "refreshed_at": refreshed_at,
        "source": {"url": discovery["url"], "etag": discovery["etag"],
                   "publication_date": discovery["date"]},
        "unit": PRICE_UNIT,
        "periods": ["MARKETING YEAR", "MONTHLY"],
        "shard_count": shard_count,
    }
    ap = _audit_path()
    refresh.write_if_changed(ap, refresh._dump_json(audit))
    paths.add(ap)
    return paths


def _validate_band(kept: int, baseline: Optional[int]) -> None:
    """Per-family Gate 2: +/-10% band vs prior price-row count. Bootstrap-
    tolerant (baseline None) and zero-tolerant (baseline 0): a legitimately
    empty price family is a valid published state, per spec 4.7's zero-shard
    invariant.
    """
    if baseline is None or baseline == 0:
        return
    delta = abs(kept - baseline) / baseline
    if delta > refresh.ROW_COUNT_TOLERANCE:
        raise SystemExit(
            f"SP-B price row count {kept} differs from baseline {baseline} by "
            f"{delta:.1%} (>{refresh.ROW_COUNT_TOLERANCE:.0%}). Aborting.")


def run_prices(download_path: Path, discovery: dict, refreshed_at: str,
               baseline: Optional[int]) -> PriceRunResult:
    """SP-B entrypoint, called from refresh.main() after the shared download."""
    with gzip.open(download_path, "rt", encoding="utf-8", newline="") as f:
        total, kept = filter_prices(csv.reader(f, delimiter="\t"))
    if total == 0:
        raise SystemExit("SP-B: bulk file produced 0 rows. Aborting.")
    _validate_band(len(kept), baseline)
    states = group_prices(kept)
    sort_price_series(states)
    missing, samples = mark_price_canonical(states)
    if missing:
        s = ", ".join(f"{f}/{c}" for f, c in samples)
        print(f"SP-B prices: {missing} (state, crop) pairs lack a canonical "
              f"marketing-year price. First {len(samples)}: {s}", file=sys.stderr)
    paths = emit_all(states, discovery, refreshed_at)
    shard_count = sum(len(st["crops"]) for st in states.values())
    print(f"SP-B prices: kept={len(kept)} shards={shard_count}", file=sys.stderr)
    return PriceRunResult(paths=paths, shard_count=shard_count,
                          kept_count=len(kept), price_states=states)
