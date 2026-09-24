import os
import sys
import json
import time
import hashlib
import sqlite3
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone

try:
    import fcntl
except ImportError:
    fcntl = None

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from sites.bringatrailer.http_client import (
    BaTClient, DEFAULT_USER_AGENT, ROBOTS_TOKEN, RateLimited, SiteUnavailable, listing_url
)
from sites.bringatrailer.activity_parser import ActivityParser, PARSER_VERSION
from storage.activity_db import ActivityDB
from storage.raw_store import RawStore, raw_db_path
from pipelines.activity_pipeline import ActivityPipeline, CircuitOpen, fmt_ts
from pipelines.crawl_budget import CrawlBudget, parse_active_hours


ROOT = os.path.join(os.path.dirname(__file__), '../..')
CONFIG_PATH = os.path.join(ROOT, 'config/sites/bringatrailer.yaml')
DEFAULT_DB = os.path.join(ROOT, 'data/db/bat_activity.db')


STOP_HINTS = {
    'site': "no listing was charged an attempt for this; once the site answers normally, run the same command again",
    'failures': "check the errors above. layout errors don't use up attempts, so a parser fix picks those listings up "
                "again; http and redirect errors do, and `reset-errors` makes those eligible again",
    'unmarked': "nothing was saved or charged for these. compare activity.selectors.ended_marker in "
                "config/sites/bringatrailer.yaml with a finished listing's page",
    'robots': "no listing was charged an attempt. if robots.txt couldn't be read, try again later; if it now "
              "disallows what this crawler needs, read it before changing anything",
}


# exit statuses besides 0 and 1
EXIT_STOPPED = 2        # the site pushed back, or failures in a row stopped the run
EXIT_LOCKED = 75        # another run holds the database (EX_TEMPFAIL): try again later
EXIT_INTERRUPTED = 130

# commands that write to the database take its lock; report only reads
LOCKED_COMMANDS = ('discover', 'fetch', 'sync', 'link', 'reparse', 'reset-errors')

# fetch --recheck-mismatches: saved listings whose page counted a different number of bids than were parsed
MISMATCH_WHERE = "fetched_at IS NOT NULL AND bids_reported IS NOT NULL AND bids_reported != n_bids"


def parse_date(value: str) -> int:
    return int(datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())


def date_arg(value: str) -> int:
    try:
        return parse_date(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a date like 2025-01-31")


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number")
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got {number}")
    return number


def active_hours_arg(value: str) -> str:
    try:
        parse_active_hours(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))
    return value.strip().lower() if value.strip().lower() in ('off', 'none') else value.strip()


def budget_arg(value: str) -> int:
    """a daily request budget; 0 or 'off' turns it off"""
    if value.strip().lower() in ('off', 'none'):
        return 0
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number or 'off'")
    if number < 0:
        raise argparse.ArgumentTypeError(f"must be 0 (off) or more, got {number}")
    return number


def non_negative_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number")
    if number < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {value}")
    return number


class LockHeld(Exception):
    pass


