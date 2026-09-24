import unittest
import sqlite3
import tempfile
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from core.models.activity import AuctionSummary, AuctionDetail, Bid, Member, HistoryLink
from storage.activity_db import ActivityDB


PV = 2
# fetch times after every synthetic auction has ended, so no saved page looks like a pre-end snapshot
SAVED = 2_000_000_000


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

        self.db.save_detail(detail(2, 'dealer', [('alice', 5000)]), now=SAVED + 5, parser_version=PV)
        for _ in range(3):
            self.db.mark_error(1, 'http 404')
        self.assertEqual(self.db.pending(), [])

    def test_upgrade_requeues_older_parser_versions(self):
        self.db.upsert_summaries([summary(1), summary(2)], now=1)
        self.db.save_detail(detail(1, 'dealer', []), now=SAVED + 2, parser_version=1)
        self.db.save_detail(detail(2, 'dealer', []), now=SAVED + 2, parser_version=2)

        self.assertEqual(self.db.pending(), [])
        self.assertEqual([r['listing_id'] for r in self.db.pending(upgrade_below=2)], [1])

    def test_refetch_replaces_bids(self):
        self.db.upsert_summaries([summary(1)], now=1)
        self.db.save_detail(detail(1, 'dealer', [('alice', 1000), ('bob', 2000)]), now=SAVED + 2, parser_version=PV)
        self.db.save_detail(detail(1, 'dealer', [('alice', 1000)]), now=SAVED + 3, parser_version=PV)
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM bids")[0]['n'], 1)

    def test_member_activity_rollup(self):
        self.db.upsert_summaries([summary(i) for i in (1, 2, 3)], now=1)
        self.db.save_detail(detail(1, 'dealer', [('alice', 1000), ('bob', 2000), ('alice', 3000)]), now=SAVED + 2, parser_version=PV)
        self.db.save_detail(detail(2, 'dealer', [('bob', 5000)]), now=SAVED + 2, parser_version=PV)
        self.db.save_detail(detail(3, 'alice', [('bob', 900)], result='reserve_not_met'), now=SAVED + 2, parser_version=PV)

        rows = {r['slug']: r for r in self.db.query("SELECT * FROM member_activity")}

        self.assertEqual((rows['dealer']['listed'], rows['dealer']['sold'], rows['dealer']['sold_usd']), (2, 2, 8000))
        self.assertEqual((rows['alice']['listed'], rows['alice']['sold']), (1, 0))
        self.assertEqual((rows['alice']['auctions_bid'], rows['alice']['bids'], rows['alice']['won']), (1, 2, 1))
        self.assertEqual((rows['bob']['auctions_bid'], rows['bob']['won'], rows['bob']['won_usd']), (3, 1, 5000))
        self.assertAlmostEqual(rows['bob']['win_rate'], 0.333)

        pairs = {(r['seller_slug'], r['bidder_slug']): r for r in self.db.query("SELECT * FROM seller_bidder_pairs")}
        self.assertEqual((pairs[('dealer', 'bob')]['auctions_bid'], pairs[('dealer', 'bob')]['won']), (2, 1))
        self.assertEqual(pairs[('alice', 'bob')]['won'], 0)

    def test_unknown_result_never_overwrites_a_known_one(self):
        self.db.upsert_summaries([summary(1)], now=1)
        self.db.save_detail(detail(1, 'dealer', [('alice', 1000)]), now=SAVED + 2, parser_version=PV)
        self.db.save_detail(detail(1, 'dealer', [('alice', 1000)], result='unknown'), now=SAVED + 3, parser_version=PV)
        self.assertEqual(self.db.query("SELECT result FROM auctions")[0]['result'], 'sold')

    def test_fields_a_page_didnt_yield_keep_their_stored_values(self):
        self.db.upsert_summaries([summary(1)], now=1)
        self.db.save_detail(detail(1, 'dealer', [('alice', 1000)], vin='WBSBR934X2EX23144'), now=SAVED + 2, parser_version=PV)
        blank = detail(1, 'dealer', [('alice', 1000)])
        blank.seller = blank.make = blank.model_slug = blank.chassis = blank.vin = None
        self.db.save_detail(blank, now=SAVED + 3, parser_version=PV)
        row = self.db.query("SELECT seller_slug, make, model_slug, vin, fetched_at FROM auctions")[0]
        self.assertEqual(tuple(row), ('dealer', 'BMW', 'bmw/model', 'WBSBR934X2EX23144', SAVED + 3))

    def test_a_bid_stored_under_another_listing_is_refused(self):
        self.db.upsert_summaries([summary(1), summary(2)], now=1)
        self.db.save_detail(detail(1, 'dealer', [('alice', 1000)]), now=SAVED + 2, parser_version=PV)
        other = detail(2, 'dealer', [('bob', 2000)])
        other.bids[0].bid_id = 100
        with self.assertRaises(ValueError) as ctx:
            self.db.save_detail(other, now=SAVED + 3, parser_version=PV)
        self.assertIn('already stored under listing 1', str(ctx.exception))
        rows = {r['listing_id']: r for r in self.db.query("SELECT listing_id, fetched_at FROM auctions")}
        self.assertIsNone(rows[2]['fetched_at'])
        self.assertEqual([tuple(r) for r in self.db.query("SELECT bid_id, listing_id FROM bids")], [(100, 1)])

    def test_mark_error_can_record_without_using_an_attempt(self):
        self.db.upsert_summaries([summary(1)], now=1)
        self.db.mark_error(1, 'layout changed', count_attempt=False)
        row = self.db.query("SELECT fetch_attempts, fetch_error FROM auctions")[0]
        self.assertEqual(tuple(row), (0, 'layout changed'))

    def test_rediscovery_keeps_what_the_page_said(self):
        self.db.upsert_summaries([summary(1, high_bid=6000, end_ts=1000)], now=1)
        page = detail(1, 'dealer', [('alice', 12000)], end_ts=1005)
        page.title = 'Gilbert & Barker Pump'
        self.db.save_detail(page, now=SAVED, parser_version=PV)

        again = summary(1, result='sold', high_bid=6000, end_ts=1000)
        again.title, again.url, again.no_reserve = 'Gilbert &#038; Barker Pump', url(1) + '?moved', True
        self.db.upsert_summaries([again], now=SAVED + 1)
        row = self.db.query("SELECT title, high_bid, end_ts, url, no_reserve FROM auctions")[0]
        # discovery still owns the url and flags; the page owns title, price and end time
        self.assertEqual(tuple(row), ('Gilbert & Barker Pump', 12000, 1005, url(1) + '?moved', 1))

    def test_discovery_never_downgrades_a_known_result_to_unknown(self):
        self.db.upsert_summaries([summary(1, result='sold')], now=1)
        self.db.upsert_summaries([summary(1, result='unknown')], now=2)
        self.assertEqual(self.db.query("SELECT result FROM auctions")[0]['result'], 'sold')

    def test_a_result_that_changes_to_sold_is_requeued(self):
        self.db.upsert_summaries([summary(1, result='reserve_not_met')], now=1)
        self.db.save_detail(detail(1, 'dealer', [('alice', 9000)], result='reserve_not_met'), now=SAVED, parser_version=PV)
        self.assertEqual(self.db.pending(now=SAVED), [])

        # a post-auction deal: bat relabels the result
        self.db.upsert_summaries([summary(1, result='sold')], now=SAVED + 1)
        row = self.db.query("SELECT result, refetch, winner_slug FROM auctions")[0]
        self.assertEqual(tuple(row), ('reserve_not_met', 1, None))
        self.assertEqual([r['listing_id'] for r in self.db.pending(now=SAVED)], [1])

        self.db.save_detail(detail(1, 'dealer', [('alice', 9000)], result='sold'), now=SAVED + 2, parser_version=PV)
        self.assertEqual(tuple(self.db.query("SELECT result, refetch FROM auctions")[0]), ('sold', 0))
        self.assertEqual(self.db.pending(now=SAVED), [])

    def test_reserve_not_met_is_checked_once_more_after_the_deal_window(self):
        end = 1_700_000_000
        self.db.upsert_summaries([summary(1, result='reserve_not_met', end_ts=end)], now=1)
        self.db.save_detail(detail(1, 'dealer', [('alice', 9000)], result='reserve_not_met', end_ts=end),
                            now=end + 3600, parser_version=PV)
        day = 86400
        self.assertEqual(self.db.pending(now=end + 5 * day), [])
        self.assertEqual([r['listing_id'] for r in self.db.pending(now=end + 11 * day)], [1])

        self.db.save_detail(detail(1, 'dealer', [('alice', 9000)], result='reserve_not_met', end_ts=end),
                            now=end + 11 * day, parser_version=PV)
        self.assertEqual(self.db.pending(now=end + 30 * day), [])

    def test_a_page_saved_before_its_auction_ended_is_requeued(self):
        end = 1_700_000_000
        self.db.upsert_summaries([summary(1, end_ts=end)], now=1)
        self.db.save_detail(detail(1, 'dealer', [('alice', 9000)], end_ts=end), now=end - 3600, parser_version=PV)
        self.assertEqual([r['listing_id'] for r in self.db.pending(now=end + 86400)], [1])
        self.db.save_detail(detail(1, 'dealer', [('alice', 9000)], end_ts=end), now=end + 86400, parser_version=PV)
        self.assertEqual(self.db.pending(now=end + 86400), [])

    def test_a_url_collision_or_bad_item_doesnt_cost_the_page(self):
        shared = summary(2)
        shared.url = url(1)
        broken = summary(3)
        broken.url = None
        skipped = []
        new = self.db.upsert_summaries([summary(1), shared, broken, summary(4)], now=1, skipped=skipped)
        self.assertEqual(new, 3)
        self.assertEqual([r['listing_id'] for r in self.db.query("SELECT listing_id FROM auctions ORDER BY 1")], [1, 2, 4])
        self.assertEqual([listing_id for listing_id, _ in skipped], [3])

    def test_scoped_refetch_queues_listings_without_hiding_them(self):
        self.db.upsert_summaries([summary(i) for i in (1, 2, 3)], now=1)
        for i in (1, 2, 3):
            self.db.save_detail(detail(i, 'dealer', [('alice', 1000)]), now=SAVED, parser_version=PV)
        self.assertEqual(self.db.mark_stale([1, 2], fetched_before=SAVED + 1), 2)
        rows = {r['listing_id']: r for r in self.db.query("SELECT listing_id, parser_version, fetched_at FROM auctions")}
        self.assertEqual((rows[1]['parser_version'], rows[1]['fetched_at'], rows[3]['parser_version']), (0, SAVED, PV))
        self.assertEqual({r['listing_id'] for r in self.db.pending(upgrade_below=PV, listing_ids=[1, 2, 3], now=SAVED)}, {1, 2})
        # already re-fetched since the scope was queued: not queued again
        self.assertEqual(self.db.mark_stale([1, 2], fetched_before=SAVED - 1), 0)

    def test_participants_follow_each_save(self):
        self.db.upsert_summaries([summary(1)], now=1)
        self.db.save_detail(detail(1, 'dealer', [('alice', 1000), ('bob', 2000), ('alice', 3000)]), now=SAVED, parser_version=PV)
        rows = {r['slug']: r for r in self.db.query("SELECT * FROM participants")}
        self.assertEqual((rows['alice']['n_bids'], rows['alice']['max_bid'], rows['alice']['won']), (2, 3000, 1))
        self.assertEqual((rows['bob']['won'], rows['bob']['seller_slug']), (0, 'dealer'))

        self.db.save_detail(detail(1, 'dealer', [('bob', 2000)]), now=SAVED + 1, parser_version=PV)
        self.assertEqual([(r['slug'], r['won']) for r in self.db.query("SELECT * FROM participants")], [('bob', 1)])
        self.assertEqual(self.db.rebuild_participants(), 1)

    def test_win_rate_comes_from_the_auctions_bid_on(self):
        self.db.upsert_summaries([summary(i) for i in (1, 2)], now=1)
        self.db.save_detail(detail(1, 'dealer', [('alice', 1000)]), now=SAVED, parser_version=PV)
        # bob won auction 2 without a parsed bid of his own
        self.db.save_detail(detail(2, 'dealer', [('alice', 500)], winner=None), now=SAVED, parser_version=PV)
        with self.db.conn:
            self.db.conn.execute("INSERT INTO members (slug) VALUES ('bob')")
            self.db.conn.execute("UPDATE auctions SET winner_slug = 'bob' WHERE listing_id = 2")
        self.db.rebuild_participants()
        rows = {r['slug']: r for r in self.db.query("SELECT * FROM member_activity")}
        self.assertEqual((rows['bob']['won'], rows['bob']['won_without_bid'], rows['bob']['win_rate']), (1, 1, None))
        self.assertEqual((rows['alice']['won'], rows['alice']['auctions_bid'], rows['alice']['win_rate']), (1, 2, 0.5))

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
            self.db.save_detail(d, now=SAVED + 1, parser_version=PV)
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

    def test_history_link_resolves_through_a_redirect(self):
        # listing 2 links to an old url; fetching it redirected to the listing's current url
        self.save(detail(2, 'b', [('c', 200)], history=['https://bringatrailer.com/listing/old-slug/']))
        moved = detail(1, 'a', [('b', 100)])
        self.db.save_detail(moved, now=SAVED + 1, parser_version=PV, requested_url='https://bringatrailer.com/listing/old-slug/')
        self.assertEqual(self.db.pending_history(), [])
        self.assertFalse(self.db.needs_fetch('https://bringatrailer.com/listing/old-slug/'))

    def test_failed_history_follows_stop_after_max_attempts(self):
        self.save(detail(2, 'b', [], history=[url(1)]))
        for _ in range(3):
            self.db.mark_url_error(url(1), 'http 404')
        self.assertEqual(self.db.pending_history(max_attempts=3), [])
        self.assertFalse(self.db.needs_fetch(url(1), max_attempts=3))
        self.assertTrue(self.db.needs_fetch(url(1), max_attempts=4))

    def test_refetching_a_listing_keeps_its_links_follow_attempts(self):
        self.save(detail(2, 'b', [], history=[url(1), url(7)]))
        self.db.mark_url_error(url(1), 'http 503')
        self.db.mark_url_error(url(1), 'http 503')
        self.save(detail(2, 'b', [], history=[url(1)]))
        links = self.db.query("SELECT related_url, follow_attempts FROM listing_links")
        self.assertEqual([(r['related_url'], r['follow_attempts']) for r in links], [(url(1), 2)])

    def test_a_listing_that_ran_out_of_attempts_isnt_followed_again(self):
        self.db.upsert_summaries([summary(1)], now=1)
        for _ in range(3):
            self.db.mark_error(1, 'http 404')
        self.assertFalse(self.db.needs_fetch(url(1), max_attempts=3))

    def test_parts_listing_quoting_a_vin_doesnt_join_that_car(self):
        self.save(
            detail(1, 'a', [('b', 100)], vin=self.VIN),
            detail(2, 'c', [('d', 50)], make='Parts and Automobilia', vin=self.VIN)
        )
        self.assertEqual(self.vehicle_ids(), {1: 1, 2: None})

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


