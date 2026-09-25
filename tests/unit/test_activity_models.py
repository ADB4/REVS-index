import io
import os
import sys
import json
import math
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from local_http import LocalServer, FakeClock
from test_activity_parser import SELECTORS, listing_html
from core.models.activity import AuctionSummary
from core.models.model_definition import (
    ModelDefinition, load_model_file, model_from_entry, normalize_slug, same_slug, title_year, word_in
)
from pipelines.activity_pipeline import ActivityPipeline
from pipelines.model_pipeline import ModelPipeline, ModelSetupError, feed_scope
from sites.bringatrailer.activity_parser import ActivityParser, PARSER_VERSION
from sites.bringatrailer.http_client import BaTClient, HTTPStatusError, Page, SiteUnavailable
from sites.bringatrailer.model_page import ModelPageError, filter_params, params_from_url, parse_model_page
from storage.activity_db import ActivityDB


BASE = 'https://bringatrailer.com'
# small pages, so a model's feed runs to several
CONFIG = {'results_endpoint': '/api', 'results_per_page': 3, 'results_sort': 'td'}
NOW = 1_790_000_000
DAY = 86400


def model_page(keyword_pages=(11, 12), canonical=f'{BASE}/chevrolet/c8/', total=7):
    data = {'base_filter': {'keyword_pages': list(keyword_pages)}, 'items': [], 'items_total': total}
    return f"""<html><head><link rel="canonical" href="{canonical}" /></head><body>
    <script>var auctionsCompletedInitialData = {json.dumps(data)};</script></body></html>"""


def item(listing_id, title, days_ago, price=90000):
    return {
        'id': listing_id, 'url': f'{BASE}/listing/car-{listing_id}/', 'title': title, 'year': None,
        'sold_text': f'Sold for USD ${price:,} <span> on 9/1/2026 </span>', 'current_bid': price, 'currency': 'USD',
        'timestamp_end': NOW - days_ago * DAY, 'country_code_alpha3': 'USA', 'noreserve': False, 'premium': False
    }


class FeedClient:
    """the results feed (narrowed to the model's items when a base_filter is sent), a model page, and listing
    pages; interrupt_at raises on that (kind, page) once, as a ctrl-c or an outage would"""

    def __init__(self, model_items, site_items=(), model_html=None, listing_html_for=None):
        self.model_items = list(model_items)
        self.site_items = list(site_items)
        self.model_html = model_html
        self.listing_html_for = listing_html_for
        self.requests = []
        self.request_count = 0
        self.interrupt_at = None

    def get_json(self, path, params=None):
        params = dict(params or {})
        filtered = any(k.startswith('base_filter') for k in params)
        kind = 'model' if filtered else 'site'
        self.requests.append((kind, params['page']))
        self.request_count += 1
        if self.interrupt_at == (kind, params['page']):
            self.interrupt_at = None
            raise KeyboardInterrupt
        items = sorted(self.model_items if filtered else self.site_items, key=lambda i: -i['timestamp_end'])
        per = params['per_page']
        page = params['page']
        return {'items': items[(page - 1) * per: page * per], 'items_total': len(items),
                'pages_total': max(1, math.ceil(len(items) / per)), 'page_current': page}

    def get_page(self, url):
        self.requests.append(('page', url))
        self.request_count += 1
        if '/listing/' in url:
            return Page(url=url, text=self.listing_html_for(url), headers={})
        return Page(url=url, text=self.model_html, headers={})

    def feed_pages(self, kind):
        return [p for k, p in self.requests if k == kind]


def c8_items(n=7, first_id=100):
    return [item(first_id + i, f'{2020 + i % 5} Chevrolet Corvette Stingray Coupe', days_ago=i * 3) for i in range(n)]


class ModelCase(unittest.TestCase):

    def setUp(self):
        self.db = ActivityDB(':memory:')
        self.model = ModelDefinition(key='chevrolet/c8', slugs=['chevrolet/c8'], make='Chevrolet',
                                     model_full='Corvette C8', model_short='Corvette ')
        self.db.save_model(self.model)

    def tearDown(self):
        self.db.close()

    def run_model(self, client, model=None, **kwargs):
        pipeline = ActivityPipeline(client, ActivityParser(SELECTORS), self.db, CONFIG)
        model = model or self.db.find_models(self.model.key)[0]
        out = io.StringIO()
        with redirect_stdout(out):
            stats = ModelPipeline(pipeline, BASE).discover(model, **kwargs)
        return stats, out.getvalue()

    def members(self, status='member'):
        return sorted(r['listing_id'] for r in self.db.query(
            "SELECT listing_id FROM model_listings WHERE model_key = ? AND status = ?", (self.model.key, status)))


