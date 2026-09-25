import io
import os
import sys
import json
import math
import unittest
from contextlib import redirect_stdout
from urllib.parse import parse_qs, urlparse

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from test_activity_parser import SELECTORS, listing_html
from test_activity_pipeline import CliCase
import cli.commands.activity as cli
from core.models.activity import AuctionSummary
from pipelines.activity_pipeline import ActivityPipeline
from sites.bringatrailer.activity_parser import ActivityParser
from storage.activity_db import ActivityDB


FEED = '/wp-json/bringatrailer/1.0/data/listings-filter'
BASE = 'https://bringatrailer.com'
NOW = 1_790_000_000
DAY = 86400
CONFIG = {'results_endpoint': '/api', 'results_per_page': 3, 'results_sort': 'td',
          'parts_categories': {379: 'Parts', 380: 'Wheels'}}


def item(listing_id, title, days_ago, base=BASE):
    return {
        'id': listing_id, 'url': f'{base}/listing/car-{listing_id}/', 'title': title, 'year': None,
        'sold_text': 'Sold for USD $9,000 <span> on 9/1/2026 </span>', 'current_bid': 9000, 'currency': 'USD',
        'timestamp_end': NOW - int(days_ago * DAY), 'country_code_alpha3': 'USA', 'noreserve': False, 'premium': False
    }


def feed_page(items, params):
    """one page of a feed narrowed by its category[] parameter, newest first"""
    per, page = int(params['per_page']), int(params['page'])
    items = sorted(items, key=lambda i: -i['timestamp_end'])
    return {'items': items[(page - 1) * per: page * per], 'items_total': len(items),
            'pages_total': max(1, math.ceil(len(items) / per)), 'page_current': page}


# the site-wide feed: cars and, among them, parts; each parts feed lists only its own
CARS = [item(100 + i, f'202{i % 5} Porsche 911 #{i}', days_ago=i) for i in range(6)]
PARTS = [item(200, 'Illuminated Dealer Sign', 1.5), item(201, 'Wheels for a C8 Corvette Z06', 3.5)]
BY_CATEGORY = {'379': [PARTS[0]], '380': [PARTS[1]]}


class FeedClient:

    def __init__(self):
        self.requests = []
        self.request_count = 0

    def get_json(self, path, params=None):
        params = dict(params)
        category = params.get('category[]')
        self.requests.append((str(category[0]) if category else 'site', params['page']))
        self.request_count += 1
        return feed_page(BY_CATEGORY[str(category[0])] if category else CARS + PARTS, params)


class TestPartsLast(unittest.TestCase):

    def setUp(self):
        self.db = ActivityDB(':memory:')
        self.db.upsert_summaries([AuctionSummary(listing_id=i, url=f'{BASE}/listing/x-{i}/', title=str(i), result='sold',
                                                 end_ts=NOW - i) for i in (1, 2, 3, 4)], now=1)
        self.db.record_feed_category(379, [2])
        self.db.record_feed_category(380, [3, 999])

    def tearDown(self):
        self.db.close()

    def test_parts_come_after_everything_else(self):
        ids = [r['listing_id'] for r in self.db.pending(last_categories=[379, 380])]
        self.assertEqual(ids, [1, 4, 2, 3])
        self.assertEqual([r['listing_id'] for r in self.db.pending(limit=2, last_categories=[379, 380])], [1, 4])
        # without the setting, newest first as before
        self.assertEqual([r['listing_id'] for r in self.db.pending()], [1, 2, 3, 4])

    def test_skipped_parts_arent_queued_or_counted(self):
        self.assertEqual([r['listing_id'] for r in self.db.pending(skip_categories=[379, 380])], [1, 4])
        self.assertEqual(self.db.pending_count(skip_categories=[379]), 3)

    def test_a_listing_the_database_doesnt_have_isnt_recorded(self):
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM feed_categories WHERE listing_id = 999")[0]['n'], 0)


