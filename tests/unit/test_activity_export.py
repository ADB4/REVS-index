import io
import os
import sys
import csv
import json
import time
import tempfile
import subprocess
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from test_activity_specs import SPECS, spec_page
from test_activity_parser import SELECTORS
import cli.commands.activity as cli
import cli.commands.ingest as ingest
from core.models.listing import Listing
from core.models.model_definition import ModelDefinition
from sites.bringatrailer.activity_parser import ActivityParser, PARSER_VERSION
from storage.activity_db import ActivityDB


ROOT = os.path.join(os.path.dirname(__file__), '../..')
OLD_KEYS = list(Listing(url='', source='', title='').to_dict())


def ts(day: str) -> int:
    return int(datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())


# listing_id, title, result, price, currency, ended, vin, country, make, tag, year
ROWS = [
    (2, '2021 Chevrolet Corvette Stingray Convertible 2LT', 'reserve_not_met', 70000, 'USD', '2026-05-01', '1G1YA3D40M5100002', 'USA', 'Chevrolet', 'chevrolet/c8', 2021),
    (3, '2022 Chevrolet Corvette Stingray Coupe', 'sold', 88000, 'USD', '2026-04-01', None, 'USA', 'Chevrolet', 'chevrolet/c8', 2022),
    (4, '2023 Chevrolet Corvette Z06 Coupe', 'sold', 120000, 'CAD', '2026-03-01', '1G1YD2D30P5100004', 'Canada', 'Chevrolet', 'chevrolet/c8', 2023),
    (5, 'Modified 2020 Chevrolet Corvette Stingray', 'sold', 60000, 'USD', '2026-02-01', '1G1Y72D40L5100005', 'USA', 'Chevrolet', 'chevrolet/c8', 2020),
    (6, '2019 Chevrolet Corvette ZR1', 'sold', 130000, 'USD', '2026-01-15', '1G1YR2D60K5100006', 'USA', 'Chevrolet', 'chevrolet/c8', 2019),
    (7, 'Wheels for C8 Chevrolet Corvette', 'sold', 5000, 'USD', '2026-01-10', None, 'USA', 'Parts and Automobilia', 'parts-and-automobilia/wheels', None),
    (8, '2024 Chevrolet Corvette E-Ray', 'withdrawn', None, None, '2026-01-05', '1G1YE2D40R5100008', 'USA', 'Chevrolet', 'chevrolet/c8', 2024),
    (9, '2020 Chevrolet Corvette C7 Grand Sport', 'sold', 70000, 'USD', '2026-01-01', '1G1YY2D70L5100009', 'USA', 'Chevrolet', 'chevrolet/c7', 2020),
]


class ExportCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, 'activity.db')
        db = ActivityDB(self.db_path)
        db.save_model(ModelDefinition(key='chevrolet/c8', slugs=['chevrolet/c8'], make='Chevrolet',
                                      model_full='C8 Corvette', model_short='Corvette ', min_year=2020, max_year=2025))
        # one auction the way a fetch saves it: parsed, specs and all
        page = spec_page(listing_id=1).replace('2003 BMW M3 Coupe 6-Speed', '2022 Chevrolet Corvette Stingray Coupe 3LT')
        detail = ActivityParser(SELECTORS, SPECS).parse_listing(page, 'https://bringatrailer.com/listing/c8-1/')
        db.save_detail(detail, ts('2026-06-01'), PARSER_VERSION)
        with db.conn:
            db.conn.execute("INSERT INTO members (slug, display_name) VALUES ('x', 'SellerX'), ('y', 'BuyerY')")
            for listing_id, title, result, price, currency, ended, vin, country, make, tag, year in ROWS:
                db.conn.execute("""
                    INSERT INTO auctions (listing_id, url, title, year, result, high_bid, currency, end_ts, vin, country,
                                          make, model_slug, fetched_at, seller_slug, winner_slug, n_bids)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'x', ?, 3)
                """, (listing_id, f'https://bringatrailer.com/listing/c8-{listing_id}/', title, year, result, price,
                      currency, ts(ended), vin, country, make, tag, ts(ended) + 60, 'y' if result == 'sold' else None))
        # the parsed one is tagged 'chevrolet/corvette-c8', so the model's feed is what makes it the model's
        db.record_model_listings('chevrolet/c8', [(1, 'member'), (7, 'member')], 'feed')
        db.close()

    def tearDown(self):
        self.tmp.cleanup()

    def export(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(cli, 'EXPORT_DIR', os.path.join(self.tmp.name, 'raw')):
            status = cli.main(['--db', self.db_path, 'export', *argv])
        return status, out.getvalue()

    def exported(self, *argv):
        status, out = self.export(*argv)
        self.assertEqual(status, 0, out)
        with open(os.path.join(self.tmp.name, 'raw', 'chevrolet-c8_data.json')) as f:
            return json.load(f), out


class TestExport(ExportCase):

    def test_the_old_filters_apply_by_default(self):
        records, out = self.exported('--model', 'chevrolet/c8')
        # newest first, as the old files ran
        self.assertEqual([r['url'].rsplit('-', 1)[1] for r in records], ['2/', '1/'])
        # the 2019 is outside the model's years, so not the model's at all
        self.assertIn("left out: 1 with 'modified' in the title, 1 without a 17-character vin, 1 outside the USA, "
                      "1 parts listings, 1 withdrawn", out)
        self.assertIn('2 auction(s) (1 sold) written to', out)

    def test_records_have_the_old_shape_and_values(self):
        records, _ = self.exported('--model', 'c8')
        unsold, sold = records
        for record in records:
            self.assertEqual(list(record), OLD_KEYS)
            self.assertEqual(Listing.from_dict(record).to_dict(), record)

        self.assertEqual((sold['result'], sold['price'], sold['high_bidder'], sold['sale_date']),
                         ('Sold', 25500, 'Lummy1088', '2025-12-31'))
        self.assertEqual((sold['make'], sold['model'], sold['variant'], sold['year']),
                         ('Chevrolet', 'C8 Corvette', 'Stingray Coupe 3LT', 2022))
        self.assertEqual((sold['engine'], sold['transmission'], sold['mileage']),
                         ('6.2-Liter LT2 V8', 'Eight-Speed Dual-Clutch Automatic Transaxle', 121000))
        self.assertEqual((sold['exterior_color'], sold['interior_color'], sold['convertible']),
                         ('Rapid Blue Paint', 'Jet Black Leather Upholstery', True))
        self.assertEqual((sold['seller'], sold['seller_type'], sold['lot_number'], sold['number_of_bids']),
                         ('TestSeller', 'N/A', '100001', 4))
        self.assertEqual(sold['vin'], 'WBSBL93453JR22502')
        self.assertEqual(sold['listing_details'][1], '121k Miles')
        self.assertEqual(len(sold['excerpt']), 2)

        # reserve not met: no price, no buyer, the end date still
        self.assertEqual((unsold['result'], unsold['price'], unsold['high_bidder'], unsold['sale_date']),
                         ('Reserve Not Met', None, 'N/A', '2026-05-01'))
        self.assertEqual((unsold['engine'], unsold['mileage'], unsold['convertible'], unsold['listing_details']),
                         ('N/A', None, False, []))

    def test_all_keeps_what_the_old_scraper_skipped_but_parts(self):
        records, out = self.exported('--model', 'chevrolet/c8', '--all')
        by_id = {r['url'].rsplit('-', 1)[1].strip('/'): r for r in records}
        self.assertEqual(sorted(by_id), ['1', '2', '3', '4', '5', '8'])
        self.assertEqual(by_id['8']['result'], 'Withdrawn')
        # a sale in another currency has no price in the old shape
        self.assertEqual((by_id['4']['result'], by_id['4']['price']), ('Sold', None))
        self.assertIn('1 sale(s) in other currencies are written without a price', out)
        self.assertIn('left out: 1 parts listings', out)

    def test_single_filters_can_be_turned_off(self):
        records, _ = self.exported('--model', 'chevrolet/c8', '--include-no-vin', '--include-modified')
        self.assertEqual(sorted(r['url'][-2] for r in records), ['1', '2', '3', '5'])

    def test_since(self):
        records, _ = self.exported('--model', 'chevrolet/c8', '--since', '2026-01-01', '--all')
        self.assertEqual(len(records), 5)
        records, _ = self.exported('--model', 'chevrolet/c8', '--since', '2026-04-15')
        self.assertEqual([r['sale_date'] for r in records], ['2026-05-01'])

    def test_csv_adds_the_crawlers_columns(self):
        path = os.path.join(self.tmp.name, 'c8.csv')
        status, out = self.export('--model', 'chevrolet/c8', '--format', 'csv', '--output', path, '--all')
        self.assertEqual(status, 0, out)
        with open(path, newline='', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(list(rows[0])[:len(OLD_KEYS)], OLD_KEYS)
        cad = next(r for r in rows if r['listing_id'] == '4')
        self.assertEqual((cad['price'], cad['high_bid'], cad['currency'], cad['winner_slug']), ('', '120000', 'CAD', 'y'))
        self.assertEqual(json.loads(next(r for r in rows if r['listing_id'] == '1')['listing_details'])[1], '121k Miles')

    def test_an_unfollowed_slug_exports_by_tag_with_the_pages_names(self):
        status, out = self.export('--model', 'chevrolet/c7', '--output', os.path.join(self.tmp.name, 'c7.json'))
        self.assertEqual(status, 0, out)
        self.assertIn("isn't a followed model", out)
        with open(os.path.join(self.tmp.name, 'c7.json')) as f:
            [record] = json.load(f)
        self.assertEqual((record['make'], record['model'], record['variant']), ('Chevrolet', 'N/A', 'Standard'))

    def test_sale_dates_are_utc_whatever_the_machines_zone(self):
        db = ActivityDB(self.db_path)
        with db.conn:
            # 02:00 utc on the 1st is still the 30th in california
            db.conn.execute("UPDATE auctions SET end_ts = ? WHERE listing_id = 2", (ts('2026-05-01') + 2 * 3600,))
        db.close()
        with mock.patch.dict(os.environ, {'TZ': 'America/Los_Angeles'}):
            time.tzset()
            try:
                records, _ = self.exported('--model', 'chevrolet/c8')
            finally:
                os.environ.pop('TZ', None)
            time.tzset()
        self.assertEqual(records[0]['sale_date'], '2026-05-01')

    def test_older_rows_and_missing_names_are_pointed_out(self):
        db = ActivityDB(self.db_path)
        with db.conn:
            db.conn.execute("UPDATE auctions SET parser_version = 3, convertible = NULL, category = 'Convertibles' "
                            "WHERE listing_id = 2")
            db.conn.execute("UPDATE models SET model_short = NULL")
        db.close()
        records, out = self.exported('--model', 'chevrolet/c8')
        self.assertIn('1 of these were saved by an older parser', out)
        self.assertIn('chevrolet/c8 has no --model-short, so every variant is Standard', out)
        self.assertEqual({r['variant'] for r in records}, {'Standard'})
        self.assertIs(records[0]['convertible'], True)

    def test_an_unfollowed_slug_covering_several_models_is_refused(self):
        status, out = self.export('--model', 'chevrolet', '--all')
        self.assertEqual(status, 1)
        self.assertIn('chevrolet covers 3 models (chevrolet/c7, chevrolet/c8, chevrolet/corvette-c8)', out)

    def test_nothing_to_export_writes_nothing(self):
        status, out = self.export('--model', 'porsche/991')
        self.assertEqual(status, 1)
        self.assertIn('nothing to export', out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, 'raw')))


