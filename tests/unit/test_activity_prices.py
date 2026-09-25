import io
import os
import sys
import argparse
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

import cli.commands.activity as cli
from core.models.model_definition import ModelDefinition
from pipelines.model_prices import mileage_band, monthly, price_groups, sale_period, split_rows, summarize
from storage.activity_db import ActivityDB


def ts(day: str) -> int:
    return int(datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())


# listing_id, title, year, result, price, currency, ended, transmission, mileage, make, fetched, tag
AUCTIONS = [
    (1, '2021 Chevrolet Corvette Stingray Coupe 3LT', 2021, 'sold', 80000, 'USD', '2026-01-10', 'Eight-Speed Dual-Clutch', 5000, 'Chevrolet', True, 'chevrolet/c8'),
    (2, '2021 Chevrolet Corvette Stingray Convertible', 2021, 'sold', 90000, 'USD', '2026-02-15', 'Eight-Speed Dual-Clutch', 12000, 'Chevrolet', True, 'chevrolet/c8'),
    (3, '2022 Chevrolet Corvette Stingray Coupe 2LT', 2022, 'sold', 100000, 'USD', '2026-04-20', 'Eight-Speed Dual-Clutch', 800, 'Chevrolet', True, 'chevrolet/c8'),
    (4, '2022 Chevrolet Corvette Stingray Coupe', 2022, 'reserve_not_met', 70000, 'USD', '2026-05-01', 'Eight-Speed Dual-Clutch', 30000, 'Chevrolet', True, 'chevrolet/c8'),
    (5, '2023 Chevrolet Corvette Z06 Coupe 3LZ', 2023, 'sold', 150000, 'USD', '2026-07-04', 'Seven-Speed Manual Transaxle', 2000, 'Chevrolet', True, 'chevrolet/c8'),
    (6, '2020 Chevrolet Corvette Stingray', 2020, 'withdrawn', None, None, '2026-03-01', None, None, 'Chevrolet', True, 'chevrolet/c8'),
    # left out of the prices: a parts listing, a euro sale's price, an auction not fetched yet
    (7, 'Wheels for C8 Chevrolet Corvette', None, 'sold', 5000, 'USD', '2026-06-01', None, None, 'Parts and Automobilia', True, 'parts-and-automobilia/wheels'),
    (8, '2023 Chevrolet Corvette Stingray Coupe', 2023, 'sold', 120000, 'EUR', '2026-06-10', 'Eight-Speed Dual-Clutch', 3000, 'Chevrolet', True, 'chevrolet/c8'),
    (9, '2024 Chevrolet Corvette E-Ray', 2024, 'sold', 110000, 'USD', '2026-06-20', None, None, None, False, None),
    # another model's
    (10, '2019 Chevrolet Corvette ZR1', 2019, 'sold', 999999, 'USD', '2026-06-30', 'Seven-Speed Manual', 100, 'Chevrolet', True, 'chevrolet/c7'),
]


def seeded_db():
    db = ActivityDB(':memory:')
    model = ModelDefinition(key='chevrolet/c8', slugs=['chevrolet/c8'], make='Chevrolet', model_full='Corvette C8',
                            model_short='Corvette ')
    db.save_model(model)
    with db.conn:
        db.conn.executemany("INSERT INTO members (slug, display_name) VALUES (?, ?)",
                            [('sellerx', 'SellerX'), ('buyery', 'BuyerY')])
        for (listing_id, title, year, result, price, currency, ended, transmission, mileage, make, fetched, tag) in AUCTIONS:
            db.conn.execute("""
                INSERT INTO auctions (listing_id, url, title, year, result, high_bid, currency, end_ts, transmission,
                                      mileage, make, model_slug, fetched_at, seller_slug, winner_slug)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sellerx', ?)
            """, (listing_id, f'https://bringatrailer.com/listing/car-{listing_id}/', title, year, result, price, currency,
                  ts(ended), transmission, mileage, make, tag, ts(ended) + 3600 if fetched else None,
                  'buyery' if result == 'sold' else None))
    db.record_model_listings(model.key, [(i, 'member') for i in (7, 9)], 'feed')
    return db, db.find_models('chevrolet/c8')[0]


class TestPriceMath(unittest.TestCase):

    def test_bands_and_periods(self):
        self.assertEqual([mileage_band(m) for m in (None, 0, 999, 1000, 4999, 5000, 99999, 100000)],
                         ['unknown', 'under 1k', 'under 1k', '1k-5k', '1k-5k', '5k-10k', '50k-100k', '100k+'])
        self.assertEqual(sale_period(ts('2026-02-15'), monthly=False), '2026 Q1')
        self.assertEqual(sale_period(ts('2026-10-01'), monthly=False), '2026 Q4')
        self.assertEqual(sale_period(ts('2026-02-15'), monthly=True), '2026-02')

    def test_summary_counts_prices_in_usd_and_sell_through_of_final_results(self):
        rows = [{'result': 'sold', 'high_bid': 10, 'currency': 'USD', 'make': 'X', 'end_ts': 1},
                {'result': 'sold', 'high_bid': 30, 'currency': 'USD', 'make': 'X', 'end_ts': 2},
                {'result': 'sold', 'high_bid': 99, 'currency': 'GBP', 'make': 'X', 'end_ts': 3},
                {'result': 'reserve_not_met', 'high_bid': 50, 'currency': 'USD', 'make': 'X', 'end_ts': 4},
                {'result': 'withdrawn', 'high_bid': None, 'currency': None, 'make': 'X', 'end_ts': 5}]
        s = summarize(rows)
        self.assertEqual((s['auctions'], s['sold'], s['reserve_not_met'], s['withdrawn']), (5, 3, 1, 1))
        self.assertEqual((s['sell_through'], s['median'], s['low'], s['high']), (0.75, 20, 10, 30))
        self.assertTrue(monthly(rows))
        self.assertEqual(summarize([])['median'], None)


