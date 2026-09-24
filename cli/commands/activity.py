import os
import sys
import argparse
from datetime import datetime, timezone

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from sites.bringatrailer.http_client import BaTClient, RateLimited, SiteUnavailable, listing_url
from sites.bringatrailer.activity_parser import ActivityParser
from storage.activity_db import ActivityDB
from pipelines.activity_pipeline import ActivityPipeline, CircuitOpen, fmt_ts


ROOT = os.path.join(os.path.dirname(__file__), '../..')
CONFIG_PATH = os.path.join(ROOT, 'config/sites/bringatrailer.yaml')
DEFAULT_DB = os.path.join(ROOT, 'data/db/bat_activity.db')


STOP_HINTS = {
    'site': "no listing was charged an attempt for this; once the site answers normally, run the same command again",
    'failures': "check the errors above. layout errors don't use up attempts, so a parser fix picks those listings up "
                "again; http and redirect errors do, and `reset-errors` makes those eligible again",
    'unmarked': "nothing was saved or charged for these. compare activity.selectors.ended_marker in "
                "config/sites/bringatrailer.yaml with a finished listing's page",
}


def parse_date(value: str) -> int:
    return int(datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())


def fmt_utc(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    except (OverflowError, OSError, ValueError):
        return 'a very long time from now'


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def build_pipeline(args, db: ActivityDB, config: dict) -> ActivityPipeline:
    client = BaTClient(
        base_url=config['site']['base_url'],
        disallowed_paths=config['robots_txt']['disallowed_paths'],
        crawl_delay=config['robots_txt']['crawl_delay'],
        delay=args.delay
    )
    parser = ActivityParser(config['activity']['selectors'])
    return ActivityPipeline(client, parser, db, config['activity'])


def run_discover(args, pipeline: ActivityPipeline) -> dict:
    print("discovering completed auctions...")
    print("=" * 70)
    stats = pipeline.discover(
        start_page=args.start_page,
        max_pages=args.max_pages,
        since_ts=parse_date(args.since) if args.since else None,
        stop_after_known=args.stop_after_known,
        backfill=args.backfill
    )
    print(f"\n{stats['pages']} page(s), {stats['seen']} auctions seen, {stats['new']} new\n")
    return stats


def run_fetch(args, pipeline: ActivityPipeline, urls=None) -> dict:
    print("fetching bid histories...")
    print("=" * 70)
    stats = pipeline.fetch(
        limit=args.limit,
        since_ts=parse_date(args.since) if args.since else None,
        max_attempts=args.max_attempts,
        upgrade=args.upgrade,
        follow_history=not args.no_follow_history,
        urls=urls
    )
    print(f"\n{stats['fetched']} auction(s) saved, {stats['bids']} bids, "
          f"{stats['followed']} history links followed, {stats['failed']} failed")
    print_vehicle_stats(stats['vehicles'])
    return stats


def reset_errors(db: ActivityDB):
    top = db.query("""
        SELECT substr(fetch_error, 1, 80) AS error, COUNT(*) AS n FROM auctions
        WHERE fetched_at IS NULL AND fetch_error IS NOT NULL
        GROUP BY 1 ORDER BY n DESC LIMIT 5
    """)
    if top:
        print("most common errors being cleared:")
        for row in top:
            print(f"  {row['n']:>6,}  {row['error']}")
    counts = db.reset_errors()
    print(f"{counts['listings']:,} listing(s) and {counts['links']:,} history link(s) will be tried again")


def print_vehicle_stats(stats: dict):
    conflicts = f", {stats['vin_conflicts']} linked by bat history despite differing vins" if stats['vin_conflicts'] else ''
    print(f"{stats['vehicles']:,} vehicles tracked, {stats['repeat_vehicles']:,} auctioned more than once{conflicts}\n")


def money(value) -> str:
    return f"${value:,.0f}" if value else '-'


def print_table(title: str, rows, columns):
    print(f"\n{title}")
    print("-" * 70)
    if not rows:
        print("  (none)")
        return

    cells = [[fmt(row[key]) for key, _, fmt in columns] for row in rows]
    widths = [
        max(len(header), *(len(c[i]) for c in cells))
        for i, (_, header, _) in enumerate(columns)
    ]
    print("  " + "  ".join(h.ljust(w) for (_, h, _), w in zip(columns, widths)))
    for c in cells:
        print("  " + "  ".join(v.ljust(w) for v, w in zip(c, widths)))


def text(value) -> str:
    return '-' if value is None else str(value)


def pct(value) -> str:
    return '-' if value is None else f"{value * 100:.0f}%"


def signed_pct(value) -> str:
    return '-' if value is None else f"{value * 100:+.0f}%"


def signed_money(value) -> str:
    return '-' if value is None else f"{'+' if value >= 0 else '-'}${abs(value):,.0f}"


def days(value) -> str:
    return '-' if value is None else f"{value:,.0f}"


def label(value) -> str:
    return text(value).replace('_', ' ')


def truncate(n):
    return lambda value: text(value)[:n]


def auction_filters(args, alias: str):
    clauses, params = [], []
    if args.model:
        clauses.append(f"({alias}.model_slug = ? OR {alias}.model_slug LIKE ?)")
        params += [args.model, f"{args.model}/%"]
    if args.since:
        clauses.append(f"{alias}.end_ts >= ?")
        params.append(parse_date(args.since))
    return ''.join(f" AND {c}" for c in clauses), params


def report_overview(db: ActivityDB):
    row = db.query("""
        SELECT
            COUNT(*) AS discovered,
            SUM(fetched_at IS NOT NULL) AS fetched,
            SUM(fetched_at IS NULL AND fetch_error IS NOT NULL) AS errored,
            SUM(bids_reported IS NOT NULL AND bids_reported != n_bids) AS bid_mismatches,
            MIN(CASE WHEN fetched_at IS NOT NULL THEN end_ts END) AS first_ts,
            MAX(CASE WHEN fetched_at IS NOT NULL THEN end_ts END) AS last_ts
        FROM auctions
    """)[0]
    counts = db.query("SELECT (SELECT COUNT(*) FROM members) AS members, (SELECT COUNT(*) FROM bids) AS bids")[0]
    vehicles = db.query("""
        SELECT COUNT(*) AS vehicles, SUM(n > 1) AS repeat_vehicles
        FROM (
            SELECT vehicle_id, COUNT(*) AS n FROM auctions
            WHERE vehicle_id IS NOT NULL AND fetched_at IS NOT NULL
            GROUP BY vehicle_id
        )
    """)[0]

    print("=" * 70)
    print("bat activity database")
    print("=" * 70)
    print(f"  auctions discovered : {row['discovered'] or 0:,}")
    print(f"  with bid history    : {row['fetched'] or 0:,} ({fmt_ts(row['first_ts'])} to {fmt_ts(row['last_ts'])})")
    print(f"  fetch errors        : {row['errored'] or 0:,}")
    print(f"  bid count mismatches: {row['bid_mismatches'] or 0:,}")
    print(f"  members             : {counts['members']:,}")
    print(f"  bids                : {counts['bids']:,}")
    print(f"  vehicles tracked    : {vehicles['vehicles'] or 0:,} ({vehicles['repeat_vehicles'] or 0:,} auctioned more than once)")

    # bat history on a later listing says "Sold by X to Y" for each earlier one: an independent check on our parsing
    checks = db.query("""
        SELECT
            COUNT(*) AS checked,
            SUM(NOT (l.summary LIKE '%by ' || COALESCE(s.display_name, '?') || ' to ' || COALESCE(w.display_name, '?') || ' for%')) AS disagree
        FROM auctions a
        JOIN (
            SELECT related_listing_id, MAX(summary) AS summary FROM listing_links
            WHERE related_listing_id IS NOT NULL AND summary LIKE 'Sold by %'
            GROUP BY related_listing_id
        ) l ON l.related_listing_id = a.listing_id
        LEFT JOIN members s ON s.slug = a.seller_slug
        LEFT JOIN members w ON w.slug = a.winner_slug
        WHERE a.fetched_at IS NOT NULL
    """)[0]
    print(f"  seller/buyer check  : {checks['checked'] or 0:,} sales checked against bat history, {checks['disagree'] or 0:,} disagree")


def report_leaderboards(db: ActivityDB, args):
    where, params = auction_filters(args, 'a')
    scope = f" ({args.model or 'all models'}{', since ' + args.since if args.since else ''})"

    sellers = db.query(f"""
        SELECT a.seller_slug AS slug, m.display_name, MAX(a.seller_type) AS seller_type,
               COUNT(*) AS listed, SUM(a.result = 'sold') AS sold,
               ROUND(1.0 * SUM(a.result = 'sold') / COUNT(*), 3) AS sell_through,
               SUM(CASE WHEN a.result = 'sold' AND a.currency = 'USD' THEN a.high_bid END) AS sold_usd
        FROM auctions a LEFT JOIN members m ON m.slug = a.seller_slug
        WHERE a.fetched_at IS NOT NULL AND a.seller_slug IS NOT NULL{where}
        GROUP BY a.seller_slug ORDER BY listed DESC, sold_usd DESC LIMIT ?
    """, params + [args.top])
    print_table("top sellers" + scope, sellers, [
        ('display_name', 'member', text), ('seller_type', 'type', text), ('listed', 'listed', text),
        ('sold', 'sold', text), ('sell_through', 'sell-thru', pct), ('sold_usd', 'gross sold', money)
    ])

    p_where, p_params = auction_filters(args, 'p')
    bidders = db.query(f"""
        SELECT p.slug, m.display_name, COUNT(*) AS auctions_bid, SUM(p.n_bids) AS bids, SUM(p.won) AS won,
               ROUND(1.0 * SUM(p.won) / COUNT(*), 3) AS win_rate
        FROM auction_participants p LEFT JOIN members m ON m.slug = p.slug
        WHERE 1 = 1{p_where}
        GROUP BY p.slug ORDER BY auctions_bid DESC, bids DESC LIMIT ?
    """, p_params + [args.top])
    print_table("most active bidders" + scope, bidders, [
        ('display_name', 'member', text), ('auctions_bid', 'auctions', text), ('bids', 'bids', text),
        ('won', 'won', text), ('win_rate', 'win rate', pct)
    ])

    winners = db.query(f"""
        SELECT a.winner_slug AS slug, m.display_name, COUNT(*) AS won,
               SUM(CASE WHEN a.currency = 'USD' THEN a.high_bid END) AS won_usd,
               MAX(CASE WHEN a.currency = 'USD' THEN a.high_bid END) AS top_usd
        FROM auctions a LEFT JOIN members m ON m.slug = a.winner_slug
        WHERE a.result = 'sold' AND a.winner_slug IS NOT NULL{where}
        GROUP BY a.winner_slug ORDER BY won DESC, won_usd DESC LIMIT ?
    """, params + [args.top])
    print_table("top buyers" + scope, winners, [
        ('display_name', 'member', text), ('won', 'won', text),
        ('won_usd', 'total spent', money), ('top_usd', 'biggest win', money)
    ])


def report_pairs(db: ActivityDB, args):
    rows = db.query("""
        SELECT s.display_name AS seller, b.display_name AS bidder, p.auctions_bid, p.bids, p.won
        FROM seller_bidder_pairs p
        LEFT JOIN members s ON s.slug = p.seller_slug
        LEFT JOIN members b ON b.slug = p.bidder_slug
        WHERE p.auctions_bid >= ?
        ORDER BY p.auctions_bid DESC, p.won DESC LIMIT ?
    """, (args.min_auctions or 3, args.top))
    print_table(f"repeat seller/bidder pairs (>= {args.min_auctions or 3} auctions together)", rows, [
        ('seller', 'seller', text), ('bidder', 'bidder', text),
        ('auctions_bid', 'auctions', text), ('bids', 'bids', text), ('won', 'won', text)
    ])


def report_vehicle(db: ActivityDB, args):
    vehicle_id = db.find_vehicle_id(args.vehicle)
    if vehicle_id is None:
        print(f"\nno tracked vehicle matches '{args.vehicle}' (use a vin, chassis number, listing url or listing id)")
        return

    rows = db.query("""
        SELECT t.*, s.display_name AS seller, w.display_name AS buyer
        FROM vehicle_timeline t
        LEFT JOIN members s ON s.slug = t.seller_slug
        LEFT JOIN members w ON w.slug = t.winner_slug
        WHERE t.vehicle_id = ? ORDER BY t.seq
    """, (vehicle_id,))
    chassis = sorted({r['vin'] or r['chassis'] for r in rows if r['vin'] or r['chassis']})

    print("=" * 70)
    print(rows[-1]['title'])
    print(f"  chassis: {', '.join(chassis) or '?'}  |  {len(rows)} auction(s) on bat")
    print("=" * 70)
    print_table("auction history", rows, [
        ('end_ts', 'ended', fmt_ts), ('result', 'result', label), ('high_bid', 'price', money),
        ('seller', 'seller', text), ('buyer', 'buyer', text), ('transition', 'what happened', label),
        ('days_since_prev', 'days since prev', days), ('change_since_prev', 'price change', signed_money)
    ])

    unfetched = db.query("""
        SELECT l.related_url, MAX(l.related_end_ts) AS end_ts, MAX(l.summary) AS summary
        FROM listing_links l JOIN auctions a ON a.listing_id = l.listing_id
        WHERE a.vehicle_id = ? AND l.related_listing_id IS NULL
        GROUP BY l.related_url ORDER BY end_ts
    """, (vehicle_id,))
    if unfetched:
        print_table("bat history entries not fetched yet", unfetched, [
            ('end_ts', 'ended', fmt_ts), ('summary', 'summary', text), ('related_url', 'url', text)
        ])


def report_resales(db: ActivityDB, args):
    clauses, params = [], []
    if args.model:
        clauses.append("(a.model_slug = ? OR a.model_slug LIKE ?)")
        params += [args.model, f"{args.model}/%"]
    if args.since:
        clauses.append("r.sold_ts >= ?")
        params.append(parse_date(args.since))
    where = ''.join(f" AND {c}" for c in clauses)
    min_resales = args.min_auctions or 2
    scope = f" ({args.model or 'all models'}{', resold since ' + args.since if args.since else ''})"

    members = db.query(f"""
        SELECT r.slug, m.display_name, COUNT(*) AS resold, SUM(r.price_change > 0) AS gains,
               ROUND(AVG(r.days_held)) AS avg_days_held,
               ROUND(AVG(r.pct_change), 3) AS avg_pct_change,
               SUM(CASE WHEN r.currency = 'USD' THEN r.price_change END) AS total_change_usd
        FROM member_resales r
        JOIN auctions a ON a.listing_id = r.bought_listing_id
        LEFT JOIN members m ON m.slug = r.slug
        WHERE 1 = 1{where}
        GROUP BY r.slug HAVING COUNT(*) >= ?
        ORDER BY resold DESC, total_change_usd DESC LIMIT ?
    """, params + [min_resales, args.top])
    print_table(f"members who resell cars they won (>= {min_resales} resales, gross before fees){scope}", members, [
        ('display_name', 'member', text), ('resold', 'resold', text), ('gains', 'at a gain', text),
        ('avg_days_held', 'avg days held', days), ('avg_pct_change', 'avg change', signed_pct),
        ('total_change_usd', 'total change', signed_money)
    ])

    recent = db.query(f"""
        SELECT r.*, m.display_name
        FROM member_resales r
        JOIN auctions a ON a.listing_id = r.bought_listing_id
        LEFT JOIN members m ON m.slug = r.slug
        WHERE 1 = 1{where}
        ORDER BY r.sold_ts DESC LIMIT ?
    """, params + [args.top])
    print_table("most recent resales" + scope, recent, [
        ('display_name', 'member', text), ('title', 'title', truncate(36)),
        ('bought_price', 'paid', money), ('sold_price', 'resold for', money),
        ('days_held', 'days held', days), ('pct_change', 'change', signed_pct)
    ])


def report_member(db: ActivityDB, args):
    slug = args.member.lower()
    summary = db.query("SELECT * FROM member_activity WHERE slug = ?", (slug,))
    if not summary:
        print(f"\nno member '{slug}' in the database")
        return
    s = summary[0]

    print("=" * 70)
    print(f"{s['display_name'] or slug}  (member/{slug}/, id {text(s['user_id'])})")
    print("=" * 70)
    print(f"  active       : {fmt_ts(s['first_seen_ts'])} to {fmt_ts(s['last_seen_ts'])}")
    print(f"  selling      : {s['listed']} listed, {s['sold']} sold, {money(s['sold_usd'])} gross")
    print(f"  bidding      : {s['bids']} bids across {s['auctions_bid']} auctions")
    print(f"  winning      : {s['won']} won ({pct(s['win_rate'])} of auctions bid), {money(s['won_usd'])} spent")

    listings = db.query("""
        SELECT a.end_ts, a.result, a.high_bid, a.title, w.display_name AS winner
        FROM auctions a LEFT JOIN members w ON w.slug = a.winner_slug
        WHERE a.seller_slug = ? ORDER BY a.end_ts DESC LIMIT ?
    """, (slug, args.top))
    print_table("sold / listed", listings, [
        ('end_ts', 'ended', fmt_ts), ('result', 'result', text), ('high_bid', 'price', money),
        ('title', 'title', truncate(40)), ('winner', 'buyer', text)
    ])

    bid_on = db.query("""
        SELECT p.end_ts, p.max_bid, p.high_bid, p.won, a.title, s.display_name AS seller
        FROM auction_participants p
        JOIN auctions a ON a.listing_id = p.listing_id
        LEFT JOIN members s ON s.slug = p.seller_slug
        WHERE p.slug = ? ORDER BY p.end_ts DESC LIMIT ?
    """, (slug, args.top))
    print_table("bid on", bid_on, [
        ('end_ts', 'ended', fmt_ts), ('max_bid', 'their max', money), ('high_bid', 'final', money),
        ('won', 'won', lambda v: 'yes' if v else ''), ('title', 'title', truncate(40)), ('seller', 'seller', text)
    ])

    makes = db.query("""
        SELECT COALESCE(make, '?') AS make, COUNT(*) AS auctions_bid, SUM(won) AS won
        FROM auction_participants WHERE slug = ?
        GROUP BY make ORDER BY auctions_bid DESC LIMIT ?
    """, (slug, args.top))
    print_table("makes bid on", makes, [('make', 'make', text), ('auctions_bid', 'auctions', text), ('won', 'won', text)])

    buys_from = db.query("""
        SELECT m.display_name AS seller, p.auctions_bid, p.won FROM seller_bidder_pairs p
        LEFT JOIN members m ON m.slug = p.seller_slug
        WHERE p.bidder_slug = ? ORDER BY p.auctions_bid DESC, p.won DESC LIMIT ?
    """, (slug, args.top))
    print_table("sellers they bid on most", buys_from, [
        ('seller', 'seller', text), ('auctions_bid', 'auctions', text), ('won', 'won', text)
    ])

    bidders = db.query("""
        SELECT m.display_name AS bidder, p.auctions_bid, p.won FROM seller_bidder_pairs p
        LEFT JOIN members m ON m.slug = p.bidder_slug
        WHERE p.seller_slug = ? ORDER BY p.auctions_bid DESC, p.won DESC LIMIT ?
    """, (slug, args.top))
    print_table("who bids on their cars", bidders, [
        ('bidder', 'bidder', text), ('auctions_bid', 'auctions', text), ('won', 'won', text)
    ])

    resales = db.query("""
        SELECT * FROM member_resales WHERE slug = ? ORDER BY sold_ts DESC LIMIT ?
    """, (slug, args.top))
    print_table("cars they won and later resold on bat", resales, [
        ('title', 'title', truncate(36)), ('bought_ts', 'bought', fmt_ts), ('bought_price', 'paid', money),
        ('sold_ts', 'resold', fmt_ts), ('sold_price', 'resold for', money),
        ('days_held', 'days', days), ('pct_change', 'change', signed_pct)
    ])


def add_discover_args(p):
    p.add_argument('--max-pages', type=int, help='stop after this many results pages')
    p.add_argument('--start-page', type=int, help='results page to start from')
    p.add_argument('--backfill', action='store_true',
                   help='walk back through history without stopping at known auctions; resumes where the last backfill stopped')
    p.add_argument('--stop-after-known', type=int, default=2,
                   help='incremental mode: stop after this many pages with no new auctions')


def add_fetch_args(p):
    p.add_argument('--limit', type=int, help='fetch at most this many listing pages')
    p.add_argument('--max-attempts', type=int, default=3, help='give up on a listing after this many failures')
    p.add_argument('--upgrade', action='store_true',
                   help='also re-fetch listings saved by an older parser version (e.g. before vin tracking)')
    p.add_argument('--no-follow-history', action='store_true',
                   help="don't fetch other auctions of the same car linked from a listing's bat history")


def main(argv=None):
    parser = argparse.ArgumentParser(description='track who sells, bids on and wins bringatrailer auctions')
    parser.add_argument('--db', default=DEFAULT_DB, help='sqlite database path')
    sub = parser.add_subparsers(dest='command', required=True)

    discover = sub.add_parser('discover', help='page through completed auction results')
    add_discover_args(discover)

    fetch = sub.add_parser('fetch', help='download bid histories for discovered auctions')
    add_fetch_args(fetch)
    fetch.add_argument('--url', nargs='+', help='fetch these listing urls (and their bat history) instead of the queue')

    sync = sub.add_parser('sync', help='discover new results, then fetch their bid histories')
    add_discover_args(sync)
    add_fetch_args(sync)

    for p in (discover, fetch, sync):
        p.add_argument('--since', help='only auctions ending on or after YYYY-MM-DD')
        p.add_argument('--delay', type=float, default=3.0, help='seconds between requests (robots crawl-delay is the floor)')

    sub.add_parser('link', help='regroup fetched auctions into vehicles by vin and bat history (no network)')
    sub.add_parser('reset-errors',
                   help='clear failure counts so listings and history links that hit --max-attempts are tried again (no network)')

    report = sub.add_parser('report', help='summarize selling, bidding and winning activity')
    report.add_argument('--top', type=int, default=15, help='rows per table')
    report.add_argument('--model', help='make or model slug, e.g. bmw or bmw/e46-m3')
    report.add_argument('--since', help='only auctions ending on or after YYYY-MM-DD')
    report.add_argument('--member', help='member slug for a single-member profile')
    report.add_argument('--vehicle', help='vin, chassis number, listing url or listing id: every bat auction of that car')
    report.add_argument('--resales', action='store_true', help='members who resell cars they won, with hold time and price change')
    report.add_argument('--pairs', action='store_true', help='show repeat seller/bidder relationships')
    report.add_argument('--min-auctions', type=int,
                        help='minimum shared auctions for --pairs (default 3) or resales for --resales (default 2)')

    args = parser.parse_args(argv)
    config = load_config()

    urls = None
    if getattr(args, 'url', None):
        try:
            urls = [listing_url(u, config['site']['base_url']) for u in args.url]
        except ValueError as e:
            parser.error(f"--url: {e}")

    db = ActivityDB(args.db)

    try:
        if args.command == 'link':
            print_vehicle_stats(db.rebuild_vehicles())
            return 0

        if args.command == 'reset-errors':
            reset_errors(db)
            return 0

        if args.command == 'report':
            if args.vehicle:
                report_vehicle(db, args)
            elif args.member:
                report_member(db, args)
            elif args.resales:
                report_resales(db, args)
            elif args.pairs:
                report_pairs(db, args)
            else:
                report_overview(db)
                report_leaderboards(db, args)
            print()
            return 0

        pipeline = build_pipeline(args, db, config)
        status = 0
        try:
            if args.command in ('discover', 'sync'):
                run_discover(args, pipeline)
            if args.command in ('fetch', 'sync'):
                stats = run_fetch(args, pipeline, urls)
                if stats['fetched'] == 0 and stats['failed'] > 0:
                    status = 1
        except KeyboardInterrupt:
            print("\ninterrupted, progress so far is saved")
            status = 130
        except RateLimited as e:
            print(f"\nstopped: {e}")
            print(f"resume after {fmt_utc(e.resume_at)}")
            status = 2
        except (SiteUnavailable, CircuitOpen) as e:
            print(f"\nstopped: {e}")
            print(STOP_HINTS[getattr(e, 'kind', 'site')])
            status = 2
        print(f"{pipeline.client.request_count} request(s) made, database at {args.db}")
        return status
    finally:
        db.close()


if __name__ == '__main__':
    sys.exit(main())