class TestPartsFeeds(unittest.TestCase):

    def setUp(self):
        self.db = ActivityDB(':memory:')

    def tearDown(self):
        self.db.close()

    def walk(self, **kwargs):
        client = FeedClient()
        pipeline = ActivityPipeline(client, ActivityParser(SELECTORS), self.db, CONFIG)
        with redirect_stdout(io.StringIO()):
            stats = pipeline.discover_parts(**kwargs)
        return client, stats

    def test_each_parts_feed_labels_its_auctions(self):
        client, stats = self.walk()
        self.assertEqual(client.requests, [('379', 1), ('380', 1)])
        rows = self.db.query("SELECT listing_id, category FROM feed_categories ORDER BY listing_id")
        self.assertEqual([(r['listing_id'], r['category']) for r in rows], [(200, 379), (201, 380)])
        self.assertTrue(stats['complete'])

    def test_a_later_walk_reads_only_whats_new_and_keeps_its_own_cursor(self):
        self.db.set_meta('discover_watermark', '123')
        self.walk()
        client, stats = self.walk()
        self.assertEqual(client.requests, [('379', 1), ('380', 1)])
        self.assertEqual(self.db.get_meta('discover_watermark'), '123')
        self.assertTrue(self.db.get_meta('category:379:discover_watermark'))


class TestCliParts(CliCase):

    def setUp(self):
        super().setUp()
        base = self.server.base_url
        self.cars = [item(100 + i, f'202{i} Porsche 911 #{i}', days_ago=i, base=base) for i in range(3)]
        self.parts = [item(200, 'Illuminated Dealer Sign', 0.5, base=base)]
        self.feed_requests = []
        self.server.routes[FEED] = self.feed
        for listing in self.cars + self.parts:
            self.server.routes[f"/listing/car-{listing['id']}/"] = (
                200, {'Content-Type': 'text/html'},
                listing_html(listing_id=listing['id'], history='', end_ts=listing['timestamp_end']).encode())

    def feed(self, handler):
        query = parse_qs(urlparse(handler.path).query)
        category = query.get('category[]', [None])[0]
        self.feed_requests.append(category or 'site')
        items = {None: self.cars + self.parts, '379': self.parts, '380': []}[category]
        body = feed_page(items, {'per_page': query['per_page'][0], 'page': query['page'][0]})
        return 200, {'Content-Type': 'application/json'}, json.dumps(body).encode()

    def listing_order(self):
        return [p for p in self.site_paths() if p.startswith('/listing/')]

    def test_sync_labels_parts_and_fetches_them_last(self):
        status, out = self.run_cli('sync')
        self.assertEqual(status, 0, out)
        self.assertEqual(self.feed_requests, ['site', '379', '380'])
        # the sign ended most recently, but waits for the cars
        self.assertEqual(self.listing_order(), ['/listing/car-100/', '/listing/car-101/', '/listing/car-102/',
                                                '/listing/car-200/'])
        status, out = self.run_cli('report')
        self.assertIn('parts auctions      : 1 known from the parts feeds, 1 fetched', out)

    def test_skip_parts_leaves_them_for_a_later_run(self):
        status, out = self.run_cli('sync', '--skip-parts')
        self.assertEqual(status, 0, out)
        self.assertNotIn('/listing/car-200/', self.listing_order())
        self.assertIn('1 parts auction(s) left for a run without --skip-parts', out)
        self.server.hits.clear()
        status, out = self.run_cli('fetch')
        self.assertEqual((status, self.listing_order()), (0, ['/listing/car-200/']))

    def test_a_fetch_budget_goes_to_cars_first(self):
        self.assertEqual(self.run_cli('discover')[0], 0)
        self.server.hits.clear()
        status, out = self.run_cli('fetch', '--limit', '3')
        self.assertEqual(self.listing_order(), ['/listing/car-100/', '/listing/car-101/', '/listing/car-102/'])

    def test_no_parts_categories_means_no_parts_feeds(self):
        with open(cli.CONFIG_PATH) as f:
            config = yaml.safe_load(f)
        config['activity']['parts_categories'] = {}
        with open(cli.CONFIG_PATH, 'w') as f:
            yaml.safe_dump(config, f)
        self.assertEqual(self.run_cli('discover')[0], 0)
        self.assertEqual(self.feed_requests, ['site'])


if __name__ == '__main__':
    unittest.main()
