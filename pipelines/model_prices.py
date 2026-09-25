from collections import defaultdict
from datetime import datetime, timezone
from statistics import median
from typing import Callable, List, Optional

from storage.activity_db import PARTS_MAKE


# upper bounds, in miles, of the bands sales are grouped into; above the last is '100k+'
MILEAGE_BANDS = [(1_000, 'under 1k'), (5_000, '1k-5k'), (10_000, '5k-10k'), (25_000, '10k-25k'),
                 (50_000, '25k-50k'), (100_000, '50k-100k')]
# a range of sales shorter than this is grouped by month, not quarter
MONTHLY_BELOW = 365 * 86400


def mileage_band(miles: Optional[int]) -> str:
    if miles is None:
        return 'unknown'
    for limit, name in MILEAGE_BANDS:
        if miles < limit:
            return name
    return '100k+'


def mileage_band_order(name: str) -> int:
    names = [n for _, n in MILEAGE_BANDS] + ['100k+', 'unknown']
    return names.index(name)


def sale_period(ts: int, monthly: bool) -> str:
    when = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{when.year}-{when.month:02d}" if monthly else f"{when.year} Q{(when.month - 1) // 3 + 1}"


def is_usd_sale(row) -> bool:
    return row['result'] == 'sold' and row['currency'] == 'USD' and row['high_bid'] is not None


def split_rows(rows: List[dict]) -> dict:
    """a model's fetched auctions: the cars summarized, the parts listings left out, and the cars' sales whose
    prices can't be summed in dollars (they still count as sold)"""
    cars = [r for r in rows if r['make'] != PARTS_MAKE]
    sold = [r for r in cars if r['result'] == 'sold']
    return {
        'cars': cars,
        'parts': len(rows) - len(cars),
        'other_currency': sum(1 for r in sold if r['high_bid'] is not None and r['currency'] not in (None, 'USD')),
        'no_price': sum(1 for r in sold if r['high_bid'] is None or r['currency'] is None),
    }


def summarize(rows: List[dict]) -> dict:
    """counts by result, sell-through (sold of those that sold or didn't meet the reserve) and usd sale prices"""
    sold = sum(1 for r in rows if r['result'] == 'sold')
    unsold = sum(1 for r in rows if r['result'] == 'reserve_not_met')
    prices = [r['high_bid'] for r in rows if is_usd_sale(r)]
    return {
        'auctions': len(rows), 'sold': sold, 'reserve_not_met': unsold,
        'withdrawn': sum(1 for r in rows if r['result'] == 'withdrawn'),
        'sell_through': sold / (sold + unsold) if sold + unsold else None,
        'median': median(prices) if prices else None,
        'low': min(prices) if prices else None,
        'high': max(prices) if prices else None,
    }


def price_groups(rows: List[dict], key: Callable[[dict], str], order: Optional[Callable[[str], object]] = None) -> List[dict]:
    """summarize() per group, in order (by the group name unless order says otherwise)"""
    groups = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    names = sorted(groups, key=order or (lambda name: name))
    return [dict(summarize(groups[name]), group=name) for name in names]


def monthly(rows: List[dict]) -> bool:
    ends = [r['end_ts'] for r in rows if r['end_ts']]
    return bool(ends) and max(ends) - min(ends) < MONTHLY_BELOW