class TestPriceReport(unittest.TestCase):

    def setUp(self):
        self.db, self.model = seeded_db()

    def tearDown(self):
        self.db.close()

    def rows(self):
        condition, params = self.db.model_scope(self.model)
        return [dict(r) for r in self.db.query(f"SELECT * FROM auctions a WHERE {condition}", params)]

    def test_the_numbers(self):
        rows = self.rows()
        self.assertEqual(sorted(r['listing_id'] for r in rows), [1, 2, 3, 4, 5, 6, 7, 8, 9])
        split = split_rows([r for r in rows if r['fetched_at']])
        self.assertEqual((split['parts'], split['other_currency']), (1, 1))
        total = summarize(split['cars'])
        self.assertEqual((total['auctions'], total['sold'], total['reserve_not_met'], total['withdrawn']), (7, 5, 1, 1))
        self.assertAlmostEqual(total['sell_through'], 5 / 6)
        self.assertEqual((total['median'], total['low'], total['high']), (95000, 80000, 150000))

        by_year = {g['group']: g for g in price_groups(split['cars'], lambda r: str(r['year']))}
        self.assertEqual((by_year['2021']['sold'], by_year['2021']['median'], by_year['2021']['low'], by_year['2021']['high']),
                         (2, 85000, 80000, 90000))
        self.assertEqual((by_year['2022']['sell_through'], by_year['2022']['median']), (0.5, 100000))
        # the euro sale counts as sold, but its price isn't mixed in with dollars
        self.assertEqual((by_year['2023']['sold'], by_year['2023']['median']), (2, 150000))
        self.assertEqual((by_year['2020']['sold'], by_year['2020']['median'], by_year['2020']['sell_through']), (0, None, None))

    def report(self, **overrides):
        args = argparse.Namespace(model='chevrolet/c8', model_def=self.model, since=None, top=10)
        vars(args).update(overrides)
        out = io.StringIO()
        with redirect_stdout(out):
            cli.report_model(self.db, args)
        return out.getvalue()

    def test_report_output(self):
        out = self.report()
        self.assertIn('Corvette C8 prices', out)
        self.assertIn('7 fetched: 5 sold, 1 reserve not met, 1 withdrawn', out)
        self.assertIn('sell-through  : 83%', out)
        self.assertIn('median $95,000, low $80,000, high $150,000 (USD; not counting 1 sale(s) in other currencies)', out)
        self.assertIn("not counted   : 1 parts listing(s), 1 of the model's auctions not fetched yet", out)
        # under a year of sales goes by month
        self.assertIn('by month of sale', out)
        self.assertRegex(out, r'2026-02\s+1\s+100%\s+\$90,000\s+\$90,000\s+\$90,000')
        self.assertRegex(out, r'2026-05\s+0\s+0%\s+-\s+-\s+-')
        self.assertRegex(out, r'Seven-Speed Manual Transaxle\s+1\s+100%\s+\$150,000')
        self.assertRegex(out, r'5k-10k\s+1\s+100%\s+\$80,000')
        self.assertNotIn('999,999', out)
        # the recent sales table: newest first, with the variant the title gives after make and model
        recent = out.split('recent sales')[1]
        self.assertLess(recent.index('$150,000'), recent.index('$100,000'))
        self.assertIn('Z06 Coupe 3LZ', recent)
        self.assertIn('2,000', recent)
        self.assertIn('https://bringatrailer.com/listing/car-5/', recent)
        # dollar sales only: the euro one isn't shown as dollars
        self.assertNotIn('car-8/', recent)
        self.assertNotIn('$120,000', recent)
        # then the people
        self.assertLess(out.index('recent sales'), out.index('top sellers (Corvette C8)'))

    def test_a_longer_range_goes_by_quarter_and_since_narrows_it(self):
        with self.db.conn:
            self.db.conn.execute("UPDATE auctions SET end_ts = ? WHERE listing_id = 1", (ts('2024-11-01'),))
        out = self.report()
        self.assertIn('by quarter of sale', out)
        self.assertRegex(out, r'2024 Q4\s+1\s+100%\s+\$80,000')
        out = self.report(since=ts('2026-04-01'))
        self.assertIn('prices since 2026-04-01', out)
        self.assertIn('4 fetched: 3 sold, 1 reserve not met, 0 withdrawn', out)
        self.assertIn('median $125,000, low $100,000, high $150,000', out)

    def test_recent_sales_stop_at_top_and_say_what_the_variant_needs(self):
        recent = self.report(top=2).split('recent sales')[1].split('top sellers')[0]
        self.assertEqual(recent.count('https://'), 2)
        self.model.model_short = None
        self.assertIn("recent sales (the variant needs the model's --make and --model-short)", self.report())

    def test_a_sale_without_an_amount_is_left_out_of_prices_not_called_foreign(self):
        with self.db.conn:
            self.db.conn.execute("UPDATE auctions SET high_bid = NULL, currency = NULL WHERE listing_id = 8")
        out = self.report()
        self.assertIn('(USD; not counting 1 sale(s) without a price)', out)
        self.assertIn('7 fetched: 5 sold', out)

    def test_a_report_of_an_unfollowed_slug_still_has_prices(self):
        out = self.report(model_def=None, model='chevrolet/c7')
        self.assertIn('chevrolet/c7 prices', out)
        self.assertIn('$999,999', out)


if __name__ == '__main__':
    unittest.main()