class TestTimelineLabels(unittest.TestCase):

    VIN = 'WBSBR934X2EX23144'

    def setUp(self):
        self.db = ActivityDB(':memory:')

    def tearDown(self):
        self.db.close()

    def timeline(self):
        self.db.rebuild_vehicles()
        return [(r['listing_id'], r['transition']) for r in self.db.query("SELECT * FROM vehicle_timeline ORDER BY seq")]

    def save(self, *details):
        for d in details:
            self.db.save_detail(d, now=SAVED, parser_version=PV)

    def test_unknown_seller_and_unknown_previous(self):
        first = detail(1, 'a', [('b', 100)], vin=self.VIN, end_ts=1000)
        nameless = detail(2, 'b', [('c', 200)], vin=self.VIN, end_ts=2000)
        nameless.seller = None
        after = detail(3, 'b', [('d', 300)], vin=self.VIN, end_ts=3000)
        self.save(first, nameless, after)
        with self.db.conn:
            self.db.conn.execute("UPDATE auctions SET seller_slug = NULL WHERE listing_id = 2")
        self.assertEqual(self.timeline(), [(1, 'first_seen'), (2, 'unknown_seller'), (3, 'unknown_prev')])

    def test_relisted_unsold_only_after_reserve_not_met_or_withdrawn(self):
        self.save(
            detail(1, 'a', [('b', 100)], vin=self.VIN, end_ts=1000, result='reserve_not_met'),
            detail(2, 'a', [('b', 100)], vin=self.VIN, end_ts=2000, result='withdrawn'),
            detail(3, 'a', [('b', 100)], vin=self.VIN, end_ts=3000),
        )
        with self.db.conn:
            self.db.conn.execute("UPDATE auctions SET result = NULL WHERE listing_id = 3")
        self.save(detail(4, 'a', [('c', 100)], vin=self.VIN, end_ts=4000))
        self.assertEqual(self.timeline(), [(1, 'first_seen'), (2, 'relisted_unsold'), (3, 'relisted_unsold'),
                                           (4, 'unknown_prev')])

    def test_undated_auctions_go_last_or_borrow_a_date_from_bat_history(self):
        undated = detail(1, 'a', [('b', 100)], vin=self.VIN)
        undated.end_ts = None
        self.save(undated, detail(2, 'b', [('c', 200)], vin=self.VIN, end_ts=5000))
        self.assertEqual(self.timeline(), [(2, 'first_seen'), (1, 'new_seller')])

        # a later listing's bat history dates it
        linker = detail(3, 'c', [('d', 300)], vin=self.VIN, end_ts=9000)
        linker.history = [HistoryLink(url=url(1), end_ts=1500)]
        self.save(linker)
        self.assertEqual(self.timeline(), [(1, 'first_seen'), (2, 'resold_by_buyer'), (3, 'resold_by_buyer')])
        self.assertEqual(self.db.query("SELECT end_ts FROM auctions WHERE listing_id = 1")[0]['end_ts'], 1500)


