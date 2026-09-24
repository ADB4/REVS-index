import io
import os
import sys
import json
import sqlite3
import subprocess
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from test_activity_parser import SELECTORS, listing_html
from test_activity_pipeline import ACTIVITY_CONFIG, CliCase, FakeClient, page_for, summaries
import cli.commands.activity as cli
from pipelines.activity_pipeline import ActivityPipeline
from sites.bringatrailer.activity_parser import ActivityParser
from sites.bringatrailer.http_client import HTTPStatusError, SiteUnavailable
from storage.activity_db import ActivityDB


PER_PAGE = 60


class FakeFeed:
    """the results api over a list of auctions, newest first, like sort=td; new ones push the rest down a page"""

    def __init__(self):
        self.items = []
        self.next_id = 1
        self.now = 1_700_000_000
        self.fail = None        # (page, exception) to raise when that page is asked for
        self.pages_total = True
        self.past_end = None    # an exception for pages past the last one, instead of an empty list
        self.requests = []
        self.request_count = 0

    def end_auctions(self, n, per_day=180):
        new = []
        for _ in range(n):
            self.now += 86400 // per_day
            new.append({'id': self.next_id, 'url': f'https://bringatrailer.com/listing/car-{self.next_id}/',
                        'title': f'2001 Car {self.next_id}', 'sold_text': 'Sold for USD $1,000',
                        'currency': 'USD', 'timestamp_end': self.now})
            self.next_id += 1
        self.items[:0] = reversed(new)

    def get_json(self, path, params=None):
        page = params['page']
        self.requests.append(page)
        self.request_count += 1
        if self.fail and self.fail[0] == page:
            raise self.fail[1]
        chunk = self.items[(page - 1) * PER_PAGE: page * PER_PAGE]
        if not chunk and self.past_end:
            raise self.past_end
        data = {'items': chunk, 'items_total': len(self.items)}
        if self.pages_total:
            data['pages_total'] = -(-len(self.items) // PER_PAGE)
        return data


class DiscoverCase(unittest.TestCase):

    def setUp(self):
        self.feed = FakeFeed()
        self.db = ActivityDB(':memory:')
        self.pipeline = ActivityPipeline(self.feed, ActivityParser(SELECTORS), self.db,
                                         dict(ACTIVITY_CONFIG, results_per_page=PER_PAGE))

    def tearDown(self):
        self.db.close()

    def discover(self, **kwargs):
        with redirect_stdout(io.StringIO()):
            try:
                return self.pipeline.discover(**kwargs)
            except (KeyboardInterrupt, SiteUnavailable) as e:
                return e

    def missing(self):
        known = {r['listing_id'] for r in self.db.query("SELECT listing_id FROM auctions")}
        return [item['id'] for item in self.feed.items if item['id'] not in known]


class TestIncrementalDiscover(DiscoverCase):

    def backfilled(self):
        self.feed.end_auctions(20 * PER_PAGE)
        self.discover(backfill=True)
        self.assertEqual(self.missing(), [])

    def test_an_interrupted_run_leaves_no_hole(self):
        self.backfilled()
        # a dozen days of results pile up, and the first sync is cut short on page 11 of about 36
        self.feed.end_auctions(12 * 180)
        self.feed.fail = (11, KeyboardInterrupt())
        self.assertIsInstance(self.discover(), KeyboardInterrupt)
        self.assertGreater(len(self.missing()), 1000)

        self.feed.fail = None
        self.feed.end_auctions(170)
        self.discover()
        self.assertEqual(self.missing(), [])

    def test_a_blocked_run_leaves_no_hole(self):
        self.backfilled()
        self.feed.end_auctions(12 * 180)
        self.feed.fail = (11, SiteUnavailable('http 403', 403))
        self.discover()
        self.feed.fail = None
        self.feed.end_auctions(170)
        self.discover()
        self.assertEqual(self.missing(), [])

    def test_a_max_pages_run_leaves_no_hole(self):
        self.backfilled()
        self.feed.end_auctions(12 * 180)
        self.discover(max_pages=10)
        self.feed.end_auctions(170)
        self.discover()
        self.assertEqual(self.missing(), [])

    def test_a_daily_run_stops_soon_after_the_overlap(self):
        self.backfilled()
        self.feed.end_auctions(170)
        self.discover()
        self.feed.end_auctions(170)
        self.feed.requests.clear()
        self.discover()
        self.assertEqual(self.missing(), [])
        # 3 pages of new results, then the 2 known pages the stop rule wants
        self.assertLessEqual(len(self.feed.requests), 6)

    def test_the_backfill_seeds_the_watermark(self):
        self.feed.end_auctions(3 * PER_PAGE)
        self.discover(backfill=True)
        self.assertEqual(int(self.db.get_meta('discover_watermark')), self.feed.items[0]['timestamp_end'])

    def test_the_watermark_only_moves_after_a_complete_run(self):
        self.backfilled()
        mark = self.db.get_meta('discover_watermark')
        self.feed.end_auctions(5 * PER_PAGE)
        self.discover(max_pages=2)
        self.assertEqual(self.db.get_meta('discover_watermark'), mark)
        self.discover()
        self.assertEqual(int(self.db.get_meta('discover_watermark')), self.feed.items[0]['timestamp_end'])


class TestBackfillCursor(DiscoverCase):

    def test_resumes_where_it_stopped(self):
        self.feed.end_auctions(10 * PER_PAGE)
        self.discover(backfill=True, max_pages=4)
        self.assertEqual(self.db.get_meta('backfill_next_page'), '5')
        self.feed.requests.clear()
        self.discover(backfill=True)
        self.assertEqual(self.feed.requests, list(range(5, 11)))
        self.assertEqual(self.missing(), [])

    def test_a_start_page_past_the_cursor_skips_nothing(self):
        self.feed.end_auctions(10 * PER_PAGE)
        self.discover(backfill=True, max_pages=2)
        self.discover(backfill=True, start_page=6, max_pages=2)
        self.assertEqual(self.db.get_meta('backfill_next_page'), '3')
        self.discover(backfill=True)
        self.assertEqual(self.missing(), [])

    def test_a_start_page_before_the_cursor_carries_it_on(self):
        self.feed.end_auctions(10 * PER_PAGE)
        self.discover(backfill=True, max_pages=4)
        self.discover(backfill=True, start_page=3, max_pages=4)
        self.assertEqual(self.db.get_meta('backfill_next_page'), '7')

    def test_without_pages_total_it_walks_until_a_page_is_empty(self):
        self.feed.end_auctions(3 * PER_PAGE + 5)
        self.feed.pages_total = False
        self.discover(backfill=True)
        self.assertEqual((self.feed.requests, self.missing()), ([1, 2, 3, 4, 5], []))

    def test_a_400_past_the_last_page_means_done(self):
        self.feed.end_auctions(2 * PER_PAGE)
        self.feed.pages_total = False
        self.feed.past_end = HTTPStatusError(400, 'x')
        stats = self.discover(backfill=True)
        self.assertEqual((stats['pages'], stats['complete'], self.missing()), (2, True, []))

    def test_reset_cursor_flag(self):
        self.feed.end_auctions(3 * PER_PAGE)
        self.discover(backfill=True)
        args = cli.argparse.Namespace(reset_backfill_cursor=True, start_page=None, max_pages=1, since=None,
                                      stop_after_known=2, backfill=True)
        self.feed.requests.clear()
        with redirect_stdout(io.StringIO()):
            cli.run_discover(args, self.pipeline)
        self.assertEqual(self.feed.requests, [1])


class TestFetchBudget(unittest.TestCase):

    def setUp(self):
        self.db = ActivityDB(':memory:')
        self.db.upsert_summaries(summaries('https://bringatrailer.com', 10), now=1)

    def tearDown(self):
        self.db.close()

    def history(self, *slugs):
        items = ''.join(f'<a href="https://bringatrailer.com/listing/{s}/" class="item"><div class="message">'
                        f'<em>Sold by a to b</em></div></a>' for s in slugs)
        return f'<div class="history"><div class="items">{items}</div></div>'

    def serve(self, url):
        slug = url.rstrip('/').rsplit('/', 1)[1]
        if slug == 'car-0':
            return listing_html(listing_id=5000, history=self.history('old-a', 'old-b'))
        if slug.startswith('old-'):
            return listing_html(listing_id=9000 + ord(slug[-1]), history='')
        return page_for(url)

    def fetch(self, **kwargs):
        client = FakeClient([self.serve])
        pipeline = ActivityPipeline(client, ActivityParser(SELECTORS), self.db, ACTIVITY_CONFIG)
        out = io.StringIO()
        with redirect_stdout(out):
            stats = pipeline.fetch(**kwargs)
        return stats, client, out.getvalue()

    def test_history_links_are_fetched_within_the_limit(self):
        stats, client, out = self.fetch(limit=3)
        self.assertEqual([u.rsplit('/', 2)[1] for u in client.requests], ['car-0', 'old-a', 'old-b'])
        self.assertEqual((stats['fetched'], stats['followed']), (3, 2))
        self.assertIn('10 auction(s) waiting', out)
        self.assertIn('this run fetches up to 3', out)

    def test_leftover_history_links_go_first(self):
        self.fetch(limit=1)
        stats, client, out = self.fetch(limit=2)
        self.assertEqual([u.rsplit('/', 2)[1] for u in client.requests], ['old-a', 'old-b'])
        self.assertEqual(stats['followed'], 2)

    def test_a_bid_count_mismatch_is_recorded_on_the_saved_row(self):
        miscounted = lambda url: listing_html(listing_id=5000, history='').replace(
            'number-bids-value">4<', 'number-bids-value">400<')
        client = FakeClient([miscounted])
        with redirect_stdout(io.StringIO()):
            ActivityPipeline(client, ActivityParser(SELECTORS), self.db, ACTIVITY_CONFIG).fetch(limit=1)
        row = self.db.query("SELECT fetched_at, fetch_error FROM auctions WHERE listing_id = 5000")[0]
        self.assertIsNotNone(row['fetched_at'])
        self.assertEqual(row['fetch_error'], 'bid count mismatch: page says 400, parsed 4')
        self.assertEqual(self.db.listing_ids_where(cli.MISMATCH_WHERE), [5000])

    def test_a_database_error_while_recording_a_failure_doesnt_end_the_run(self):
        client = FakeClient([HTTPStatusError(404, 'x'), page_for])
        pipeline = ActivityPipeline(client, ActivityParser(SELECTORS), self.db, ACTIVITY_CONFIG)
        out = io.StringIO()
        with mock.patch.object(self.db, 'mark_error', side_effect=sqlite3.OperationalError('database is locked')), \
                redirect_stdout(out):
            stats = pipeline.fetch(limit=2, follow_history=False)
        self.assertEqual((stats['failed'], stats['fetched']), (1, 1))
        self.assertIn("couldn't record that failure", out.getvalue())


class TestCliConcurrencyAndScope(CliCase):

    def test_two_runs_cant_share_a_database(self):
        self.seed(3)
        self.serve_listings(3)
        holder = subprocess.Popen([sys.executable, '-c', textwrap.dedent(f"""
            import fcntl, sys, time
            f = open({self.db_path + '.lock'!r}, 'a')
            fcntl.flock(f, fcntl.LOCK_EX)
            print('locked', flush=True)
            time.sleep(30)
        """)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), 'locked')
            for command in ('fetch', 'discover', 'sync', 'link', 'reset-errors'):
                status, out = self.run_cli(command)
                self.assertEqual(status, cli.EXIT_LOCKED, command)
                self.assertIn('another run is using', out)
            self.assertEqual(self.server.hits, [])
            # reading needs no lock
            self.assertEqual(self.run_cli('report')[0], 0)
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()
        self.assertEqual(self.run_cli('fetch')[0], 0)

    def test_sync_fetches_what_it_discovered_not_the_backlog(self):
        base = self.server.base_url
        # an old backlog nobody has fetched yet, then one new result on the feed
        db = ActivityDB(self.db_path)
        db.upsert_summaries(summaries(base, 3), now=1)
        db.close()
        self.serve_listings(4)
        item = {'id': 5003, 'url': f'{base}/listing/car-3/', 'title': 'car 3', 'sold_text': 'Sold for USD $25,500',
                'timestamp_end': 1_767_210_841}
        self.server.routes['/wp-json/bringatrailer/1.0/data/listings-filter'] = (
            200, {'Content-Type': 'application/json'},
            json.dumps({'items': [item], 'pages_total': 1}).encode())

        status, out = self.run_cli('sync')
        self.assertEqual(status, 0, out)
        self.assertEqual([p for p in self.site_paths() if p.startswith('/listing/')], ['/listing/car-3/'])

        self.server.hits.clear()
        status, out = self.run_cli('sync', '--all')
        self.assertEqual(sorted(p for p in self.site_paths() if p.startswith('/listing/')),
                         ['/listing/car-0/', '/listing/car-1/', '/listing/car-2/'])

    def test_recheck_mismatches(self):
        self.seed(2)
        self.serve_listings(2)
        miscounted = listing_html(listing_id=5001, history='').replace('number-bids-value">4<', 'number-bids-value">5<')
        self.server.routes['/listing/car-1/'] = (200, {}, miscounted.encode())
        self.assertEqual(self.run_cli('fetch')[0], 0)
        db = ActivityDB(self.db_path)
        with db.conn:
            db.conn.execute("UPDATE auctions SET fetched_at = fetched_at - 100")
        db.close()

        self.server.hits.clear()
        status, out = self.run_cli('fetch', '--recheck-mismatches')
        self.assertEqual((status, self.site_paths()), (0, ['/listing/car-1/']))
        self.assertIn('1 listing(s) in scope', out)

    def test_bad_numbers_and_dates_are_argument_errors(self):
        for argv in (('fetch', '--limit', '0'), ('fetch', '--max-attempts', '-1'), ('discover', '--max-pages', '0'),
                     ('discover', '--stop-after-known', 'x'), ('fetch', '--since', '2025-13-01'),
                     ('report', '--since', 'yesterday'), ('fetch', '--delay', '-1')):
            with redirect_stdout(io.StringIO()), mock.patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
                self.run_cli(*argv)
        self.assertEqual(self.server.hits, [])


if __name__ == '__main__':
    unittest.main()
