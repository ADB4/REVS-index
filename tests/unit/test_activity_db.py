import unittest
import sqlite3
import tempfile
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from core.models.activity import AuctionSummary, AuctionDetail, Bid, Member, HistoryLink
from storage.activity_db import ActivityDB


PV = 2


def url(listing_id):
    return f"https://bringatrailer.com/listing/car-{listing_id}/"


def summary(listing_id, result='sold', high_bid=10000, end_ts=1000):
    return AuctionSummary(
        listing_id=listing_id,
        url=url(listing_id),
        title=f"car {listing_id}",
        result=result,
        high_bid=high_bid,
        currency='USD',
        end_ts=end_ts
    )


def detail(listing_id, seller, bids, result='sold', winner=None, make='BMW', end_ts=None,
           vin=None, chassis=None, history=()):
    members = {slug: Member(slug=slug, display_name=slug.title()) for slug in {seller, *(b[0] for b in bids)}}
    bid_objs = [Bid(bid_id=listing_id * 100 + i, bidder=members[slug], amount=amt, ts=i) for i, (slug, amt) in enumerate(bids)]
    top = max(bid_objs, key=lambda b: b.amount) if bid_objs else None
    return AuctionDetail(
        listing_id=listing_id,
        url=url(listing_id),
        title=f"car {listing_id}",
        result=result,
        high_bid=top.amount if top else None,
        currency='USD',
        end_ts=end_ts if end_ts is not None else 1000 + listing_id,
        seller=members[seller],
        make=make,
        model_slug=f"{make.lower()}/model",
        chassis=chassis or vin,
        vin=vin,
        history=[HistoryLink(url=u) for u in history],
        high_bidder=top.bidder if top else None,
        winner=members[winner] if winner else (top.bidder if top and result == 'sold' else None),
        bids=bid_objs
    )


class TestActivityDB(unittest.TestCase):

    def setUp(self):
        self.db = ActivityDB(':memory:')

    def tearDown(self):
        self.db.close()

    def test_upsert_summaries_counts_only_new(self):
        self.assertEqual(self.db.upsert_summaries([summary(1), summary(2)], now=1), 2)
        self.assertEqual(self.db.upsert_summaries([summary(2), summary(3)], now=2), 1)

    def test_pending_and_errors(self):
        self.db.upsert_summaries([summary(1, end_ts=10), summary(2, end_ts=20)], now=1)
        self.assertEqual([r['listing_id'] for r in self.db.pending()], [2, 1])

        self.db.save_detail(detail(2, 'dealer', [('alice', 5000)]), now=5, parser_version=PV)
        for _ in range(3):
            self.db.mark_error(1, 'http 404')
        self.assertEqual(self.db.pending(), [])

    def test_upgrade_requeues_older_parser_versions(self):
        self.db.upsert_summaries([summary(1), summary(2)], now=1)
        self.db.save_detail(detail(1, 'dealer', []), now=2, parser_version=1)
        self.db.save_detail(detail(2, 'dealer', []), now=2, parser_version=2)

        self.assertEqual(self.db.pending(), [])
        self.assertEqual([r['listing_id'] for r in self.db.pending(upgrade_below=2)], [1])

    def test_refetch_replaces_bids(self):
        self.db.upsert_summaries([summary(1)], now=1)
        self.db.save_detail(detail(1, 'dealer', [('alice', 1000), ('bob', 2000)]), now=2, parser_version=PV)
        self.db.save_detail(detail(1, 'dealer', [('alice', 1000)]), now=3, parser_version=PV)
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM bids")[0]['n'], 1)

    def test_member_activity_rollup(self):
        self.db.upsert_summaries([summary(i) for i in (1, 2, 3)], now=1)
        self.db.save_detail(detail(1, 'dealer', [('alice', 1000), ('bob', 2000), ('alice', 3000)]), now=2, parser_version=PV)
        self.db.save_detail(detail(2, 'dealer', [('bob', 5000)]), now=2, parser_version=PV)
        self.db.save_detail(detail(3, 'alice', [('bob', 900)], result='reserve_not_met'), now=2, parser_version=PV)

        rows = {r['slug']: r for r in self.db.query("SELECT * FROM member_activity")}

        self.assertEqual((rows['dealer']['listed'], rows['dealer']['sold'], rows['dealer']['sold_usd']), (2, 2, 8000))
        self.assertEqual((rows['alice']['listed'], rows['alice']['sold']), (1, 0))
        self.assertEqual((rows['alice']['auctions_bid'], rows['alice']['bids'], rows['alice']['won']), (1, 2, 1))
        self.assertEqual((rows['bob']['auctions_bid'], rows['bob']['won'], rows['bob']['won_usd']), (3, 1, 5000))
        self.assertAlmostEqual(rows['bob']['win_rate'], 0.333)

        pairs = {(r['seller_slug'], r['bidder_slug']): r for r in self.db.query("SELECT * FROM seller_bidder_pairs")}
        self.assertEqual((pairs[('dealer', 'bob')]['auctions_bid'], pairs[('dealer', 'bob')]['won']), (2, 1))
        self.assertEqual(pairs[('alice', 'bob')]['won'], 0)

    def test_meta_roundtrip(self):
        self.assertIsNone(self.db.get_meta('backfill_next_page'))
        self.db.set_meta('backfill_next_page', '12')
        self.db.set_meta('backfill_next_page', '13')
        self.assertEqual(self.db.get_meta('backfill_next_page'), '13')