class TestFindVehicle(unittest.TestCase):

    def setUp(self):
        self.db = ActivityDB(':memory:')
        for d in (detail(168413, 'a', [('b', 1)], vin='WBSBR934X2EX23144'),
                  detail(2, 'c', [('d', 1)], make='Porsche', chassis='168413'),
                  detail(3, 'e', [('f', 1)], make='Porsche', chassis='9113101234'),
                  detail(4, 'g', [('h', 1)], make='Jaguar', chassis='9113101234')):
            self.db.save_detail(d, now=SAVED, parser_version=PV)
        self.db.rebuild_vehicles()

    def tearDown(self):
        self.db.close()

    def test_a_listing_id_beats_a_chassis_with_the_same_digits(self):
        self.assertEqual(self.db.find_vehicle_id('168413'), 168413)

    def test_vins_and_urls_are_normalized(self):
        self.assertEqual(self.db.find_vehicle_id('wbs-br934x2ex23144 '), 168413)
        self.assertEqual(self.db.find_vehicle_id('http://bringatrailer.com/listing/car-168413?utm=x'), 168413)
        self.assertEqual(self.db.find_vehicle_id('https://bringatrailer.com/listing/car-2'), 2)

    def test_an_ambiguous_chassis_lists_the_candidates(self):
        self.assertIsNone(self.db.find_vehicle_id('911 310 1234'))
        self.assertEqual([r['vehicle_id'] for r in self.db.find_vehicles('911 310 1234')], [3, 4])
        self.assertEqual(self.db.find_vehicles('nothing-like-this'), [])


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
            self.assertTrue({'vin', 'chassis', 'chassis_raw', 'vehicle_id', 'parser_version', 'bids_reported'} <= columns)

            view_columns = [d[0] for d in db.conn.execute("SELECT * FROM member_activity").description]
            self.assertIn('listed', view_columns)

            self.assertEqual([r['listing_id'] for r in db.pending(upgrade_below=PV)], [1])
            db.close()

    def test_drops_unique_url_from_an_existing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'v1.db')
            db = ActivityDB(path)
            db.upsert_summaries([summary(1), summary(2)], now=1)
            db.save_detail(detail(1, 'a', [('b', 100), ('c', 200)], history=[url(2)]), now=SAVED, parser_version=PV)
            db.close()

            # put the database back the way the first release left it: url UNIQUE, no schema version
            conn = sqlite3.connect(path)
            conn.executescript("""
                PRAGMA foreign_keys=OFF;
                CREATE TABLE auctions_v1 AS SELECT * FROM auctions;
                DROP TABLE auctions;
            """)
            create = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'auctions_v1'").fetchone()
            columns = [r[1] for r in conn.execute("PRAGMA table_info(auctions_v1)")]
            conn.executescript(f"""
                CREATE TABLE auctions (listing_id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE,
                    {', '.join(c for c in columns if c not in ('listing_id', 'url'))});
                INSERT INTO auctions SELECT {', '.join(columns)} FROM auctions_v1;
                DROP TABLE auctions_v1;
                DELETE FROM meta WHERE key = 'schema_version';
            """)
            conn.close()
            self.assertIsNotNone(create)

            db = ActivityDB(path)
            self.assertFalse(db._url_is_unique())
            self.assertEqual(db.get_meta('schema_version'), '3')
            self.assertEqual(db.query("SELECT COUNT(*) AS n FROM auctions")[0]['n'], 2)
            # the participants table is filled from the bids already there
            self.assertEqual([(r['slug'], r['n_bids']) for r in db.query("SELECT * FROM participants ORDER BY slug")],
                             [('b', 1), ('c', 1)])
            self.assertEqual(db.query("SELECT COUNT(*) AS n FROM bids")[0]['n'], 2)
            self.assertEqual(db.query("PRAGMA foreign_key_check"), [])
            self.assertEqual(db.query("SELECT related_listing_id FROM listing_links")[0]['related_listing_id'], 2)
            self.assertTrue(db.query("SELECT * FROM member_activity"))
            # two listings may now share a url
            other = summary(3)
            other.url = url(1)
            self.assertEqual(db.upsert_summaries([other], now=2), 1)
            db.close()


if __name__ == '__main__':
    unittest.main()
