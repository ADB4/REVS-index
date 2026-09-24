import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from local_http import LocalServer, FakeClock
from test_activity_parser import SELECTORS, listing_html
import cli.commands.activity as cli
import sites.bringatrailer.http_client as http_client
from core.models.activity import AuctionSummary
from pipelines.activity_pipeline import ActivityPipeline, CircuitOpen
from sites.bringatrailer.activity_parser import ActivityParser
from sites.bringatrailer.http_client import Page, HTTPStatusError, RateLimited, SiteUnavailable
from storage.activity_db import ActivityDB


REAL_CONFIG = os.path.join(os.path.dirname(__file__), '../../config/sites/bringatrailer.yaml')
ACTIVITY_CONFIG = {'results_endpoint': '/api', 'results_per_page': 60, 'results_sort': 'td'}
CHALLENGE = b'<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>'


def summaries(base_url, n, first_id=5000):
    return [
        AuctionSummary(listing_id=first_id + i, url=f"{base_url}/listing/car-{i}/", title=f"car {i}",
                       result='sold', high_bid=25500, currency='USD', end_ts=1_767_210_841 - i)
        for i in range(n)
    ]


class FakeClient:
    """answers get_page from a script: a Page's text, or an exception to raise, per request in order"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []
        self.request_count = 0

    def get_page(self, url):
        self.requests.append(url)
        self.request_count += 1
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return Page(url=url, text=outcome(url) if callable(outcome) else outcome, headers={})


def page_for(url):
    """a synthetic listing page whose id matches the car-N url the queue was seeded with"""
    n = int(url.rstrip('/').rsplit('-', 1)[1])
    return listing_html(listing_id=5000 + n, history='')


class TestFetchCircuitBreaker(unittest.TestCase):

    def setUp(self):
        self.db = ActivityDB(':memory:')
        self.db.upsert_summaries(summaries('https://bringatrailer.com', 40), now=1)

    def tearDown(self):
        self.db.close()

    def fetch(self, outcomes, **kwargs):
        client = FakeClient(outcomes)
        pipeline = ActivityPipeline(client, ActivityParser(SELECTORS), self.db, ACTIVITY_CONFIG)
        with redirect_stdout(io.StringIO()):
            try:
                return pipeline.fetch(follow_history=False, **kwargs), client, None
            except CircuitOpen as e:
                return None, client, e

    def attempts(self):
        return [r['fetch_attempts'] for r in self.db.query("SELECT fetch_attempts FROM auctions ORDER BY end_ts DESC")]

    def test_site_level_failures_stop_after_five_without_charging_attempts(self):
        stats, client, stopped = self.fetch([SiteUnavailable('http 403 from x', 403)])
        self.assertEqual(stopped.kind, 'site')
        self.assertEqual(len(client.requests), 5)
        self.assertEqual(set(self.attempts()), {0})
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM auctions WHERE fetch_error IS NOT NULL")[0]['n'], 0)

    def test_challenge_page_is_site_level(self):
        stats, client, stopped = self.fetch([CHALLENGE.decode()])
        self.assertEqual(stopped.kind, 'site')
        self.assertIn('challenge', str(stopped))
        self.assertEqual((len(client.requests), set(self.attempts())), (5, {0}))

    def test_a_success_resets_the_streak(self):
        blocked = SiteUnavailable('http 503', 503)
        stats, client, stopped = self.fetch([blocked] * 4 + [page_for] + [blocked] * 4 + [page_for], limit=10)
        self.assertIsNone(stopped)
        self.assertEqual((stats['fetched'], stats['failed']), (2, 8))

    def test_listing_level_failures_are_charged_and_stop_after_twenty(self):
        stats, client, stopped = self.fetch([HTTPStatusError(404, 'x')])
        self.assertEqual(stopped.kind, 'failures')
        self.assertEqual(len(client.requests), 20)
        self.assertEqual(self.attempts(), [1] * 20 + [0] * 20)

    def test_rate_limited_stops_at_once(self):
        client = FakeClient([RateLimited(2_000_000_000, 'asks for a 3600s pause', 429)])
        pipeline = ActivityPipeline(client, ActivityParser(SELECTORS), self.db, ACTIVITY_CONFIG)
        with redirect_stdout(io.StringIO()), self.assertRaises(RateLimited):
            pipeline.fetch(follow_history=False)
        self.assertEqual((len(client.requests), set(self.attempts())), (1, {0}))


class TestFetchSavesOnlyFinalData(unittest.TestCase):

    def setUp(self):
        self.db = ActivityDB(':memory:')
        self.db.upsert_summaries(summaries('https://bringatrailer.com', 6), now=1)

    def tearDown(self):
        self.db.close()

    def fetch(self, outcomes, **kwargs):
        self.client = FakeClient(outcomes)
        pipeline = ActivityPipeline(self.client, ActivityParser(SELECTORS), self.db, ACTIVITY_CONFIG)
        with redirect_stdout(io.StringIO()):
            try:
                return pipeline.fetch(**kwargs)
            except CircuitOpen as e:
                return e

    def row(self, listing_id):
        return self.db.query("SELECT * FROM auctions WHERE listing_id = ?", (listing_id,))[0]

    def test_a_url_serving_another_listing_is_an_error_not_a_save(self):
        self.fetch([listing_html(listing_id=9999, history='')], limit=1)
        row = self.row(5000)
        self.assertEqual((row['fetched_at'], row['fetch_attempts']), (None, 1))
        self.assertIn('serves listing 9999', row['fetch_error'])
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM auctions WHERE listing_id = 9999")[0]['n'], 0)
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM bids")[0]['n'], 0)

    def test_a_live_page_is_skipped_until_it_ends(self):
        live = lambda url: listing_html(listing_id=5000, ended=False, end_ts=None,
                                        result_text='Current Bid: <strong>USD $6,000</strong>')
        stats = self.fetch([live], limit=1)
        row = self.row(5000)
        self.assertEqual((stats['fetched'], stats['not_final'], stats['followed']), (0, 1, 0))
        self.assertEqual((row['fetched_at'], row['fetch_attempts'], row['fetch_error']), (None, 0, None))
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM listing_links")[0]['n'], 0)
        self.assertEqual(self.client.requests, ['https://bringatrailer.com/listing/car-0/'])

        stats = self.fetch([lambda url: listing_html(listing_id=5000)], limit=1)
        self.assertEqual(stats['fetched'], 1)
        self.assertIsNotNone(self.row(5000)['fetched_at'])

    def test_a_renamed_ended_marker_stops_the_run(self):
        renamed = lambda url: page_for(url).replace('listing-closed', 'listing-done').replace('listing-stats ended', 'listing-stats done')
        stopped = self.fetch([renamed])
        self.assertIsInstance(stopped, CircuitOpen)
        self.assertEqual(stopped.kind, 'unmarked')
        self.assertEqual(len(self.client.requests), 5)
        self.assertEqual({(r['fetched_at'], r['fetch_attempts']) for r in self.db.query("SELECT * FROM auctions")}, {(None, 0)})

    def test_layout_errors_are_recorded_without_using_attempts(self):
        broken = lambda url: page_for(url).replace('listing-available-info', 'listing-info-v2')
        stats = self.fetch([broken], limit=2)
        self.assertEqual((stats['fetched'], stats['failed']), (0, 2))
        rows = self.db.query("SELECT fetch_attempts, fetch_error FROM auctions WHERE fetch_error IS NOT NULL")
        self.assertEqual([r['fetch_attempts'] for r in rows], [0, 0])
        self.assertIn('has no result', rows[0]['fetch_error'])

    def test_twenty_layout_errors_in_a_row_stop_the_run(self):
        self.db.upsert_summaries(summaries('https://bringatrailer.com', 30), now=1)
        broken = lambda url: page_for(url).replace('class="essentials"', 'class="essentials-v2"')
        stopped = self.fetch([broken])
        self.assertEqual(stopped.kind, 'failures')
        self.assertEqual(len(self.client.requests), 20)
        self.assertEqual({r['fetch_attempts'] for r in self.db.query("SELECT fetch_attempts FROM auctions")}, {0})


class CliCase(unittest.TestCase):
    """the real cli, client, parser and database against a 127.0.0.1 server, with a fake clock for pacing"""

    def setUp(self):
        self.server = LocalServer()
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, 'activity.db')

        with open(REAL_CONFIG) as f:
            config = yaml.safe_load(f)
        config['site']['base_url'] = self.server.base_url
        config_path = os.path.join(self.tmp.name, 'bringatrailer.yaml')
        with open(config_path, 'w') as f:
            yaml.safe_dump(config, f)

        self.clock = FakeClock()
        for patcher in (mock.patch.object(cli, 'CONFIG_PATH', config_path),
                        mock.patch.object(http_client, 'time', self.clock)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.server.close()
        self.tmp.cleanup()

    def seed(self, n):
        db = ActivityDB(self.db_path)
        db.upsert_summaries(summaries(self.server.base_url, n), now=1)
        db.close()

    def serve_listings(self, n):
        for i in range(n):
            self.server.routes[f'/listing/car-{i}/'] = (200, {'Content-Type': 'text/html; charset=utf-8'},
                                                        listing_html(listing_id=5000 + i, history='').encode())

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            status = cli.main(['--db', self.db_path, *argv])
        return status, out.getvalue()

    def rows(self):
        db = ActivityDB(self.db_path)
        try:
            return [dict(r) for r in db.query("SELECT listing_id, fetched_at, fetch_attempts, fetch_error FROM auctions")]
        finally:
            db.close()


class TestCliExitCodes(CliCase):

    def test_five_403s_stop_the_run_and_exit_2(self):
        self.seed(30)
        self.server.default = (403, {}, b'blocked')
        status, out = self.run_cli('fetch')
        self.assertEqual(status, 2)
        self.assertEqual(len(self.server.hits), 5)
        self.assertEqual({(r['fetch_attempts'], r['fetch_error']) for r in self.rows()}, {(0, None)})
        self.assertIn('stopped: 5 site-level failures in a row', out)

    def test_challenge_200_exits_2(self):
        self.seed(10)
        self.server.default = (200, {'Content-Type': 'text/html'}, CHALLENGE)
        status, out = self.run_cli('fetch')
        self.assertEqual((status, len(self.server.hits)), (2, 5))
        self.assertEqual({r['fetch_attempts'] for r in self.rows()}, {0})

    def test_rate_limit_prints_when_to_resume(self):
        self.seed(10)
        self.server.default = (429, {'Retry-After': '3600'}, b'')
        status, out = self.run_cli('fetch')
        self.assertEqual((status, len(self.server.hits)), (2, 1))
        self.assertIn('resume after 2026-', out)
        self.assertIn('UTC', out)

    def test_nothing_fetched_and_something_failed_exits_1(self):
        self.seed(3)
        status, out = self.run_cli('fetch')
        self.assertEqual(status, 1)
        self.assertEqual({r['fetch_attempts'] for r in self.rows()}, {1})

    def test_success_exits_0(self):
        self.seed(3)
        self.serve_listings(3)
        status, out = self.run_cli('fetch')
        self.assertEqual(status, 0, out)
        self.assertTrue(all(r['fetched_at'] for r in self.rows()))

    def test_ctrl_c_exits_130(self):
        self.seed(3)
        with mock.patch.object(ActivityPipeline, 'fetch', side_effect=KeyboardInterrupt):
            status, out = self.run_cli('fetch')
        self.assertEqual(status, 130)

    def test_sync_stops_before_fetch_when_discovery_is_blocked(self):
        self.seed(3)
        self.server.default = (403, {}, b'blocked')
        status, out = self.run_cli('sync')
        self.assertEqual(status, 2)
        self.assertEqual(len(self.server.hits), 1)
        self.assertTrue(self.server.paths()[0].startswith('/wp-json/'))


class TestCliRedirects(CliCase):

    def test_history_link_that_redirects_is_saved_and_resolved(self):
        base = self.server.base_url
        old_url, new_url = f'{base}/listing/old-slug/', f'{base}/listing/new-slug/'
        history = f'''<div class="history"><div class="items">
            <a href="{old_url}" class="item"><div class="message"><em>Sold by x to y</em></div></a></div></div>'''
        self.seed(1)
        self.server.routes['/listing/car-0/'] = (200, {}, listing_html(listing_id=5000, history=history).encode())
        self.server.routes['/listing/old-slug/'] = (301, {'Location': '/listing/new-slug/'}, b'')
        self.server.routes['/listing/new-slug/'] = (200, {}, listing_html(listing_id=7000, history='').encode())

        status, out = self.run_cli('fetch')
        self.assertEqual(status, 0, out)
        self.assertEqual(self.server.paths(), ['/listing/car-0/', '/listing/old-slug/', '/listing/new-slug/'])
        db = ActivityDB(self.db_path)
        try:
            self.assertEqual(db.query("SELECT url FROM auctions WHERE listing_id = 7000")[0]['url'], new_url)
            self.assertEqual(db.query("SELECT related_listing_id FROM listing_links")[0]['related_listing_id'], 7000)
            self.assertEqual(db.pending_history(), [])
        finally:
            db.close()

        # a second run has nothing left to follow
        self.server.hits.clear()
        status, out = self.run_cli('fetch')
        self.assertEqual((status, self.server.hits), (0, []))


class TestCliUrlAndReset(CliCase):

    def test_bad_url_is_refused_before_any_request(self):
        for bad in ('https://evil.example/listing/x/', f'{self.server.base_url}/member/x/'):
            with redirect_stdout(io.StringIO()), mock.patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
                cli.main(['--db', self.db_path, 'fetch', '--url', bad])
        self.assertEqual(self.server.hits, [])

    def test_bare_slug_is_fetched_from_the_base_site(self):
        self.serve_listings(1)
        status, out = self.run_cli('fetch', '--url', 'car-0')
        self.assertEqual(status, 0, out)
        self.assertEqual(self.server.paths(), ['/listing/car-0/'])

    def test_reset_errors_requeues_listings_that_ran_out_of_attempts(self):
        self.seed(3)
        db = ActivityDB(self.db_path)
        for _ in range(3):
            db.mark_error(5000, 'http 404')
        db.save_detail(ActivityParser(SELECTORS).parse_listing(listing_html(listing_id=5001, history=''), 'u'), 1, 3)
        db.mark_error(5001, 'fetched rows keep their attempts')
        self.assertEqual([r['listing_id'] for r in db.pending()], [5002])
        db.close()

        status, out = self.run_cli('reset-errors')
        self.assertEqual(status, 0)
        self.assertIn('http 404', out)
        rows = {r['listing_id']: r for r in self.rows()}
        self.assertEqual((rows[5000]['fetch_attempts'], rows[5000]['fetch_error']), (0, None))
        self.assertEqual(rows[5001]['fetch_attempts'], 1)


if __name__ == '__main__':
    unittest.main()
