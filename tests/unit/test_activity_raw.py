import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from test_activity_parser import SELECTORS
from test_activity_pipeline import ACTIVITY_CONFIG, CliCase, FakeClient, page_for, summaries
from pipelines.activity_pipeline import ActivityPipeline
from sites.bringatrailer.activity_parser import ActivityParser
from storage.activity_db import ActivityDB
from storage.raw_store import RawStore, raw_db_path


ROW = """SELECT listing_id, url, title, make, model, seller_slug, seller_type, location, result, high_bid, end_ts,
                winner_slug, high_bidder_slug, n_bids, n_comments, chassis, vin, fetched_at, parser_version
         FROM auctions ORDER BY listing_id"""


class RawCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = ActivityDB(os.path.join(self.tmp.name, 'activity.db'))
        self.raw = RawStore(raw_db_path(os.path.join(self.tmp.name, 'activity.db')))
        self.db.upsert_summaries(summaries('https://bringatrailer.com', 3), now=1)
        self.parser = ActivityParser(SELECTORS)

    def tearDown(self):
        self.db.close()
        self.raw.close()
        self.tmp.cleanup()

    def pipeline(self, outcomes=(page_for,)):
        self.client = FakeClient(outcomes)
        return ActivityPipeline(self.client, self.parser, self.db, ACTIVITY_CONFIG, raw_store=self.raw)

    def rows(self):
        return [tuple(r) for r in self.db.query(ROW)]

    def quiet(self, fn, *args, **kwargs):
        with redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs)


class TestRawStore(RawCase):

    def test_raw_db_sits_next_to_the_database(self):
        self.assertEqual(raw_db_path('data/db/bat_activity.db'), 'data/db/bat_activity_raw.db')
        self.assertIsNone(raw_db_path(':memory:'))

    def test_fetch_keeps_fragments_that_reparse_the_same(self):
        self.quiet(self.pipeline().fetch, follow_history=False)
        stats = self.raw.stats()
        self.assertEqual(list(stats), ['fragments'])
        self.assertEqual(stats['fragments']['pages'], 3)

        stored = self.raw.get(5000)
        self.assertEqual(self.parser.parse_listing(stored.html, stored.url, now=stored.fetched_at),
                         self.parser.parse_listing(page_for(stored.url), stored.url, now=stored.fetched_at))
        self.assertLess(len(stored.html), len(page_for(stored.url)))

    def test_whole_page_is_kept_when_fragments_miss_something(self):
        # a fragment list that loses the group links would lose make and model on reparse
        dropped = lambda soup, vms: ActivityParser._fragments(self.parser, soup, vms).replace('group-link', 'x')
        with mock.patch.object(self.parser, '_fragments', dropped):
            self.quiet(self.pipeline().fetch, follow_history=False, limit=1)
        stored = self.raw.get(5000)
        self.assertEqual(stored.kind, 'page')
        self.assertEqual(stored.html, page_for(stored.url))

    def test_reparse_gives_the_same_rows(self):
        self.quiet(self.pipeline().fetch, follow_history=False)
        before = self.rows()
        stats = self.quiet(ActivityPipeline(None, self.parser, self.db, ACTIVITY_CONFIG, raw_store=self.raw).reparse)
        self.assertEqual((stats['reparsed'], stats['failed']), (3, 0))
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM bids")[0]['n'], 12)

    def test_reparse_repairs_rows_offline_and_keeps_fetch_times(self):
        self.quiet(self.pipeline().fetch, follow_history=False)
        before = self.rows()
        with self.db.conn:
            self.db.conn.execute("UPDATE auctions SET location = 'somewhere wrong', parser_version = 1")
            self.db.conn.execute("DELETE FROM bids WHERE listing_id = 5001")

        pipeline = ActivityPipeline(None, self.parser, self.db, ACTIVITY_CONFIG, raw_store=self.raw)
        self.quiet(pipeline.reparse, [5001])
        rows = {r[0]: r for r in self.rows()}
        self.assertEqual(rows[5001], before[1])
        self.assertEqual(rows[5000][7], 'somewhere wrong')
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM bids WHERE listing_id = 5001")[0]['n'], 4)


class TestCliReparseAndScopedFetch(CliCase):

    def age_fetches(self, seconds=100):
        """as if the fetches happened a while ago, so a scope requested now is clearly later"""
        db = ActivityDB(self.db_path)
        with db.conn:
            db.conn.execute("UPDATE auctions SET fetched_at = fetched_at - ?", (seconds,))
        db.close()

    def test_reparse_command(self):
        self.seed(2)
        self.serve_listings(2)
        self.assertEqual(self.run_cli('fetch')[0], 0)
        status, out = self.run_cli('reparse')
        self.assertEqual(status, 0, out)
        self.assertIn('2 listing(s) reparsed, 0 failed', out)

        status, out = self.run_cli('report')
        self.assertIn('stored pages        : 2 fragments', out)

    def test_reparse_without_stored_pages(self):
        status, out = self.run_cli('reparse')
        self.assertEqual(status, 1)
        self.assertIn('no stored pages', out)

    def test_scoped_refetch_resumes_after_an_interruption(self):
        self.seed(3)
        self.serve_listings(3)
        self.assertEqual(self.run_cli('fetch')[0], 0)
        self.age_fetches()

        # the first scoped run gets car-0 again, then the site stops answering for the rest
        self.server.hits.clear()
        self.server.routes['/listing/car-1/'] = self.server.routes['/listing/car-2/'] = (503, {}, b'')
        status, out = self.run_cli('fetch', '--where', 'listing_id IN (5000, 5001, 5002)', '--max-attempts', '9')
        self.assertIn('3 listing(s) in scope, 3 newly queued', out)
        self.assertEqual(sorted(set(self.server.paths())), ['/listing/car-0/', '/listing/car-1/', '/listing/car-2/'])

        # the rerun picks up only what's left
        self.serve_listings(3)
        self.server.hits.clear()
        status, out = self.run_cli('fetch', '--where', 'listing_id IN (5000, 5001, 5002)', '--max-attempts', '9')
        self.assertEqual(status, 0, out)
        self.assertIn('3 listing(s) in scope, 0 newly queued', out)
        self.assertEqual(self.server.paths(), ['/listing/car-1/', '/listing/car-2/'])

        # finished, so the same scope later means a fresh re-fetch of all of it
        self.age_fetches()
        status, out = self.run_cli('fetch', '--where', 'listing_id IN (5000, 5001, 5002)')
        self.assertIn('3 newly queued', out)

    def test_ids_from_file(self):
        self.seed(3)
        self.serve_listings(3)
        self.assertEqual(self.run_cli('fetch')[0], 0)
        self.age_fetches()
        path = os.path.join(self.tmp.name, 'ids.txt')
        with open(path, 'w') as f:
            f.write('# listings to look at again\n5002\n\n')
        self.server.hits.clear()
        status, out = self.run_cli('fetch', '--ids-from', path)
        self.assertEqual((status, self.server.paths()), (0, ['/listing/car-2/']))

    def test_bad_where_is_an_argument_error(self):
        with redirect_stdout(io.StringIO()), mock.patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
            self.run_cli('fetch', '--where', 'no_such_column = 1')


if __name__ == '__main__':
    unittest.main()
