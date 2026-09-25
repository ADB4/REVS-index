import io
import os
import sys
import json
import math
import unittest
from contextlib import redirect_stdout
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from test_activity_parser import listing_html
from test_activity_pipeline import CliCase
from pipelines.activity_pipeline import ActivityPipeline
from core.models.model_definition import ModelDefinition
from storage.activity_db import ActivityDB


FEED = '/wp-json/bringatrailer/1.0/data/listings-filter'
NOW = 1_790_000_000
DAY = 86400


class ModelCliCase(CliCase):
    """a model page at /chevrolet/c8/, its feed (the results feed with its base_filter), and its listing pages"""

    def setUp(self):
        super().setUp()
        self.model_items = []
        self.site_items = []
        self.feed_requests = []
        self.server.routes[FEED] = self.feed
        self.server.routes['/chevrolet/c8/'] = (200, {'Content-Type': 'text/html'}, self.model_page().encode())

    def model_page(self, keyword_pages=(11, 12)):
        data = {'base_filter': {'keyword_pages': list(keyword_pages)}, 'items': [], 'items_total': 0}
        return f"""<html><head><link rel="canonical" href="{self.server.base_url}/chevrolet/c8/" /></head><body>
        <script>var auctionsCompletedInitialData = {json.dumps(data)};</script></body></html>"""

    def feed(self, handler):
        query = parse_qs(urlparse(handler.path).query)
        filtered = 'base_filter[keyword_pages][]' in query
        self.feed_requests.append(('model' if filtered else 'site', int(query['page'][0]), query.get('base_filter[keyword_pages][]')))
        items = sorted(self.model_items if filtered else self.site_items, key=lambda i: -i['timestamp_end'])
        per, page = int(query['per_page'][0]), int(query['page'][0])
        body = {'items': items[(page - 1) * per: page * per], 'items_total': len(items),
                'pages_total': max(1, math.ceil(len(items) / per)), 'page_current': page}
        return 200, {'Content-Type': 'application/json'}, json.dumps(body).encode()

    def add_car(self, n, title='2022 Chevrolet Corvette Stingray Coupe 3LT', days_ago=1, tag='chevrolet/c8',
                history='', price=90000, in_model_feed=True):
        base = self.server.base_url
        item = {
            'id': 7000 + n, 'url': f'{base}/listing/car-{n}/', 'title': title, 'year': None,
            'sold_text': f'Sold for USD ${price:,} <span> on 9/1/2026 </span>', 'current_bid': price,
            'currency': 'USD', 'timestamp_end': NOW - days_ago * DAY, 'country_code_alpha3': 'USA',
            'noreserve': False, 'premium': False
        }
        (self.model_items if in_model_feed else self.site_items).append(item)
        self.serve_car(n, title, tag, history, days_ago)

    def serve_car(self, n, title, tag='chevrolet/c8', history='', days_ago=1):
        base = self.server.base_url
        groups = f'''<div class="group-item"><a class="group-link" href="{base}/chevrolet/"><strong class="group-title-label">Make</strong>Chevrolet</a></div>
            <div class="group-item"><a class="group-link" href="{base}/{tag}/"><strong class="group-title-label">Model</strong>Corvette C8</a></div>'''
        page = listing_html(listing_id=7000 + n, history=history, groups=groups, end_ts=NOW - days_ago * DAY)
        page = page.replace('2003 BMW M3 Coupe 6-Speed', title)
        self.server.routes[f'/listing/car-{n}/'] = (200, {'Content-Type': 'text/html; charset=utf-8'}, page.encode())

    def listing_paths(self):
        return [p for p in self.site_paths() if p.startswith('/listing/')]

    def db_query(self, sql, params=()):
        db = ActivityDB(self.db_path)
        try:
            return [dict(r) for r in db.query(sql, params)]
        finally:
            db.close()


