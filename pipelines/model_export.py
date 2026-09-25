import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from core.models.listing import Listing
from core.models.model_definition import ModelDefinition, title_year
from extractors.variant import extract_variant
from storage.activity_db import PARTS_MAKE


RESULTS = {'sold': 'Sold', 'reserve_not_met': 'Reserve Not Met', 'withdrawn': 'Withdrawn'}
# the page's country ("USA"), or the feed's code, which is alpha-2 when it has no alpha-3
USA = ('USA', 'US')

# what the selenium scraper's pipeline skipped (pipelines/scraping_pipeline.py), and so what the old files lack. its
# year range needs no filter here: a model's auctions outside its years aren't the model's (see model_scope)
OLD_FILTERS = ('non_usa', 'modified', 'no_vin')

# the csv adds the crawler's own columns to the old fields
CSV_EXTRAS = ['listing_id', 'end_ts', 'high_bid', 'currency', 'n_bids', 'bids_reported', 'seller_slug', 'winner_slug',
              'high_bidder_slug', 'vehicle_id', 'model_slug', 'country', 'country_code', 'categories']

EXPORT_QUERY = """
    SELECT a.*, s.display_name AS seller_name, w.display_name AS winner_name
    FROM auctions a
    LEFT JOIN members s ON s.slug = a.seller_slug
    LEFT JOIN members w ON w.slug = a.winner_slug
    WHERE a.fetched_at IS NOT NULL{where}
    ORDER BY a.end_ts DESC, a.listing_id DESC
"""


@dataclass
class ExportRow:
    listing: Listing
    row: dict


def sale_date(end_ts: Optional[int]) -> Optional[str]:
    return datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime('%Y-%m-%d') if end_ts else None


def json_list(value: Optional[str]) -> list:
    return json.loads(value) if value else []


def to_listing(row: dict, model: Optional[ModelDefinition], source: str) -> Listing:
    """an auction in the old scraper's shape: make and model from the model's definition, the price only for a
    sale in dollars, the winner as high_bidder only for a sale"""
    make = (model.make if model else None) or row['make']
    sold = row['result'] == 'sold'
    return Listing(
        url=row['url'],
        source=source,
        title=row['title'] or '',
        lot_number=row['lot_number'],
        seller=row['seller_name'] or row['seller_slug'],
        seller_type=row['seller_type'],
        result=RESULTS.get(row['result']),
        high_bidder=(row['winner_name'] or row['winner_slug']) if sold else None,
        price=row['high_bid'] if sold and row['currency'] == 'USD' else None,
        sale_date=sale_date(row['end_ts']),
        number_of_bids=row['bids_reported'] if row['bids_reported'] is not None else row['n_bids'],
        vin=row['vin'],
        year=row['year'] or title_year(row['title']),
        make=make,
        model=(model.model_full if model else None) or row['model'],
        variant=extract_variant(row['title'] or '', make, model.model_short if model else None),
        # saved before parser 4, a listing has only its first category tag to go by
        convertible=bool(row['convertible']) if row['convertible'] is not None
        else 'convertible' in (row['categories'] or row['category'] or '').lower(),
        engine=row['engine'],
        transmission=row['transmission'],
        exterior_color=row['exterior_color'],
        interior_color=row['interior_color'],
        mileage=row['mileage'],
        location=row['location'],
        country=row['country'] or row['country_code'],
        listing_details=json_list(row['listing_details']),
        excerpt=json_list(row['excerpt'])
    )


def skip_reason(row: dict, listing: Listing, keep: set) -> Optional[str]:
    """why the old scraper would have left this auction out of its file, or None. keep names the filters turned off"""
    if 'non_usa' not in keep and (row['country'] or row['country_code'] or 'USA') not in USA:
        return 'non_usa'
    if 'modified' not in keep and 'modified' in (listing.title or '').lower():
        return 'modified'
    if 'no_vin' not in keep and not listing.vin:
        return 'no_vin'
    return None


def export_rows(db, where: str, params: list, model: Optional[ModelDefinition], source: str,
                keep: set = frozenset(), withdrawn: bool = False) -> dict:
    """the fetched auctions a condition on auctions (as a) picks, as old-shape listings, with what was skipped.
    parts listings are never exported: their make and model would be the car's"""
    kept, skipped = [], {}
    for r in db.query(EXPORT_QUERY.format(where=where), params):
        row = dict(r)
        if row['make'] == PARTS_MAKE:
            reason = 'parts'
        elif row['result'] not in ('sold', 'reserve_not_met') and not (withdrawn and row['result'] == 'withdrawn'):
            reason = row['result'] or 'unknown'
        else:
            listing = to_listing(row, model, source)
            reason = skip_reason(row, listing, keep)
            if reason is None:
                kept.append(ExportRow(listing, row))
                continue
        skipped[reason] = skipped.get(reason, 0) + 1
    return {'rows': kept, 'skipped': skipped}


def write_json(rows: List[ExportRow], path: str) -> None:
    # as storage/json_storage.py wrote the old files
    with open(path, 'w') as f:
        json.dump([r.listing.to_dict() for r in rows], f, indent=4)


def write_csv(rows: List[ExportRow], path: str) -> None:
    fields = list(Listing(url='', source='', title='').to_dict()) + CSV_EXTRAS
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            record = r.listing.to_dict()
            record['listing_details'] = json.dumps(record['listing_details'], ensure_ascii=False)
            record['excerpt'] = json.dumps(record['excerpt'], ensure_ascii=False)
            record.update({k: r.row.get(k) for k in CSV_EXTRAS})
            writer.writerow(record)