class TestSlugs(unittest.TestCase):

    def test_both_spellings_name_one_model(self):
        self.assertTrue(same_slug('e46-m3', 'bmw/e46-m3'))
        self.assertTrue(same_slug('https://bringatrailer.com/bmw/e46-m3/', '/BMW/E46-M3/'))
        self.assertFalse(same_slug('e46-m3', 'bmw/e46-m3-csl'))
        self.assertFalse(same_slug('bmw/e46-m3', 'alpina/e46-m3'))
        self.assertEqual(normalize_slug('https://bringatrailer.com/chevrolet/c8/?x=1'), 'chevrolet/c8')

    def test_a_short_slug_matches_a_tag_only_under_the_models_make(self):
        model = ModelDefinition(key='gt', slugs=['gt'], make='Ford')
        self.assertTrue(model.matches_tag('ford/gt'))
        self.assertFalse(model.matches_tag('pontiac/gt'))
        self.assertTrue(ModelDefinition(key='gt', slugs=['gt']).matches_tag('pontiac/gt'))
        # a make whose name and tag differ in spelling still agrees through the parsed make
        self.assertTrue(ModelDefinition(key='db9', slugs=['db9'], make='Aston Martin').matches_tag('aston/db9', 'Aston Martin'))

    def test_title_matching(self):
        model = ModelDefinition(key='e46-m3', slugs=['e46-m3'], make='BMW', model_short='M3 ', min_year=2001, max_year=2006)
        self.assertTrue(model.title_matches('2003 BMW M3 Coupe 6-Speed'))
        self.assertFalse(model.title_matches('2004 BMW M340i xDrive'))
        self.assertFalse(model.title_matches('1995 BMW M3 Coupe'))
        self.assertFalse(model.title_matches('2003 Alpina M3-ish Replica'))
        # as in the old lists, a model_short without a trailing space may run on
        clk = ModelDefinition(key='clk-class', slugs=['clk-class'], make='Mercedes-Benz', model_short='CLK')
        self.assertTrue(clk.title_matches('2006 Mercedes-Benz CLK500 Cabriolet'))
        self.assertFalse(clk.title_matches('2006 Mercedes-Benz SLK350'))
        self.assertTrue(word_in('Impreza WRX ', '2004 Subaru Impreza  WRX STi'))
        self.assertEqual(title_year('2,100-Mile 2020 Chevrolet Corvette'), 2020)

    def test_model_file_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'cars.json')
            with open(path, 'w') as f:
                json.dump([{'slug': ['c6-corvette'], 'make': 'Chevrolet', 'modelFull': 'C6 Corvette', 'modelShort': 'Corvette '},
                           {'slug': 'db9', 'make': 'Aston Martin', 'modelFull': 'DB9', 'modelSHort': 'DB9 ', 'minYear': 2004}], f)
            c6, db9 = load_model_file(path)
        self.assertEqual((c6.key, c6.slugs, c6.model_full, c6.model_short), ('c6-corvette', ['c6-corvette'], 'C6 Corvette', 'Corvette '))
        self.assertEqual((db9.key, db9.model_short, db9.min_year), ('db9', 'DB9 ', 2004))
        self.assertEqual(model_from_entry({'slug': ['/Porsche/997-GT3/'], 'make': 'Porsche', 'modelFull': '911 997 GT3'}).key,
                         'porsche/997-gt3')

    def test_stored_models_are_found_by_either_spelling(self):
        db = ActivityDB(':memory:')
        db.save_model(ModelDefinition(key='e46-m3', slugs=['e46-m3'], make='BMW', tag_slugs=['bmw/e46-m3']))
        db.save_model(ModelDefinition(key='chevrolet/c8', slugs=['chevrolet/c8']))
        self.assertEqual([m.key for m in db.find_models('bmw/e46-m3')], ['e46-m3'])
        self.assertEqual([m.key for m in db.find_models('E46-M3')], ['e46-m3'])
        self.assertEqual([m.key for m in db.find_models('c8')], ['chevrolet/c8'])
        self.assertEqual(db.find_models('c7'), [])
        db.close()


