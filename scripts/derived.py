#!/usr/bin/env python3
"""SP-C: NASS derived families.

Pure-Python derivations computed at emit time from the in-memory structures a
refresh already built: the county leaves (canonical marked by
refresh.mark_canonical) and the state price tree (from prices.run_prices). No
re-parse of the bulk file. Emits two sharded families plus an audit:

  data/derived/{fips}/counties/{code}/{slug}.json   per-county revenue + trend + rank
  data/states/{fips}/derived/state-{slug}.json      prod-weighted yield + county scan

See spec sections 4.5 (families), 4.6 (marketing-year join), 4.7 (safety).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import refresh  # lazy-safe: refresh imports this module only inside main()

# Marketing-year label offset added to the yield year, per crop (spec 4.6).
# Identity for all three crops here; stated explicitly so a future spring or
# southern-hemisphere commodity does not blindly reuse it.
MARKETING_YEAR_LABEL = {"corn": 0, "soybeans": 0, "wheat": 0}

TRAILING_MIN_5 = 3    # min present years in a 5-year window to emit an average
TRAILING_MIN_10 = 5   # min present years in a 10-year window


@dataclass(frozen=True)
class DerivedRunResult:
    paths: set
    shard_count: int
    kept_count: int


def _canonical(com: dict, statistic: str) -> Optional[dict]:
    """The canonical series for a statistic within one commodity, or None."""
    return next((s for s in com["series"]
                 if s.get("canonical") and s["statistic"] == statistic), None)


def _canonical_price(price_states: dict, fips: str, slug: str) -> Optional[dict]:
    """The canonical (ALL CLASSES marketing-year) price series, or None."""
    st = price_states.get(fips)
    if not st:
        return None
    com = st["crops"].get(slug)
    if not com:
        return None
    return next((s for s in com["series"] if s.get("canonical")), None)


def _marketing_year(slug: str, yield_year: str) -> Optional[str]:
    off = MARKETING_YEAR_LABEL.get(slug)
    if off is None:
        return None
    return str(int(yield_year) + off)


def compute_revenue(states: dict, price_states: dict) -> dict:
    """{(fips, code, slug): {year: revenue_record}}.

    per-harvested = yield*price; per-planted = production*price/area_planted
    (optional, only where production+area_planted present and planted>0). Emits
    a year only where yield and the joined marketing-year price both exist.
    """
    out: dict = {}
    for fips, st in states.items():
        for code, cty in st["counties"].items():
            for slug, com in cty["commodities"].items():
                yld = _canonical(com, "YIELD")
                if yld is None:
                    continue
                price = _canonical_price(price_states, fips, slug)
                if price is None:
                    continue
                prod = _canonical(com, "PRODUCTION")
                planted = _canonical(com, "AREA PLANTED")
                recs: dict = {}
                for year, yv in yld["values"].items():
                    my = _marketing_year(slug, year)
                    if my is None or my not in price["values"]:
                        continue
                    pv = price["values"][my]
                    rec = {
                        "marketing_year": my,
                        "yield": yv,
                        "price": pv,
                        "revenue_per_harvested_acre": round(yv * pv, 4),
                    }
                    if prod is not None and planted is not None:
                        pa = planted["values"].get(year)
                        pq = prod["values"].get(year)
                        if pa and pa > 0 and pq is not None:
                            rec["revenue_per_planted_acre"] = round(pq * pv / pa, 4)
                    recs[year] = rec
                if recs:
                    out[(fips, code, slug)] = recs
    return out


def _rank_one(value: float, all_values: list[float]) -> tuple[int, int, float]:
    """Competition rank (1=highest, ties share), count, percentile.

    rank = 1 + count strictly greater. percentile = (n - rank)/(n - 1), or 1.0
    when n == 1.
    """
    n = len(all_values)
    rank = 1 + sum(1 for v in all_values if v > value)
    pct = 1.0 if n == 1 else round((n - rank) / (n - 1), 4)
    return rank, n, pct


def compute_ranks(states: dict) -> dict:
    """{(fips, code, slug): {year: rank_record}} on canonical YIELD.

    rank/percentile within-state and within-nation, per (crop, year).
    """
    # Gather canonical yield values: by (slug, year) -> nation list; and
    # (fips, slug, year) -> state list; remember each county's own value.
    nation: dict = {}                 # (slug, year) -> [values]
    state_pool: dict = {}             # (fips, slug, year) -> [values]
    own: dict = {}                    # (fips, code, slug) -> {year: value}
    for fips, st in states.items():
        for code, cty in st["counties"].items():
            for slug, com in cty["commodities"].items():
                yld = _canonical(com, "YIELD")
                if yld is None:
                    continue
                for year, v in yld["values"].items():
                    nation.setdefault((slug, year), []).append(v)
                    state_pool.setdefault((fips, slug, year), []).append(v)
                    own.setdefault((fips, code, slug), {})[year] = v
    out: dict = {}
    for (fips, code, slug), years in own.items():
        recs: dict = {}
        for year, v in years.items():
            sr, sn, sp = _rank_one(v, state_pool[(fips, slug, year)])
            nr, nn, npc = _rank_one(v, nation[(slug, year)])
            recs[year] = {
                "rank_in_state": sr, "count_in_state": sn, "percentile_in_state": sp,
                "rank_in_nation": nr, "count_in_nation": nn, "percentile_in_nation": npc,
            }
        out[(fips, code, slug)] = recs
    return out


def compute_weighted_yield(states: dict) -> dict:
    """{(fips, slug): {"state": {year: bu/ac}, "national": {year: bu/ac}}}.

    sum(production)/sum(area harvested) over counties where both canonical
    values exist for the year. National repeats across every state's file.
    """
    # accumulate (prod_sum, area_sum) per (slug, year) nationally and per
    # (fips, slug, year) by state
    nat: dict = {}     # (slug, year) -> [prod, area]
    bystate: dict = {} # (fips, slug, year) -> [prod, area]
    slugs_by_state: dict = {}  # fips -> set(slug)
    for fips, st in states.items():
        for cty in st["counties"].values():
            for slug, com in cty["commodities"].items():
                prod = _canonical(com, "PRODUCTION")
                area = _canonical(com, "AREA HARVESTED")
                if prod is None or area is None:
                    continue
                for year, pv in prod["values"].items():
                    av = area["values"].get(year)
                    if av is None or av == 0:
                        continue
                    n = nat.setdefault((slug, year), [0.0, 0.0]); n[0] += pv; n[1] += av
                    b = bystate.setdefault((fips, slug, year), [0.0, 0.0]); b[0] += pv; b[1] += av
                    slugs_by_state.setdefault(fips, set()).add(slug)
    nat_yield = {k: round(v[0] / v[1], 4) for k, v in nat.items() if v[1] > 0}
    out: dict = {}
    for fips, slugs in slugs_by_state.items():
        for slug in slugs:
            state_y = {year: round(b[0] / b[1], 4)
                       for (f, s, year), b in bystate.items()
                       if f == fips and s == slug and b[1] > 0}
            national_y = {year: nat_yield[(slug, year)]
                          for (s, year) in nat_yield if s == slug}
            out[(fips, slug)] = {"state": state_y, "national": national_y}
    return out


def _slope(points: list[tuple[int, float]]) -> Optional[float]:
    """OLS slope of y over x; None with < 2 distinct x."""
    xs = [x for x, _ in points]
    if len(set(xs)) < 2:
        return None
    n = len(points)
    mx = sum(xs) / n
    my = sum(y for _, y in points) / n
    num = sum((x - mx) * (y - my) for x, y in points)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return round(num / den, 4)


def compute_yield_stats(states: dict) -> dict:
    """{(fips, code, slug): {slope_bu_per_year?, yoy_pct{}, trailing_5yr_avg{}, trailing_10yr_avg{}}}
    on canonical YIELD. Suppressed years are simply absent from values."""
    out: dict = {}
    for fips, st in states.items():
        for code, cty in st["counties"].items():
            for slug, com in cty["commodities"].items():
                yld = _canonical(com, "YIELD")
                if yld is None:
                    continue
                vals = {int(y): v for y, v in yld["values"].items()}
                years = sorted(vals)
                stats: dict = {"yoy_pct": {}, "trailing_5yr_avg": {}, "trailing_10yr_avg": {}}
                slope = _slope([(y, vals[y]) for y in years])
                if slope is not None:
                    stats["slope_bu_per_year"] = slope
                for y in years:
                    if (y - 1) in vals and vals[y - 1] != 0:
                        stats["yoy_pct"][str(y)] = round((vals[y] - vals[y - 1]) / vals[y - 1] * 100, 2)
                    w5 = [vals[k] for k in range(y - 4, y + 1) if k in vals]
                    if len(w5) >= TRAILING_MIN_5:
                        stats["trailing_5yr_avg"][str(y)] = round(sum(w5) / len(w5), 2)
                    w10 = [vals[k] for k in range(y - 9, y + 1) if k in vals]
                    if len(w10) >= TRAILING_MIN_10:
                        stats["trailing_10yr_avg"][str(y)] = round(sum(w10) / len(w10), 2)
                out[(fips, code, slug)] = stats
    return out