class TestDownstream(ExportCase):
    """the exported file goes through the old tools as the selenium scraper's did"""

    def test_normalize_reads_and_writes_it(self):
        records, _ = self.exported('--model', 'chevrolet/c8', '--all')
        source = os.path.join(self.tmp.name, 'raw', 'chevrolet-c8_data.json')
        target = os.path.join(self.tmp.name, 'normalized.json')
        result = subprocess.run([sys.executable, os.path.join(ROOT, 'cli/commands/normalize.py'), '--input', source,
                                 '--output', target, '--analyze'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'normalized {len(records)} listings', result.stdout)
        with open(target) as f:
            self.assertEqual(json.load(f), records)

    def test_ingest_builds_rows_for_a_sale_and_a_reserve_not_met(self):
        records, _ = self.exported('--model', 'chevrolet/c8')
        conn = FakeConnection()
        for record in records:
            self.assertEqual(ingest.ingest_listing(conn, record, make_id=1, model_id=2), 'inserted')
        inserted = [params for sql, params in conn.executed if sql.lstrip().startswith('INSERT INTO listings')]
        self.assertEqual([(p['sale_price'], p['reserve_met'], p['result']) for p in inserted],
                         [(None, None, 'Reserve Not Met'), (2550000, True, 'Sold')])


class FakeConnection:
    """enough of a psycopg2 connection for ingest_listing: no listing exists yet, every insert returns id 1"""

    def __init__(self):
        self.executed = []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass


class FakeCursor:

    def __init__(self, conn):
        self.conn = conn
        self.last = ''

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.last = sql
        self.conn.executed.append((sql, params))

    def fetchone(self):
        return (1,) if 'RETURNING' in self.last else None


if __name__ == '__main__':
    unittest.main()