class TestVehicleTracking(unittest.TestCase):

    VIN = 'WBSBR934X2EX23144'

    def setUp(self):
        self.db = ActivityDB(':memory:')

    def tearDown(self):
        self.db.close()

    def save(self, *details):
        for d in details:
            self.db.save_detail(d, now=1, parser_version=PV)
        return self.db.rebuild_vehicles()

    def vehicle_ids(self):
        return {r['listing_id']: r['vehicle_id'] for r in self.db.query("SELECT listing_id, vehicle_id FROM auctions")}

    def test_same_vin_is_one_vehicle(self):
        stats = self.save(
            detail(10, 'willousb', [('niacc', 13000)], vin=self.VIN),
            detail(20, 'niacc', [('drc354', 13500)], vin=self.VIN),
            detail(30, 'other', [('x', 1)], vin='WBSBL93453JR22502')
        )
        self.assertEqual(self.vehicle_ids(), {10: 10, 20: 10, 30: 30})
        self.assertEqual((stats['vehicles'], stats['repeat_vehicles'], stats['vin_conflicts']), (2, 1, 0))

    def test_short_chassis_only_matches_within_make(self):
        self.save(
            detail(1, 'a', [], make='Porsche', chassis='9113101234'),
            detail(2, 'b', [], make='Porsche', chassis='9113101234'),
            detail(3, 'c', [], make='Jaguar', chassis='9113101234')
        )
        ids = self.vehicle_ids()
        self.assertEqual(ids[1], ids[2])
        self.assertNotEqual(ids[1], ids[3])

    def test_history_links_join_listings_with_different_chassis(self):
        # bat history knows it's the same car even though the vin was typed differently
        stats = self.save(
            detail(1, 'a', [('b', 100)], vin=self.VIN),
            detail(2, 'b', [('c', 200)], vin='WBSBR934X2EX23145', history=[url(1)])
        )
        self.assertEqual(self.vehicle_ids(), {1: 1, 2: 1})
        self.assertEqual(stats['vin_conflicts'], 1)

    def test_history_link_resolves_when_target_is_saved_later(self):
        self.save(detail(2, 'b', [('c', 200)], history=[url(1)]))
        self.assertEqual([r['url'] for r in self.db.pending_history()], [url(1)])
        self.assertTrue(self.db.needs_fetch(url(1)))

        self.save(detail(1, 'a', [('b', 100)]))
        self.assertEqual(self.db.pending_history(), [])
        self.assertFalse(self.db.needs_fetch(url(1)))
        self.assertEqual(self.vehicle_ids(), {1: 1, 2: 1})

    def test_failed_history_follows_stop_after_max_attempts(self):
        self.save(detail(2, 'b', [], history=[url(1)]))
        for _ in range(3):
            self.db.mark_url_error(url(1), 'http 404')
        self.assertEqual(self.db.pending_history(max_attempts=3), [])

    def test_listings_without_identity_are_not_vehicles(self):
        self.save(detail(1, 'a', [], make='Parts and Automobilia'))
        self.assertEqual(self.vehicle_ids(), {1: None})

    def test_timeline_and_resales(self):
        # the real chain of one e46 m3 convertible on bat
        self.save(
            detail(1, 'willousb', [('flsandman', 12750)], vin=self.VIN, end_ts=1441830117),
            detail(2, 'willousb', [('niacc', 13000)], vin=self.VIN, end_ts=1442265300),
            detail(3, 'niacc', [('drc354', 13500)], vin=self.VIN, end_ts=1564437693),
            detail(4, 'drc354', [('lowball', 15000)], vin=self.VIN, end_ts=1600000000, result='reserve_not_met'),
            detail(5, 'drc354', [('szvc', 17000)], vin=self.VIN, end_ts=1633293620),
            detail(6, 'someone', [('wsomerville', 18500)], vin=self.VIN, end_ts=1676228826)
        )

        timeline = self.db.query("SELECT listing_id, transition, change_since_prev FROM vehicle_timeline ORDER BY seq")
        self.assertEqual([(r['listing_id'], r['transition']) for r in timeline], [
            (1, 'first_seen'),
            (2, 'relisted_after_sale'),
            (3, 'resold_by_buyer'),
            (4, 'resold_by_buyer'),
            (5, 'relisted_unsold'),
            (6, 'new_seller'),
        ])
        self.assertEqual(timeline[2]['change_since_prev'], 500)

        resales = self.db.query("SELECT * FROM member_resales ORDER BY bought_ts")
        self.assertEqual(
            [(r['slug'], r['bought_price'], r['sold_price']) for r in resales],
            [('niacc', 13000, 13500), ('drc354', 13500, 17000)]
        )
        self.assertAlmostEqual(resales[1]['pct_change'], 0.259)
        self.assertEqual(self.db.find_vehicle_id(self.VIN.lower()), 1)
        self.assertEqual(self.db.find_vehicle_id(url(5)), 1)
        self.assertEqual(self.db.find_vehicle_id('5'), 1)