class TestModelPage(unittest.TestCase):

    def test_reads_the_feed_filter_and_the_pages_own_slug(self):
        page = parse_model_page(model_page(), f'{BASE}/c8-corvette/')
        self.assertEqual(page.slug, 'chevrolet/c8')
        self.assertEqual(page.params, {'base_filter[keyword_pages][]': [11, 12]})
        self.assertEqual(page.items_total, 7)

    def test_sub_models_are_the_tags_listings_carry(self):
        items = json.dumps([{'id': 11, 'url': f'{BASE}/chevrolet/corvette-c8-z06/'},
                            {'id': 12, 'url': f'{BASE}/chevrolet/corvette-c8-e-ray/'}])
        html = model_page().replace('<body>', f'<body><section class="model-list" data-items=\'{items}\'></section>')
        self.assertEqual(parse_model_page(html, BASE).sub_slugs, ['chevrolet/corvette-c8-z06', 'chevrolet/corvette-c8-e-ray'])

    def test_a_filter_with_only_empty_values_is_no_filter(self):
        with self.assertRaises(ModelPageError):
            parse_model_page(model_page(keyword_pages=()), BASE)

    def test_a_page_without_the_filter_still_names_its_tags(self):
        html = model_page().replace('auctionsCompletedInitialData', 'somethingElse')
        with self.assertRaises(ModelPageError) as e:
            parse_model_page(html, f'{BASE}/c8/')
        self.assertEqual(e.exception.slugs, ['chevrolet/c8'])

    def test_a_page_without_the_filter_is_an_error(self):
        with self.assertRaises(ModelPageError):
            parse_model_page('<html><script>var auctionsCompletedInitialData = {"items": []};</script></html>', BASE)
        with self.assertRaises(ModelPageError):
            parse_model_page('<html>nothing here</html>', BASE)

    def test_nested_filters_are_written_like_the_pages_script_writes_them(self):
        self.assertEqual(filter_params({'keyword_pages': [1], 'era': {'from': 2020}}),
                         {'base_filter[keyword_pages][]': [1], 'base_filter[era][from]': 2020})

    def test_a_request_copied_from_the_browser_gives_its_filter(self):
        url = (f'{BASE}/wp-json/bringatrailer/1.0/data/listings-filter?page=2&per_page=24&get_items=1&get_stats=0'
               '&base_filter%5Bkeyword_pages%5D%5B%5D=11&base_filter%5Bkeyword_pages%5D%5B%5D=12&sort=td&_=17900')
        self.assertEqual(params_from_url(url), {'base_filter[keyword_pages][]': ['11', '12']})
        with self.assertRaises(ValueError):
            params_from_url(f'{BASE}/wp-json/bringatrailer/1.0/data/listings-filter?page=2&sort=td')