@contextmanager
def run_lock(db_path: str):
    """one crawler per database: a second run would trail the first through the same queue"""
    if fcntl is None or db_path == ':memory:':
        yield
        return
    lock_file = open(os.path.abspath(db_path) + '.lock', 'a')
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        raise LockHeld(f"another run is using {db_path}")
    try:
        yield
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def fmt_utc(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    except (OverflowError, OSError, ValueError):
        return 'a very long time from now'


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def user_agent(http: dict) -> str:
    agent = http.get('user_agent') or DEFAULT_USER_AGENT
    return f"{agent} (+{http['contact']})" if http.get('contact') else agent


def build_budget(args, db: ActivityDB) -> CrawlBudget:
    """--daily-budget and --active-hours are remembered in the database, so later runs keep to them"""
    if args.daily_budget is not None:
        db.set_meta('daily_budget', str(args.daily_budget))
    if args.active_hours is not None:
        db.set_meta('active_hours', args.active_hours)
    daily = int(db.get_meta('daily_budget') or 0) or None
    hours = parse_active_hours(db.get_meta('active_hours') or 'off')
    return CrawlBudget(db.get_meta, db.set_meta, daily=daily, active_hours=hours)


def build_pipeline(args, db: ActivityDB, config: dict, raw_store: RawStore) -> ActivityPipeline:
    client = None
    if hasattr(args, 'delay'):
        http = config['activity'].get('http') or {}
        budget = build_budget(args, db)
        print(f"pacing: {max(args.delay, config['robots_txt']['crawl_delay']):g}s between requests or more, "
              f"{budget.describe()}")
        client = BaTClient(
            base_url=config['site']['base_url'],
            disallowed_paths=config['robots_txt']['disallowed_paths'],
            crawl_delay=config['robots_txt']['crawl_delay'],
            delay=args.delay,
            user_agent=user_agent(http),
            robots_token=http.get('robots_token') or ROBOTS_TOKEN,
            fetch_robots=True,
            # the run needs the results feed and listing pages; robots.txt ruling either out stops it
            required_paths=(config['activity']['results_endpoint'], '/listing/example/'),
            budget=budget,
            long_pause_every=tuple(http['long_pause_every']) if http.get('long_pause_every') else None,
            long_pause_seconds=tuple(http['long_pause_seconds']) if http.get('long_pause_seconds') else None
        )
    parser = ActivityParser(config['activity']['selectors'])
    return ActivityPipeline(client, parser, db, config['activity'], raw_store=raw_store)


def scope_ids(args, db: ActivityDB, parser: argparse.ArgumentParser):
    """listing ids from --where, --ids-from or --recheck-mismatches, or None when none was given"""
    if getattr(args, 'recheck_mismatches', False):
        return db.listing_ids_where(MISMATCH_WHERE)
    if getattr(args, 'where', None):
        try:
            return db.listing_ids_where(args.where)
        except sqlite3.Error as e:
            parser.error(f"--where: {e}")
    if getattr(args, 'ids_from', None):
        ids = []
        with open(args.ids_from) as f:
            for n, line in enumerate(f, 1):
                line = line.split('#', 1)[0].strip()
                if not line:
                    continue
                if not line.isdigit():
                    parser.error(f"--ids-from: line {n} is not a listing id: {line!r}")
                ids.append(int(line))
        return ids
    return None


def mark_scope_stale(db: ActivityDB, args, ids) -> int:
    """queue a scope for re-fetching; rerunning the same scope after an interruption resumes instead of restarting"""
    scope = getattr(args, 'where', None) or 'ids:' + hashlib.sha1(','.join(map(str, sorted(ids))).encode()).hexdigest()
    saved = json.loads(db.get_meta('refetch_scope') or '{}')
    since = saved['since'] if saved.get('scope') == scope else int(time.time())
    db.set_meta('refetch_scope', json.dumps({'scope': scope, 'since': since}))
    return db.mark_stale(ids, fetched_before=since)


def run_discover(args, pipeline: ActivityPipeline) -> dict:
    print("discovering completed auctions...")
    print("=" * 70)
    if args.reset_backfill_cursor:
        pipeline.db.delete_meta('backfill_next_page')
        print("  backfill cursor reset to page 1")
    stats = pipeline.discover(
        start_page=args.start_page,
        max_pages=args.max_pages,
        since_ts=args.since,
        stop_after_known=args.stop_after_known,
        backfill=args.backfill
    )
    print(f"\n{stats['pages']} page(s), {stats['seen']} auctions seen, {stats['new']} new\n")
    return stats


def run_fetch(args, pipeline: ActivityPipeline, urls=None, listing_ids=None, due_refetches=False, rescan=False) -> dict:
    print("fetching bid histories...")
    print("=" * 70)
    stats = pipeline.fetch(
        limit=args.limit,
        since_ts=args.since,
        max_attempts=args.max_attempts,
        upgrade=args.upgrade or rescan,
        follow_history=not args.no_follow_history,
        urls=urls,
        listing_ids=listing_ids,
        due_refetches=due_refetches
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
        params.append(args.since)
    return ''.join(f" AND {c}" for c in clauses), params


def report_overview(db: ActivityDB, raw_path=None):
    row = db.query("""
        SELECT
            COUNT(*) AS discovered,
            SUM(fetched_at IS NOT NULL) AS fetched,
            SUM(fetched_at IS NULL AND fetch_error IS NOT NULL) AS errored,
            SUM(bids_reported IS NOT NULL AND bids_reported != n_bids) AS bid_mismatches,
            SUM(fetched_at IS NOT NULL AND (seller_slug IS NULL OR make IS NULL OR result IS NULL OR result = 'unknown')) AS incomplete,
            SUM(refetch = 1) AS flagged,
            MIN(CASE WHEN fetched_at IS NOT NULL THEN end_ts END) AS first_ts,
            MAX(CASE WHEN fetched_at IS NOT NULL THEN end_ts END) AS last_ts
        FROM auctions
    """)[0]
    # the stored bids against the auction rows that describe them
    bid_rows = db.query("""
        SELECT
            SUM(COALESCE(b.n, 0) != COALESCE(a.n_bids, 0)) AS count_differs,
            SUM(a.high_bid > b.top) AS price_above_top_bid
        FROM auctions a
        LEFT JOIN (SELECT listing_id, COUNT(*) AS n, MAX(amount) AS top FROM bids GROUP BY listing_id) b
            ON b.listing_id = a.listing_id
        WHERE a.fetched_at IS NOT NULL
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
    print(f"  bid count mismatches: {row['bid_mismatches'] or 0:,} (page counter vs parsed bids)")
    print(f"  bid rows vs n_bids  : {bid_rows['count_differs'] or 0:,} differ")
    print(f"  price above top bid : {bid_rows['price_above_top_bid'] or 0:,} (post-auction deals, or a parse problem)")
    print(f"  incomplete rows     : {row['incomplete'] or 0:,} fetched without a seller, make or known result")
    print(f"  flagged to re-fetch : {row['flagged'] or 0:,} (discovery says the result changed)")
    print(f"  members             : {counts['members']:,}")
    print(f"  bids                : {counts['bids']:,}")
    print(f"  vehicles tracked    : {vehicles['vehicles'] or 0:,} ({vehicles['repeat_vehicles'] or 0:,} auctioned more than once)")

    # rows discovery stored carry its no_reserve flag; history-followed ones don't until the feed lists them
    feed_total = db.get_meta('feed_items_total')
    if feed_total:
        from_feed = db.query("SELECT COUNT(*) AS n FROM auctions WHERE no_reserve IS NOT NULL")[0]['n']
        print(f"  results feed        : lists {int(feed_total):,} completed auctions, {from_feed:,} of them stored "
              f"({int(feed_total) - from_feed:,} not discovered yet)")

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

    if raw_path and os.path.exists(raw_path):
        store = RawStore(raw_path)
        try:
            stats = store.stats()
        finally:
            store.close()
        parts = [f"{v['pages']:,} {kind} ({v['bytes'] / 1e6:,.1f} MB)" for kind, v in sorted(stats.items())]
        print(f"  stored pages        : {', '.join(parts) or 'none'}")


def report_leaderboards(db: ActivityDB, args):
    where, params = auction_filters(args, 'a')
    scope = f" ({args.model or 'all models'}{', since ' + fmt_ts(args.since) if args.since else ''})"

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
        params.append(args.since)
    where = ''.join(f" AND {c}" for c in clauses)
    min_resales = args.min_auctions or 2
    scope = f" ({args.model or 'all models'}{', resold since ' + fmt_ts(args.since) if args.since else ''})"

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
    p.add_argument('--max-pages', type=positive_int, help='stop after this many results pages')
    p.add_argument('--start-page', type=positive_int, help='results page to start from')
    p.add_argument('--reset-backfill-cursor', action='store_true', help='start the backfill over from page 1')
    p.add_argument('--backfill', action='store_true',
                   help='walk back through history without stopping at known auctions; resumes where the last backfill stopped')
    p.add_argument('--stop-after-known', type=positive_int, default=2,
                   help='incremental mode: stop after this many pages with nothing new, counting only pages older '
                        'than what the last complete run had already seen')


def add_scope_args(p, verb):
    scope = p.add_mutually_exclusive_group()
    scope.add_argument('--where', help=f"{verb} the listings matching this sql condition on the auctions table, "
                                       "e.g. \"make = 'Porsche' AND end_ts < 1600000000\"")
    scope.add_argument('--ids-from', metavar='FILE', help=f'{verb} the listing ids in this file, one per line')
    return scope


def add_fetch_args(p):
    p.add_argument('--limit', type=positive_int, help='fetch at most this many listing pages, bat history links included')
    p.add_argument('--max-attempts', type=positive_int, default=3,
                   help='give up on a listing after this many failures (reset-errors clears them)')
    p.add_argument('--upgrade', action='store_true',
                   help='also re-fetch listings saved by an older parser version (e.g. before vin tracking)')
    p.add_argument('--no-follow-history', action='store_true',
                   help="don't fetch other auctions of the same car linked from a listing's bat history")


def main(argv=None):
    parser = argparse.ArgumentParser(description='track who sells, bids on and wins bringatrailer auctions')
    parser.add_argument('--db', default=DEFAULT_DB, help='sqlite database path')
    parser.add_argument('--raw-db', help='where fetched pages are kept for reparse (default: next to --db, as <name>_raw.db)')
    sub = parser.add_subparsers(dest='command', required=True)

    discover = sub.add_parser('discover', help='page through completed auction results')
    add_discover_args(discover)

    fetch = sub.add_parser('fetch', help='download bid histories for discovered auctions')
    add_fetch_args(fetch)
    scope = add_scope_args(fetch, 're-fetch')
    scope.add_argument('--url', nargs='+', help='fetch these listing urls (and their bat history) instead of the queue')
    scope.add_argument('--recheck-mismatches', action='store_true',
                       help="re-fetch saved listings whose page counted a different number of bids than were parsed")

    sync = sub.add_parser('sync', help='discover new results, then fetch what was just discovered')
    add_discover_args(sync)
    add_fetch_args(sync)
    sync.add_argument('--all', action='store_true',
                      help='fetch the whole queue, not just what this run discovered and re-fetches that are due')

    for p in (discover, fetch, sync):
        p.add_argument('--since', type=date_arg,
                       help='only auctions ending on or after YYYY-MM-DD (bat history links from them are still followed)')
        p.add_argument('--delay', type=non_negative_float, default=3.0,
                       help="seconds between requests, plus up to 1.5s jitter (robots.txt's crawl-delay is the floor)")
        p.add_argument('--daily-budget', type=budget_arg, metavar='N',
                       help='at most N requests per local day, remembered for later runs; 0 or off to remove')
        p.add_argument('--active-hours', type=active_hours_arg, metavar='HH:MM-HH:MM',
                       help='only send requests in this local window, e.g. 08:00-22:00, remembered for later runs; '
                            'off to remove')

    sub.add_parser('link', help='regroup fetched auctions into vehicles by vin and bat history (no network)')
    reparse = sub.add_parser('reparse', help='run the current parser over stored pages again (no network)')
    add_scope_args(reparse, 'reparse')
    sub.add_parser('reset-errors',
                   help='clear failure counts so listings and history links that hit --max-attempts are tried again (no network)')

    report = sub.add_parser('report', help='summarize selling, bidding and winning activity')
    report.add_argument('--top', type=positive_int, default=15, help='rows per table')
    report.add_argument('--model', help='make or model slug, e.g. bmw or bmw/e46-m3')
    report.add_argument('--since', type=date_arg, help='only auctions ending on or after YYYY-MM-DD')
    report.add_argument('--member', help='member slug for a single-member profile')
    report.add_argument('--vehicle', help='vin, chassis number, listing url or listing id: every bat auction of that car')
    report.add_argument('--resales', action='store_true', help='members who resell cars they won, with hold time and price change')
    report.add_argument('--pairs', action='store_true', help='show repeat seller/bidder relationships')
    report.add_argument('--min-auctions', type=positive_int,
                        help='minimum shared auctions for --pairs (default 3) or resales for --resales (default 2)')

    args = parser.parse_args(argv)
    config = load_config()

    urls = None
    if getattr(args, 'url', None):
        try:
            urls = [listing_url(u, config['site']['base_url']) for u in args.url]
        except ValueError as e:
            parser.error(f"--url: {e}")

    if args.command not in LOCKED_COMMANDS:
        return run_command(args, parser, config, urls)
    try:
        with run_lock(args.db):
            return run_command(args, parser, config, urls)
    except LockHeld as e:
        print(f"{e}; not starting a second run")
        return EXIT_LOCKED


def run_command(args, parser: argparse.ArgumentParser, config: dict, urls) -> int:
    db = ActivityDB(args.db)
    raw_path = args.raw_db or raw_db_path(args.db)
    raw_store = None

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
                report_overview(db, raw_path)
                report_leaderboards(db, args)
            print()
            return 0

        if args.command == 'reparse':
            if not raw_path or not os.path.exists(raw_path):
                print(f"no stored pages at {raw_path}; only listings fetched with the raw store can be reparsed")
                return 1
            raw_store = RawStore(raw_path)
            pipeline = build_pipeline(args, db, config, raw_store)
            print("reparsing stored pages...")
            print("=" * 70)
            stats = pipeline.reparse(scope_ids(args, db, parser))
            print(f"\n{stats['reparsed']:,} listing(s) reparsed, {stats['failed']:,} failed")
            print_vehicle_stats(stats['vehicles'])
            return 1 if stats['failed'] and not stats['reparsed'] else 0

        raw_store = RawStore(raw_path) if raw_path and args.command in ('fetch', 'sync') else None
        pipeline = build_pipeline(args, db, config, raw_store)
        status = 0
        try:
            discovered = None
            if args.command in ('discover', 'sync'):
                discovered = run_discover(args, pipeline)
            if args.command in ('fetch', 'sync'):
                ids = scope_ids(args, db, parser)
                rescan = ids is not None
                if rescan:
                    marked = mark_scope_stale(db, args, ids)
                    print(f"{len(ids):,} listing(s) in scope, {marked:,} newly queued for a re-fetch")
                # a daily sync fetches what it just discovered (and re-fetches that are due), not the whole backlog
                daily = args.command == 'sync' and not args.all
                if daily:
                    ids = discovered['seen_ids']
                stats = run_fetch(args, pipeline, urls, ids, due_refetches=daily, rescan=rescan)
                if rescan and not db.pending(max_attempts=args.max_attempts, upgrade_below=PARSER_VERSION, listing_ids=ids):
                    db.delete_meta('refetch_scope')
                if stats['fetched'] == 0 and stats['failed'] > 0:
                    status = 1
        except KeyboardInterrupt:
            print("\ninterrupted, progress so far is saved")
            status = EXIT_INTERRUPTED
        except RateLimited as e:
            print(f"\nstopped: {e}")
            print(f"resume after {fmt_utc(e.resume_at)}")
            status = EXIT_STOPPED
        except (SiteUnavailable, CircuitOpen) as e:
            print(f"\nstopped: {e}")
            print(STOP_HINTS[getattr(e, 'kind', 'site')])
            status = EXIT_STOPPED
        print(f"{pipeline.client.request_count} request(s) made, database at {args.db}")
        return status
    finally:
        db.close()
        if raw_store:
            raw_store.close()


if __name__ == '__main__':
    sys.exit(main())
