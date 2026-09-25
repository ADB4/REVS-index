import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from test_activity_db import SAVED, detail, summary, url
import cli.commands.activity as cli
from core.models.activity import HistoryLink
from storage.activity_db import ActivityDB


VIN = 'WBSBR934X2EX23144'


class ReportCase(unittest.TestCase):
    """a small database: one bmw sold three times (the middle auction discovered but not fetched yet), two
    porsches and a jaguar sharing a chassis number, and a win with no parsed bid from the winner"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, 'activity.db')
        db = ActivityDB(self.db_path)
        db.upsert_summaries([summary(i, end_ts=1_000_000 * i) for i in (1, 2, 3)], now=1)

        first = detail(1, 'alice', [('bob', 100), ('carol', 90)], vin=VIN, end_ts=1_000_000)
        last = detail(3, 'bob', [('dave', 300), ('carol', 250)], vin=VIN, end_ts=3_000_000)
        last.history = [HistoryLink(url=url(1), end_ts=1_000_000), HistoryLink(url=url(2), end_ts=2_000_000)]
        porsche = detail(4, 'erin', [('carol', 50)], make='Porsche', chassis='9113101234', end_ts=4_000_000)
        porsche_again = detail(5, 'erin', [('frank', 60)], make='Porsche', chassis='9113101234', end_ts=5_000_000)
        jaguar = detail(6, 'gina', [('carol', 70)], make='Jaguar', chassis='9113101234', end_ts=6_000_000)
        for d in (first, last, porsche, porsche_again, jaguar):
            db.save_detail(d, now=SAVED, parser_version=3)
        # frank is recorded as the winner of the jaguar without a bid of his there
        with db.conn:
            db.conn.execute("UPDATE auctions SET winner_slug = 'frank' WHERE listing_id = 6")
        db.rebuild_participants()
        db.rebuild_vehicles()
        db.close()

    def tearDown(self):
        self.tmp.cleanup()

    def report(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            status = cli.main(['--db', self.db_path, 'report', *argv])
        return status, out.getvalue()

    def refused(self, *argv):
        with redirect_stdout(io.StringIO()), mock.patch('sys.stderr', io.StringIO()) as err, \
                self.assertRaises(SystemExit):
            cli.main(['--db', self.db_path, 'report', *argv])
        return err.getvalue()


class TestReportModes(ReportCase):

    def test_modes_are_exclusive(self):
        self.assertIn('not allowed with', self.refused('--member', 'bob', '--pairs'))
        self.assertIn('not allowed with', self.refused('--vehicle', VIN, '--resales'))

    def test_filters_that_dont_apply_are_refused(self):
        self.assertIn("don't apply to --member", self.refused('--member', 'bob', '--model', 'bmw'))
        self.assertIn("don't apply to --member", self.refused('--vehicle', VIN, '--since', '2025-01-01'))
        self.assertIn('only applies to --pairs and --resales', self.refused('--min-auctions', '2'))

    def test_a_filtered_report_is_scoped_and_leaves_out_the_overview(self):
        status, out = self.report('--model', 'porsche')
        self.assertEqual(status, 0)
        self.assertNotIn('bat activity database', out)
        self.assertIn('top sellers (porsche)', out)
        self.assertIn('Erin', out)
        self.assertNotIn('Alice', out)
        self.assertIn('bat activity database', self.report()[1])

    def test_pairs_apply_the_model_filter(self):
        status, out = self.report('--pairs', '--min-auctions', '1', '--model', 'jaguar')
        self.assertIn('repeat seller/bidder pairs (>= 1 auctions together, jaguar)', out)
        self.assertIn('Gina', out)
        self.assertNotIn('Alice', out)
        status, out = self.report('--pairs', '--min-auctions', '1')
        self.assertIn('Alice', out)


class TestVehicleReport(ReportCase):

    def test_an_unfetched_auction_between_two_shows_and_makes_the_label_provisional(self):
        status, out = self.report('--vehicle', VIN)
        self.assertEqual(status, 0)
        self.assertIn('2 of 3 known auction(s) fetched', out)
        self.assertIn('auctions of this car not fetched yet', out)
        self.assertIn(url(2), out)
        history = out.split('auction history')[1].split('auctions of this car')[0]
        lines = [l for l in history.splitlines() if l.strip().startswith('1970')]
        self.assertIn('first seen', lines[0])
        self.assertNotIn('provisional', lines[0])
        self.assertIn('resold by buyer (provisional)', lines[1])

    def test_a_chassis_shared_by_several_cars_lists_them(self):
        status, out = self.report('--vehicle', '911 310 1234')
        self.assertIn("matches 2 cars; pick one by listing url or listing id", out)
        self.assertIn('car 4', out)
        self.assertIn('car 6', out)
        self.assertNotIn('auction history', out)
        status, out = self.report('--vehicle', '6')
        self.assertIn('1 of 1 known auction(s) fetched', out)


class TestMemberReport(ReportCase):

    def test_win_rate_counts_the_auctions_bid_on(self):
        status, out = self.report('--member', 'carol')
        self.assertEqual(status, 0)
        self.assertIn('bidding      : 4 bids across 4 auctions', out)
        # the porsche she was the only bidder on
        self.assertIn('winning      : 1 won (25% of auctions bid), $50 spent', out)
        # one win with his bid, one recorded without: the rate only counts the auction he bid on
        status, out = self.report('--member', 'frank')
        self.assertIn('winning      : 2 won (100% of auctions bid), $130 spent; 1 of them without a bid of theirs', out)

    def test_a_seller_profile(self):
        status, out = self.report('--member', 'bob')
        self.assertIn('selling      : 1 listed, 1 sold', out)
        self.assertIn('who bids on their cars', out)
        self.assertIn('Dave', out)
        self.assertIn('cars they won and later resold on bat', out)

    def test_unknown_member(self):
        self.assertIn("no member 'nobody'", self.report('--member', 'nobody')[1])


class TestLink(ReportCase):

    def test_link_rebuilds_participants(self):
        db = ActivityDB(self.db_path)
        with db.conn:
            db.conn.execute("DELETE FROM participants")
        db.close()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(['--db', self.db_path, 'link']), 0)
        self.assertIn('7 bidder/auction rows rebuilt', out.getvalue())


if __name__ == '__main__':
    unittest.main()