class TestModelPageFeed(ModelCase):

    def test_the_model_page_is_read_once_and_its_feed_paged_through(self):
        client = FeedClient(c8_items(7), model_html=model_page())
        stats, out = self.run_model(client)
        self.assertEqual(client.requests[0], ('page', f'{BASE}/chevrolet/c8/'))
        self.assertEqual(client.feed_pages('model'), [1, 2, 3])
        self.assertEqual(client.feed_pages('site'), [])
        self.assertEqual(self.members(), list(range(100, 107)))
        self.assertTrue(stats['complete'])

        stored = self.db.find_models('chevrolet/c8')[0]
        self.assertEqual(stored.filters, {'chevrolet/c8': {'base_filter[keyword_pages][]': [11, 12]}})

        # the second run doesn't read the page again, and stops once it's below what the first run saw
        client = FeedClient(c8_items(7), model_html=model_page())
        self.run_model(client)
        self.assertEqual([r for r in client.requests if r[0] == 'page'], [])
        self.assertEqual(client.feed_pages('model'), [1])

    def test_a_second_run_picks_up_new_auctions_only(self):
        items = c8_items(12)
        self.run_model(FeedClient(items, model_html=model_page()))
        newer = [item(200 + i, '2025 Chevrolet Corvette Z06 Coupe', days_ago=-1 - i) for i in range(4)]
        client = FeedClient(newer + items, model_html=model_page())
        stats, out = self.run_model(client)
        self.assertEqual(stats['new'], 4)
        # the 4 new ones and 12 old ones make 6 pages; the walk stops at the first page below the last run's newest
        self.assertEqual(client.feed_pages('model'), [1, 2])
        self.assertEqual(self.members(), list(range(100, 112)) + list(range(200, 204)))

    def test_an_interrupted_first_run_resumes_without_holes(self):
        items = c8_items(15)
        client = FeedClient(items, model_html=model_page())
        client.interrupt_at = ('model', 3)
        with self.assertRaises(KeyboardInterrupt):
            self.run_model(client)
        self.assertEqual(self.members(), list(range(100, 106)))

        # new auctions arrived in between, pushing everything down a page
        newer = [item(300 + i, '2026 Chevrolet Corvette E-Ray', days_ago=-1 - i) for i in range(3)]
        client = FeedClient(newer + items, model_html=model_page())
        stats, out = self.run_model(client)
        self.assertEqual(self.members(), list(range(100, 115)) + [300, 301, 302])
        self.assertTrue(stats['complete'])
        # the backfill carries on from its cursor (with a page's overlap, in case the feed moved up), then the
        # newer ones, down to where the first run started
        self.assertEqual(client.feed_pages('model'), [2, 3, 4, 5, 6, 1, 2])

    def test_since_bounds_the_first_run_and_a_later_earlier_since_goes_deeper(self):
        items = c8_items(12)
        client = FeedClient(items, model_html=model_page())
        self.run_model(client, since_ts=NOW - 10 * DAY)
        self.assertEqual(client.feed_pages('model'), [1, 2])

        client = FeedClient(items, model_html=model_page())
        self.run_model(client, since_ts=NOW - 30 * DAY)
        self.assertEqual(client.feed_pages('model'), [2, 3, 4, 1])
        self.assertEqual(self.members(), list(range(100, 112)))

        client = FeedClient(items, model_html=model_page())
        stats, out = self.run_model(client, since_ts=NOW - 30 * DAY)
        self.assertIn('covered by an earlier run', out)
        self.assertEqual(client.feed_pages('model'), [1])

    def test_the_short_spelling_learns_the_tag_spelling_from_the_page(self):
        model = model_from_entry({'slug': ['c8-corvette'], 'make': 'Chevrolet', 'modelFull': 'C8 Corvette'})
        self.db.save_model(model)
        items = json.dumps([{'id': 11, 'url': f'{BASE}/chevrolet/corvette-c8-z06/'}])
        html = model_page().replace('<body>', f'<body><section class="model-list" data-items=\'{items}\'></section>')
        self.run_model(FeedClient(c8_items(3), model_html=html), model=self.db.find_models('c8-corvette')[0])
        stored = self.db.find_models('c8-corvette')[0]
        self.assertEqual(stored.tag_slugs, ['chevrolet/c8', 'chevrolet/corvette-c8-z06'])
        self.assertEqual([m.key for m in self.db.find_models('chevrolet/c8')], ['chevrolet/c8'])
        self.assertEqual([m.key for m in self.db.find_models('chevrolet/corvette-c8-z06')], ['c8-corvette'])
        # an auction the feed didn't list, reached another way, is the model's by its sub-model tag
        self.db.upsert_summaries([AuctionSummary(listing_id=990, url=f'{BASE}/listing/z-990/', title='2023 Chevrolet Corvette Z06',
                                                 result='sold', year=2023)], now=1)
        with self.db.conn:
            self.db.conn.execute("UPDATE auctions SET model_slug = 'chevrolet/corvette-c8-z06', make = 'Chevrolet' WHERE listing_id = 990")
        condition, params = self.db.model_scope(stored)
        self.assertIn(990, [r[0] for r in self.db.query(f"SELECT a.listing_id FROM auctions a WHERE {condition}", params)])

    def test_an_empty_first_page_isnt_taken_as_the_whole_feed(self):
        client = FeedClient([], model_html=model_page())
        stats, out = self.run_model(client)
        self.assertFalse(stats['complete'])
        client = FeedClient(c8_items(7), model_html=model_page())
        stats, out = self.run_model(client)
        self.assertEqual((client.feed_pages('model'), self.members()), ([1, 2, 3], list(range(100, 107))))
        self.assertTrue(stats['complete'])

    def test_a_page_budget_moves_the_backfill_on_each_run(self):
        items = c8_items(15)
        walked = []
        for run in range(5):
            client = FeedClient(items, model_html=model_page())
            stats, out = self.run_model(client, max_pages=2)
            walked.append(client.feed_pages('model'))
        # each resume rereads the page before its cursor; the last run has budget left for newer auctions
        self.assertEqual(walked, [[1, 2], [2, 3], [3, 4], [4, 5], [1]])
        self.assertTrue(stats['complete'])
        self.assertEqual(self.members(), list(range(100, 115)))

    def test_an_interrupted_run_for_newer_auctions_leaves_no_hole(self):
        items = c8_items(15)
        self.run_model(FeedClient(items, model_html=model_page()))
        newer = [item(400 + i, '2026 Chevrolet Corvette ZR1', days_ago=-1 - i) for i in range(12)]
        client = FeedClient(newer + items, model_html=model_page())
        client.interrupt_at = ('model', 3)
        with self.assertRaises(KeyboardInterrupt):
            self.run_model(client)
        client = FeedClient(newer + items, model_html=model_page())
        stats, out = self.run_model(client)
        self.assertEqual(self.members(), list(range(100, 115)) + list(range(400, 412)))
        self.assertTrue(stats['complete'])
        # a first run that stopped after one page reads each page once on the resume, the overlap page aside
        self.db.close()
        self.setUp()
        client = FeedClient(items, model_html=model_page())
        client.interrupt_at = ('model', 2)
        with self.assertRaises(KeyboardInterrupt):
            self.run_model(client)
        client = FeedClient(items, model_html=model_page())
        self.run_model(client)
        self.assertEqual(client.feed_pages('model'), [1, 2, 3, 4, 5, 1])

    def test_a_model_feed_keeps_its_own_cursor_and_watermark(self):
        self.db.set_meta('discover_watermark', '123')
        self.db.set_meta('backfill_next_page', '77')
        self.db.set_meta('feed_items_total', '264714')
        self.run_model(FeedClient(c8_items(7), model_html=model_page()))
        self.assertEqual([self.db.get_meta(k) for k in ('discover_watermark', 'backfill_next_page', 'feed_items_total')],
                         ['123', '77', '264714'])
        scoped = [r['key'] for r in self.db.query("SELECT key FROM meta WHERE key LIKE 'model:chevrolet/c8:%'")]
        self.assertTrue(any(k.endswith(':discover_watermark') for k in scoped))

    def test_a_feed_the_filter_didnt_narrow_is_refused(self):
        class Unfiltered(FeedClient):
            def get_json(self, path, params=None):
                data = super().get_json(path, {k: v for k, v in (params or {}).items() if not k.startswith('base_filter')})
                return dict(data, items_total=5000)
        client = Unfiltered([], [item(900 + i, f'2021 Porsche 911 #{i}', days_ago=i) for i in range(6)], model_html=model_page())
        with self.assertRaises(ModelSetupError) as e:
            self.run_model(client)
        self.assertIn('the model page said 7', str(e.exception))
        self.assertEqual((self.members(), self.db.query("SELECT COUNT(*) AS n FROM auctions")[0]['n']), ([], 0))

    def test_a_challenge_instead_of_the_model_page_stops_the_run(self):
        client = FeedClient([], [item(1, '2022 Chevrolet Corvette', days_ago=1)],
                            model_html='<html><title>Just a moment...</title>challenge-platform</html>')
        with self.assertRaises(SiteUnavailable):
            self.run_model(client, since_ts=NOW - 10 * DAY)
        self.assertEqual(client.feed_pages('site'), [])

    def test_the_filter_is_read_again_after_a_week_but_a_given_one_stands(self):
        self.run_model(FeedClient(c8_items(3), model_html=model_page()))
        self.db.set_meta('model:chevrolet/c8:chevrolet/c8:page:read_at', str(NOW - 8 * DAY))
        client = FeedClient(c8_items(3), model_html=model_page())
        self.run_model(client)
        self.assertEqual(client.requests[0], ('page', f'{BASE}/chevrolet/c8/'))

        self.db.set_meta('model:chevrolet/c8:chevrolet/c8:page:manual', '1')
        self.db.set_meta('model:chevrolet/c8:chevrolet/c8:page:read_at', '0')
        client = FeedClient(c8_items(3), model_html=model_page())
        self.run_model(client)
        self.assertNotIn('page', [r[0] for r in client.requests])

    def test_slugs_sharing_a_page_share_one_feed_and_missing_ones_are_flagged(self):
        model = ModelDefinition(key='c8', slugs=['c8-corvette', 'chevrolet/c8', 'corvette-c8-gone'], make='Chevrolet')
        self.db.save_model(model)

        class Pages(FeedClient):
            def get_page(self, url):
                if 'gone' in url:
                    self.requests.append(('page', url))
                    raise HTTPStatusError(404, url)
                return super().get_page(url)
        client = Pages(c8_items(7), model_html=model_page())
        stats, out = self.run_model(client, model=self.db.find_models('c8')[0])
        self.assertEqual(client.feed_pages('model'), [1, 2, 3])
        self.assertFalse(stats['complete'])
        self.assertIn('no feed for corvette-c8-gone', out)

    def test_a_page_budget_takes_turns_between_feeds(self):
        model = ModelDefinition(key='997', slugs=['porsche/997-a', 'porsche/997-b'], make='Porsche')
        self.db.save_model(model)

        class TwoPages(FeedClient):
            def get_page(self, url):
                self.requests.append(('page', url))
                keyword = 1 if url.endswith('997-a/') else 2
                return Page(url=url, text=model_page(keyword_pages=(keyword,)), headers={})

            def get_json(self, path, params=None):
                self.walked.append(params['base_filter[keyword_pages][]'][0])
                return super().get_json(path, params)
        first = TwoPages(c8_items(9), model_html=None)
        first.walked = []
        self.run_model(first, model=self.db.find_models('997')[0], max_pages=2)
        second = TwoPages(c8_items(9), model_html=None)
        second.walked = []
        self.run_model(second, model=self.db.find_models('997')[0], max_pages=2)
        self.assertEqual((first.walked[0], second.walked[0]), (1, 2))

    def test_years_outside_the_models_range_are_set_aside(self):
        model = self.db.find_models('chevrolet/c8')[0]
        model.min_year, model.max_year = 2021, 2023
        self.db.save_model(model)
        self.run_model(FeedClient(c8_items(5), model_html=model_page()))
        # titles run 2020, 2021, 2022, 2023, 2024
        self.assertEqual(self.members(), [101, 102, 103])
        self.assertEqual(self.members('out_of_years'), [100, 104])
        self.assertEqual(sorted(self.db.model_listing_ids(model)), [101, 102, 103])

    def test_changing_the_year_range_re_sorts_what_was_recorded(self):
        model = self.db.find_models('chevrolet/c8')[0]
        model.min_year, model.max_year = 2021, 2023
        self.db.save_model(model)
        self.run_model(FeedClient(c8_items(5), model_html=model_page()))
        # a title match set aside for its year
        self.db.upsert_summaries([AuctionSummary(listing_id=900, url=f'{BASE}/listing/car-900/', result='sold',
                                                 title='2019 Chevrolet Corvette Stingray', year=2019)], now=1)
        self.db.record_model_listings(model.key, [(900, 'out_of_years')], 'title')

        model.min_year, model.max_year = 2022, None
        self.assertEqual(self.db.apply_year_range(model), 2)
        # 2020 and 2021 out, 2022 and 2023 stay, 2024 back in
        self.assertEqual(self.members(), [102, 103, 104])
        self.assertEqual(self.members('out_of_years'), [100, 101, 900])
        self.assertEqual(self.db.apply_year_range(model), 0)

        # with no range, everything counts again; the title match goes back to be checked
        model.min_year = None
        self.db.apply_year_range(model)
        self.assertEqual((self.members(), self.members('unchecked')), ([100, 101, 102, 103, 104], [900]))

    def test_a_model_nothing_was_found_for_can_be_forgotten(self):
        self.db.set_meta('model:chevrolet/c8:chevrolet/c8:page:read_at', '1')
        self.db.set_meta('model:chevrolet/c80:x', '1')
        self.assertTrue(self.db.delete_model('chevrolet/c8'))
        self.assertEqual(self.db.find_models('chevrolet/c8'), [])
        self.assertEqual([r['key'] for r in self.db.query("SELECT key FROM meta WHERE key LIKE 'model:%'")],
                         ['model:chevrolet/c80:x'])
        self.db.save_model(self.model)
        self.run_model(FeedClient(c8_items(2), model_html=model_page()))
        self.assertFalse(self.db.delete_model('chevrolet/c8'))

    def test_a_feed_item_that_couldnt_be_stored_isnt_recorded(self):
        self.assertEqual(self.db.record_model_listings(self.model.key, [(12345, 'member')], 'feed'), 0)
        self.assertEqual(self.members(), [])

    def test_a_changed_filter_is_a_new_feed(self):
        model = self.db.find_models('chevrolet/c8')[0]
        self.assertNotEqual(feed_scope(model, 'chevrolet/c8', {'a': [1]}), feed_scope(model, 'chevrolet/c8', {'a': [1, 2]}))
        self.run_model(FeedClient(c8_items(7), model_html=model_page()))
        client = FeedClient(c8_items(7), model_html=model_page(keyword_pages=(11, 12, 13)))
        self.run_model(client, refresh_filter=True)
        self.assertEqual(client.requests[0][0], 'page')
        # a fresh backfill of the new filter's feed, not an incremental look at the top
        self.assertEqual(client.feed_pages('model'), [1, 2, 3])

    def test_a_model_page_that_wont_parse_falls_back_to_titles(self):
        client = FeedClient([], model_html='<html>redesigned</html>')
        with self.assertRaises(ModelSetupError) as e:
            self.run_model(client)
        self.assertIn('--since', str(e.exception))
        self.assertEqual(client.feed_pages('site'), [])