class TestModelCommand(ModelCliCase):

    def test_model_run_discovers_fetches_and_reports(self):
        for n in range(5):
            self.add_car(n, days_ago=n + 1)
        status, out = self.run_cli('model', 'chevrolet/c8', '--model-full', 'Corvette C8')
        self.assertEqual(status, 0, out)

        # robots.txt, the model page once, its feed, then each listing
        self.assertEqual(self.server.paths()[:3], ['/robots.txt', '/chevrolet/c8/', FEED])
        self.assertEqual({(kind, keywords) for kind, _, keywords in [(k, p, tuple(kw or ())) for k, p, kw in self.feed_requests]},
                         {('model', ('11', '12'))})
        self.assertEqual(sorted(self.listing_paths()), [f'/listing/car-{n}/' for n in range(5)])
        self.assertIn('top sellers (Corvette C8)', out)
        self.assertIn('5 auction(s) saved', out)

        row = self.db_query("SELECT * FROM models")[0]
        self.assertEqual((row['key'], row['model_full']), ('chevrolet/c8', 'Corvette C8'))
        self.assertEqual(json.loads(row['filters']), {'chevrolet/c8': {'base_filter[keyword_pages][]': [11, 12]}})
        self.assertEqual(len(self.db_query("SELECT * FROM model_listings WHERE status = 'member'")), 5)

    def test_a_second_run_fetches_only_whats_new(self):
        for n in range(5):
            self.add_car(n, days_ago=n + 2)
        self.assertEqual(self.run_cli('model', 'chevrolet/c8')[0], 0)
        self.server.hits.clear()
        self.feed_requests.clear()
        self.add_car(9, days_ago=1)

        status, out = self.run_cli('model', 'chevrolet/c8', '--no-report')
        self.assertEqual(status, 0, out)
        self.assertNotIn('/chevrolet/c8/', self.server.paths())
        self.assertEqual(self.listing_paths(), ['/listing/car-9/'])
        self.assertNotIn('top sellers', out)

    def test_an_interrupted_run_resumes_where_it_stopped(self):
        for n in range(4):
            self.add_car(n, days_ago=n + 1)
        real_fetch = ActivityPipeline._fetch_listing
        calls = []

        def fetch_then_interrupt(pipeline, url, now):
            calls.append(url)
            if len(calls) == 3:
                raise KeyboardInterrupt
            return real_fetch(pipeline, url, now)

        with mock.patch.object(ActivityPipeline, '_fetch_listing', fetch_then_interrupt):
            status, out = self.run_cli('model', 'chevrolet/c8')
        self.assertEqual(status, 130, out)
        self.assertEqual(len(self.db_query("SELECT * FROM auctions WHERE fetched_at IS NOT NULL")), 2)

        self.server.hits.clear()
        status, out = self.run_cli('model', 'chevrolet/c8')
        self.assertEqual(status, 0, out)
        self.assertEqual(len(self.listing_paths()), 2)
        self.assertEqual(len(self.db_query("SELECT * FROM auctions WHERE fetched_at IS NOT NULL")), 4)

    def test_bat_history_links_of_the_models_cars_are_followed(self):
        base = self.server.base_url
        history = f'''<div class="history"><div class="items">
            <a href="{base}/listing/car-50/" class="item"><div class="message"><em>Sold by a to b for USD $80,000</em>
            <span class="date-localize" data-timestamp="{NOW - 400 * DAY}">x</span></div></a></div></div>'''
        self.add_car(0, history=history)
        self.serve_car(50, '2020 Chevrolet Corvette Stingray Coupe', days_ago=400)
        status, out = self.run_cli('model', 'chevrolet/c8')
        self.assertEqual(status, 0, out)
        self.assertEqual(self.listing_paths(), ['/listing/car-0/', '/listing/car-50/'])
        # the earlier auction is tagged as the model, so it's in the model's report too
        self.assertIn('2 auction(s) saved', out)

    def test_a_block_stops_the_run_with_exit_2(self):
        for n in range(3):
            self.add_car(n)
        self.server.routes[FEED] = (403, {}, b'blocked')
        status, out = self.run_cli('model', 'chevrolet/c8')
        self.assertEqual(status, 2, out)
        self.assertEqual(self.listing_paths(), [])

    def test_json_entries_are_followed_and_remembered(self):
        self.add_car(0)
        path = os.path.join(self.tmp.name, 'cars.json')
        with open(path, 'w') as f:
            json.dump([{'slug': ['chevrolet/c8'], 'make': 'Chevrolet', 'modelFull': 'C8 Corvette', 'modelShort': 'Corvette ',
                        'minYear': 2020}], f)
        status, out = self.run_cli('model', '--json', path, '--no-report')
        self.assertEqual(status, 0, out)
        row = self.db_query("SELECT * FROM models")[0]
        self.assertEqual((row['make'], row['model_full'], row['model_short'], row['min_year']),
                         ('Chevrolet', 'C8 Corvette', 'Corvette ', 2020))

        # the stored definition is reused by the short spelling, with its cached filter
        self.server.hits.clear()
        status, out = self.run_cli('model', 'c8', '--no-report')
        self.assertEqual(status, 0, out)
        self.assertNotIn('/chevrolet/c8/', self.server.paths())

    def test_a_new_year_range_applies_to_what_was_already_recorded(self):
        for n, year in enumerate((2020, 2021, 2022)):
            self.add_car(n, title=f'{year} Chevrolet Corvette Stingray', days_ago=n + 1)
        self.assertEqual(self.run_cli('model', 'chevrolet/c8', '--min-year', '2021', '--no-report')[0], 0)
        self.assertEqual(sorted(self.listing_paths()), ['/listing/car-1/', '/listing/car-2/'])

        self.server.hits.clear()
        status, out = self.run_cli('model', 'chevrolet/c8', '--min-year', '2020', '--no-report')
        self.assertEqual(status, 0, out)
        # the 2020 wasn't fetched for the model before; now it is, with no new feed pages needed
        self.assertEqual(self.listing_paths(), ['/listing/car-0/'])
        statuses = {r['listing_id']: r['status'] for r in self.db_query("SELECT * FROM model_listings")}
        self.assertEqual(statuses, {7000: 'member', 7001: 'member', 7002: 'member'})

    def test_a_model_without_a_page_or_names_is_refused_before_any_feed_request(self):
        status, out = self.run_cli('model', 'c8-corvette')
        self.assertEqual(status, 1, out)
        self.assertEqual(self.site_paths(), ['/c8-corvette/'])
        self.assertIn('check the slug', out)
        self.assertIn('--model-short', out)

    def test_title_fallback_needs_since(self):
        status, out = self.run_cli('model', 'c8-corvette', '--make', 'Chevrolet', '--model-short', 'Corvette ')
        self.assertEqual(status, 1, out)
        self.assertIn('give --since', out)
        self.assertEqual(self.feed_requests, [])

    def test_title_fallback_end_to_end(self):
        self.add_car(0, title='2022 Chevrolet Corvette Stingray', tag='chevrolet/c8-corvette', in_model_feed=False)
        self.add_car(1, title='2019 Chevrolet Corvette Grand Sport', tag='chevrolet/c7', in_model_feed=False)
        self.add_car(2, title='2021 Porsche 911 Turbo S', tag='porsche/992-911', in_model_feed=False)
        status, out = self.run_cli('model', 'c8-corvette', '--make', 'Chevrolet', '--model-short', 'Corvette ',
                                   '--since', '2026-09-01')
        self.assertEqual(status, 0, out)
        self.assertEqual({kind for kind, _, _ in self.feed_requests}, {'site'})
        # both corvettes are fetched; only the one tagged as the model stays in it
        self.assertEqual(sorted(self.listing_paths()), ['/listing/car-0/', '/listing/car-1/'])
        statuses = {r['listing_id']: r['status'] for r in self.db_query("SELECT * FROM model_listings")}
        self.assertEqual(statuses, {7000: 'member', 7001: 'other_model'})
        self.assertIn("1 are the model, 1 another model's", out)

    def history_to(self, n):
        base = self.server.base_url
        return f'''<div class="history"><div class="items">
            <a href="{base}/listing/car-{n}/" class="item"><div class="message"><em>Sold by a to b for USD $80,000</em>
            <span class="date-localize" data-timestamp="{NOW - 400 * DAY}">x</span></div></a></div></div>'''

    def test_limit_budgets_the_run_and_a_rerun_fetches_the_rest(self):
        for n in range(4):
            self.add_car(n, days_ago=n + 1)
        status, out = self.run_cli('model', 'chevrolet/c8', '--limit', '2', '--no-report')
        self.assertEqual((status, len(self.listing_paths())), (0, 2))
        self.server.hits.clear()
        self.assertEqual(self.run_cli('model', 'chevrolet/c8', '--no-report')[0], 0)
        self.assertEqual(len(self.listing_paths()), 2)

    def test_limit_spans_the_models_of_a_json_file(self):
        self.add_car(0)
        path = os.path.join(self.tmp.name, 'cars.json')
        with open(path, 'w') as f:
            json.dump([{'slug': ['chevrolet/c8']}, {'slug': ['chevrolet/c8-other']}], f)
        self.server.routes['/chevrolet/c8-other/'] = (200, {}, self.model_page(keyword_pages=(99,)).encode())
        status, out = self.run_cli('model', '--json', path, '--limit', '1', '--no-report')
        self.assertEqual(status, 0, out)
        self.assertIn('the --limit budget is spent', out)
        self.assertEqual(len(self.listing_paths()), 1)

    def test_no_follow_history(self):
        self.add_car(0, history=self.history_to(50))
        self.serve_car(50, '2020 Chevrolet Corvette Stingray Coupe', days_ago=400)
        self.assertEqual(self.run_cli('model', 'chevrolet/c8', '--no-follow-history', '--no-report')[0], 0)
        self.assertEqual(self.listing_paths(), ['/listing/car-0/'])

    def test_since_leaves_older_auctions_of_the_model_unfetched(self):
        self.add_car(0, days_ago=1)
        self.add_car(1, days_ago=100)
        self.assertEqual(self.run_cli('model', 'chevrolet/c8', '--since', '2026-08-01', '--no-report')[0], 0)
        self.assertEqual(self.listing_paths(), ['/listing/car-0/'])

    def test_a_rerun_follows_links_to_auctions_stored_but_not_fetched(self):
        from core.models.activity import AuctionSummary
        # the earlier auction of car-0's car is already known, from a site-wide discovery say
        db = ActivityDB(self.db_path)
        db.upsert_summaries([AuctionSummary(listing_id=7050, url=f'{self.server.base_url}/listing/car-50/', result='sold',
                                            title='2020 Chevrolet Corvette Stingray Coupe', end_ts=NOW - 400 * DAY)], now=1)
        db.close()
        self.add_car(0, history=self.history_to(50))
        self.serve_car(50, '2020 Chevrolet Corvette Stingray Coupe', tag='chevrolet/c7', days_ago=400)
        self.assertEqual(self.run_cli('model', 'chevrolet/c8', '--limit', '1', '--no-report')[0], 0)
        self.assertEqual(self.listing_paths(), ['/listing/car-0/'])
        self.server.hits.clear()
        self.assertEqual(self.run_cli('model', 'chevrolet/c8', '--no-report')[0], 0)
        self.assertEqual(self.listing_paths(), ['/listing/car-50/'])

    def test_a_slug_that_found_nothing_isnt_kept(self):
        status, out = self.run_cli('model', 'c8')
        self.assertEqual(status, 1, out)
        self.assertIn("c8 isn't kept", out)
        self.assertEqual(self.db_query("SELECT * FROM models"), [])
        # so the long spelling isn't taken for it
        self.add_car(0)
        self.server.hits.clear()
        self.assertEqual(self.run_cli('model', 'chevrolet/c8', '--no-report')[0], 0)
        self.assertIn('/chevrolet/c8/', self.server.paths())

    def test_a_followed_model_without_a_feed_tries_another_spelling(self):
        db = ActivityDB(self.db_path)
        db.save_model(ModelDefinition(key='c8', slugs=['c8'], make='Chevrolet', model_short='Corvette '))
        db.close()
        self.add_car(0)
        status, out = self.run_cli('model', 'chevrolet/c8', '--no-report')
        self.assertEqual(status, 0, out)
        self.assertEqual(self.site_paths()[:2], ['/c8/', '/chevrolet/c8/'])
        self.assertIn('no feed for c8', out)
        self.assertEqual(json.loads(self.db_query("SELECT slugs FROM models")[0]['slugs']), ['c8', 'chevrolet/c8'])

    def test_filter_url_goes_to_the_slug_named(self):
        db = ActivityDB(self.db_path)
        db.save_model(ModelDefinition(key='chevrolet/c8', slugs=['chevrolet/c8', 'chevrolet/c8-z06']))
        db.close()
        url = f'{self.server.base_url}{FEED}?page=2&base_filter%5Bkeyword_pages%5D%5B%5D=99'
        status, out = self.run_cli('model', 'chevrolet/c8-z06', '--filter-url', url, '--no-report')
        filters = json.loads(self.db_query("SELECT filters FROM models")[0]['filters'])
        self.assertEqual(filters['chevrolet/c8-z06'], {'base_filter[keyword_pages][]': ['99']})
        self.assertEqual(filters['chevrolet/c8'], {'base_filter[keyword_pages][]': [11, 12]})

    def test_a_json_entry_in_the_other_spelling_is_the_same_model(self):
        self.add_car(0)
        self.assertEqual(self.run_cli('model', 'chevrolet/c8', '--no-report')[0], 0)
        path = os.path.join(self.tmp.name, 'cars.json')
        with open(path, 'w') as f:
            json.dump([{'slug': ['c8'], 'make': 'Chevrolet', 'modelFull': 'C8 Corvette', 'modelShort': 'Corvette '}], f)
        self.server.hits.clear()
        status, out = self.run_cli('model', '--json', path, '--no-report')
        self.assertEqual(status, 0, out)
        rows = self.db_query("SELECT key, slugs, model_full FROM models")
        self.assertEqual([(r['key'], json.loads(r['slugs']), r['model_full']) for r in rows],
                         [('chevrolet/c8', ['chevrolet/c8', 'c8'], 'C8 Corvette')])

    def test_an_ambiguous_slug_exits_1(self):
        db = ActivityDB(self.db_path)
        db.save_model(ModelDefinition(key='bmw/m3', slugs=['bmw/m3']))
        db.save_model(ModelDefinition(key='alpina/m3', slugs=['alpina/m3']))
        db.close()
        status, out = self.run_cli('model', 'm3')
        self.assertEqual(status, 1)
        self.assertIn('names 2 followed models', out)
        self.assertEqual(self.server.paths(), [])

    def test_auctions_saved_by_an_older_parser_are_pointed_out(self):
        self.add_car(0)
        self.assertEqual(self.run_cli('model', 'chevrolet/c8', '--no-report')[0], 0)
        db = ActivityDB(self.db_path)
        with db.conn:
            db.conn.execute("UPDATE auctions SET parser_version = 3")
        db.close()
        status, out = self.run_cli('model', 'chevrolet/c8', '--no-report')
        self.assertIn('1 of the model\'s auctions were saved by an older parser', out)
        self.server.hits.clear()
        self.run_cli('model', 'chevrolet/c8', '--no-report', '--upgrade')
        self.assertEqual(self.listing_paths(), ['/listing/car-0/'])

    def test_model_runs_keep_pages_for_reparse(self):
        self.add_car(0)
        self.assertEqual(self.run_cli('model', 'chevrolet/c8', '--no-report')[0], 0)
        status, out = self.run_cli('reparse')
        self.assertEqual(status, 0, out)
        self.assertIn('1 listing(s) reparsed', out)

    def test_slug_or_json_is_required(self):
        with redirect_stdout(io.StringIO()), mock.patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
            self.run_cli('model')

    def test_another_run_holding_the_database_exits_75(self):
        from cli.commands.activity import run_lock
        with run_lock(self.db_path):
            status, out = self.run_cli('model', 'chevrolet/c8')
        self.assertEqual(status, 75)
        self.assertEqual(self.server.paths(), [])


class TestReportModel(ModelCliCase):

    def setUp(self):
        super().setUp()
        for n in range(3):
            self.add_car(n, days_ago=n + 1)
        status, out = self.run_cli('model', 'chevrolet/c8', '--model-full', 'Corvette C8', '--no-report')
        self.assertEqual(status, 0, out)

    def test_either_spelling_reports_the_model(self):
        outputs = [self.run_cli('report', '--model', ref) for ref in ('chevrolet/c8', 'c8', 'CHEVROLET/C8/')]
        self.assertEqual({status for status, _ in outputs}, {0})
        self.assertEqual(len({out for _, out in outputs}), 1)
        self.assertIn('top sellers (Corvette C8)', outputs[0][1])
        self.assertIn('Just1more', outputs[0][1])

    def test_an_unfollowed_short_slug_matches_tags_by_their_last_part(self):
        db = ActivityDB(self.db_path)
        with db.conn:
            db.conn.execute("DELETE FROM model_listings")
            db.conn.execute("DELETE FROM models")
        db.close()
        status, out = self.run_cli('report', '--model', 'c8')
        self.assertEqual(status, 0, out)
        self.assertIn('top sellers (c8)', out)
        self.assertIn('Just1more', out)


if __name__ == '__main__':
    unittest.main()