class TestMigration(unittest.TestCase):

    def test_opens_database_created_before_vin_tracking(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'old.db')
            conn = sqlite3.connect(path)
            # the auctions table as first released
            conn.executescript("""
                CREATE TABLE auctions (
                    listing_id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT, year INTEGER,
                    make TEXT, model TEXT, model_slug TEXT, era TEXT, origin TEXT, category TEXT,
                    country_code TEXT, country TEXT, location TEXT, no_reserve INTEGER, premium INTEGER,
                    result TEXT, high_bid INTEGER, currency TEXT, end_ts INTEGER, lot_number TEXT,
                    seller_slug TEXT, seller_type TEXT, high_bidder_slug TEXT, winner_slug TEXT,
                    n_bids INTEGER, n_comments INTEGER, discovered_at INTEGER, fetched_at INTEGER,
                    fetch_attempts INTEGER NOT NULL DEFAULT 0, fetch_error TEXT
                );
                CREATE VIEW member_activity AS SELECT 1 AS stale;
                INSERT INTO auctions (listing_id, url, fetched_at) VALUES (1, 'https://bringatrailer.com/listing/x/', 5);
            """)
            conn.close()

            db = ActivityDB(path)
            columns = {r['name'] for r in db.query("PRAGMA table_info(auctions)")}
            self.assertTrue({'vin', 'chassis', 'vehicle_id', 'parser_version', 'bids_reported'} <= columns)

            view_columns = [d[0] for d in db.conn.execute("SELECT * FROM member_activity").description]
            self.assertIn('listed', view_columns)

            self.assertEqual([r['listing_id'] for r in db.pending(upgrade_below=PV)], [1])
            db.close()


if __name__ == '__main__':
    unittest.main()