class TestModelPageRobots(ModelCase):
    """the model page is fetched through BaTClient, so robots.txt decides whether it's requested at all"""

    def setUp(self):
        super().setUp()
        self.server = LocalServer()
        self.model = ModelDefinition(key='chevrolet/c8', slugs=['chevrolet/c8'])
        self.db.save_model(self.model)

    def tearDown(self):
        self.server.close()
        super().tearDown()

    def client(self):
        return BaTClient(self.server.base_url, [], crawl_delay=0, delay=0, jitter=0, fetch_robots=True,
                         clock=FakeClock())

    def read_feeds(self, client):
        pipeline = ActivityPipeline(client, ActivityParser(SELECTORS), self.db, CONFIG)
        out = io.StringIO()
        with redirect_stdout(out):
            return ModelPipeline(pipeline, self.server.base_url).feeds(self.db.find_models('chevrolet/c8')[0]), out.getvalue()

    def test_a_disallowed_model_page_is_never_requested(self):
        self.server.routes['/robots.txt'] = (200, {}, b'User-agent: *\nDisallow: /chevrolet/\n')
        self.server.routes['/chevrolet/c8/'] = (200, {}, model_page().encode())
        feeds, out = self.read_feeds(self.client())
        self.assertEqual(feeds, {})
        self.assertEqual(self.server.paths(), ['/robots.txt'])
        self.assertIn('robots.txt disallows', out)

    def test_an_allowed_model_page_is_read_after_robots_txt(self):
        self.server.routes['/robots.txt'] = (200, {}, b'User-agent: *\nDisallow: /member/\n')
        self.server.routes['/chevrolet/c8/'] = (200, {}, model_page(canonical=f'{self.server.base_url}/chevrolet/c8/').encode())
        feeds, out = self.read_feeds(self.client())
        self.assertEqual(self.server.paths(), ['/robots.txt', '/chevrolet/c8/'])
        self.assertEqual(feeds, {'chevrolet/c8': {'base_filter[keyword_pages][]': [11, 12]}})

    def test_a_missing_model_page_says_to_check_the_slug(self):
        self.server.routes['/robots.txt'] = (200, {}, b'User-agent: *\nDisallow:\n')
        feeds, out = self.read_feeds(self.client())
        self.assertEqual(feeds, {})
        self.assertIn('check the slug', out)


class TestTitleFallback(ModelCase):
    """source B: the site-wide feed, title matches, then the fetched pages' model tags settle it"""

    SITE = [
        item(1, '2022 Chevrolet Corvette Stingray Coupe 3LT', days_ago=1),
        item(2, '2021 Porsche 911 Carrera S', days_ago=2),
        item(3, '2023 Chevrolet Corvette Z06 Convertible', days_ago=3),
        # a C7 whose title reads the same until its page's tag says otherwise
        item(4, '2019 Chevrolet Corvette Grand Sport', days_ago=4),
        item(5, '1967 Chevrolet Corvette 427/435 Convertible', days_ago=5),
        item(6, '2024 Chevrolet Corvette E-Ray', days_ago=40),
    ]

    def setUp(self):
        super().setUp()
        self.model.min_year = 2019
        self.db.save_model(self.model)

    def test_since_is_required_and_nothing_is_walked_without_it(self):
        client = FeedClient([], self.SITE, model_html='<html>no filter</html>')
        self.db.set_meta('feed_items_total', '264714')
        with self.assertRaises(ModelSetupError) as e:
            self.run_model(client)
        self.assertIn('about 88,238 pages', str(e.exception))
        self.assertEqual(client.feed_pages('site'), [])

    def test_make_and_model_short_are_required(self):
        self.db.save_model(ModelDefinition(key='chevrolet/c8', slugs=['chevrolet/c8']))
        client = FeedClient([], self.SITE, model_html='<html>no filter</html>')
        with self.assertRaises(ModelSetupError) as e:
            self.run_model(client, since_ts=NOW - 10 * DAY)
        self.assertIn('--model-short', str(e.exception))

    def test_title_matches_are_checked_against_the_fetched_pages(self):
        client = FeedClient([], self.SITE, model_html='<html>no filter</html>')
        stats, out = self.run_model(client, since_ts=NOW - 10 * DAY)
        self.assertEqual(client.feed_pages('site'), [1, 2])
        # the 1967 is out of the model's years, the E-Ray ended before --since
        self.assertEqual(self.members('unchecked'), [1, 3, 4])

        # what fetching would save: two tagged as the C8, one as the C7
        tags = {1: 'chevrolet/c8', 3: 'chevrolet/c8', 4: 'chevrolet/c7'}
        for listing_id, tag in tags.items():
            groups = f'''<div class="group-item"><a class="group-link" href="{BASE}/chevrolet/"><strong class="group-title-label">Make</strong>Chevrolet</a></div>
                <div class="group-item"><a class="group-link" href="{BASE}/{tag}/"><strong class="group-title-label">Model</strong>Corvette</a></div>'''
            detail = ActivityParser(SELECTORS).parse_listing(listing_html(listing_id=listing_id, history='', groups=groups), 'u')
            self.db.save_detail(detail, NOW, PARSER_VERSION)
        checked = self.db.classify_model_candidates(self.db.find_models('chevrolet/c8')[0])
        self.assertEqual((checked['member'], checked['other_model'], dict(checked['other_tags'])),
                         (2, 1, {'chevrolet/c7': 1}))
        self.assertEqual((self.members(), self.members('other_model')), ([1, 3], [4]))

        # adding the other tag to the model takes it back in
        model = self.db.find_models('chevrolet/c8')[0]
        model.tag_slugs.append('chevrolet/c7')
        self.db.save_model(model)
        self.assertEqual(self.db.classify_model_candidates(model)['member'], 1)
        self.assertEqual(self.members(), [1, 3, 4])
        model.tag_slugs.remove('chevrolet/c7')
        self.db.save_model(model)
        self.db.classify_model_candidates(model)

        # a later run doesn't queue the ruled-out one again
        client = FeedClient([], self.SITE, model_html='<html>no filter</html>')
        stats, out = self.run_model(client, since_ts=NOW - 10 * DAY)
        self.assertEqual(stats['candidates'], 0)
        self.assertEqual(sorted(self.db.model_listing_ids(self.db.find_models('chevrolet/c8')[0])), [1, 3])

    def test_the_fallback_shares_the_site_wide_feeds_progress(self):
        self.db.set_meta('discover_watermark', str(NOW - DAY // 2))
        self.db.set_meta('backfill_reached', str(NOW - 100 * DAY))
        client = FeedClient([], self.SITE, model_html='<html>no filter</html>')
        stats, out = self.run_model(client, since_ts=NOW - 10 * DAY)
        self.assertIn('covered by an earlier run', out)
        self.assertEqual(client.feed_pages('site'), [1])


class TestHistoryScope(unittest.TestCase):
    """a model run follows the bat history links on its own listings only, and never a url given up on"""

    def test_scoped_history_includes_links_to_stored_but_unfetched_auctions(self):
        db = ActivityDB(':memory:')
        db.upsert_summaries([AuctionSummary(listing_id=i, url=f'{BASE}/listing/l-{i}/', title='x', result='sold')
                             for i in (1, 2)], now=1)
        with db.conn:
            db.conn.execute("INSERT INTO listing_links (listing_id, related_url, related_listing_id) VALUES (1, ?, 2)",
                            (f'{BASE}/listing/l-2/',))
        self.assertEqual([r['url'] for r in db.pending_history(3, [1])], [f'{BASE}/listing/l-2/'])
        # the whole queue gets it from pending() instead, so it isn't counted twice
        self.assertEqual(db.pending_history(3), [])
        with db.conn:
            db.conn.execute("UPDATE auctions SET fetched_at = 5 WHERE listing_id = 2")
        self.assertEqual(db.pending_history(3, [1]), [])
        db.close()

    def test_pending_history_scoped_to_listings(self):
        db = ActivityDB(':memory:')
        db.upsert_summaries([AuctionSummary(listing_id=i, url=f'{BASE}/listing/l-{i}/', title='x', result='sold')
                             for i in (1, 2, 3)], now=1)
        with db.conn:
            db.conn.executemany("INSERT INTO listing_links (listing_id, related_url, follow_attempts) VALUES (?, ?, ?)", [
                (1, f'{BASE}/listing/a/', 0), (2, f'{BASE}/listing/b/', 0),
                # given up on through listing 3, then linked again from listing 2
                (3, f'{BASE}/listing/dead/', 3), (2, f'{BASE}/listing/dead/', 0)])
        self.assertEqual([r['url'] for r in db.pending_history(3, [1])], [f'{BASE}/listing/a/'])
        self.assertEqual([r['url'] for r in db.pending_history(3, [2])], [f'{BASE}/listing/b/'])
        self.assertEqual(db.pending_history(3, []), [])
        self.assertEqual(len(db.pending_history(3)), 2)

        # fetch with history_from queues only those; without it, a scoped fetch queues none
        client = FeedClient([], listing_html_for=lambda url: listing_html(listing_id=7000, history=''))
        pipeline = ActivityPipeline(client, ActivityParser(SELECTORS), db, CONFIG)
        with redirect_stdout(io.StringIO()):
            pipeline.fetch(listing_ids=[], history_from=[1], limit=5)
        self.assertEqual([u for k, u in client.requests if k == 'page'], [f'{BASE}/listing/a/'])
        client.requests.clear()
        with redirect_stdout(io.StringIO()):
            pipeline.fetch(listing_ids=[], limit=5)
        self.assertEqual(client.requests, [])
        db.close()


class TestModelScope(ModelCase):
    """what reports and exports count as the model's: members, plus fetched auctions tagged as it, less exclusions"""

    def save(self, listing_id, tag, year=2022, make='Chevrolet'):
        groups = f'''<div class="group-item"><a class="group-link" href="{BASE}/{tag.split('/')[0]}/"><strong class="group-title-label">Make</strong>{make}</a></div>
            <div class="group-item"><a class="group-link" href="{BASE}/{tag}/"><strong class="group-title-label">Model</strong>x</a></div>'''
        page = listing_html(listing_id=listing_id, history='', groups=groups).replace('2003 BMW M3 Coupe', f'{year} Chevrolet Corvette')
        self.db.save_detail(ActivityParser(SELECTORS).parse_listing(page, 'u'), NOW, PARSER_VERSION)

    def scoped(self, model):
        condition, params = self.db.model_scope(model)
        return sorted(r[0] for r in self.db.query(f"SELECT a.listing_id FROM auctions a WHERE {condition}", params))

    def test_scope(self):
        model = ModelDefinition(key='c8-corvette', slugs=['c8-corvette'], make='Chevrolet', tag_slugs=['chevrolet/c8'],
                                min_year=2020)
        self.db.save_model(model)
        self.save(1, 'chevrolet/c8')                  # tagged as the model
        self.save(2, 'chevrolet/c8-corvette')         # the short spelling's tag
        self.save(3, 'chevrolet/c7')                  # another model...
        self.db.record_model_listings(model.key, [(3, 'member')], 'feed')   # ...that the model's feed lists
        self.save(4, 'chevrolet/c8')
        self.db.record_model_listings(model.key, [(4, 'other_model')], 'title')
        self.save(5, 'chevrolet/c8', year=2019)       # before the model's years
        self.save(6, 'other-make/c8-corvette', make='Other Make')   # same last part, another make
        self.assertEqual(self.scoped(model), [1, 2, 3])


if __name__ == '__main__':
    unittest.main()
