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
from sites.bringatrailer.listing_specs import ListingSpecs
from storage.activity_db import ActivityDB
from storage.raw_store import RawStore, raw_db_path
from core.models.model_definition import ModelDefinition, load_model_file, normalize_slug, same_slug
from sites.bringatrailer.model_page import params_from_url
from pipelines.activity_pipeline import ActivityPipeline, CircuitOpen, fmt_ts
from pipelines.model_pipeline import ModelPipeline, ModelSetupError, page_scope
from pipelines.model_prices import (
    mileage_band, mileage_band_order, monthly, price_groups, sale_period, split_rows, summarize, is_usd_sale
)
from extractors.variant import extract_variant
from pipelines.model_export import OLD_FILTERS, export_rows, write_csv, write_json
from pipelines.crawl_budget import CrawlBudget, parse_active_hours


ROOT = os.path.join(os.path.dirname(__file__), '../..')
CONFIG_PATH = os.path.join(ROOT, 'config/sites/bringatrailer.yaml')
DEFAULT_DB = os.path.join(ROOT, 'data/db/bat_activity.db')
# where the selenium scraper wrote <slug>_data.json, which normalize, ingest and the llm step read
EXPORT_DIR = os.path.join(ROOT, 'data/json/output/raw')


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
LOCKED_COMMANDS = ('discover', 'fetch', 'sync', 'model', 'link', 'reparse', 'reset-errors')

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
    parser = ActivityParser(config['activity']['selectors'], ListingSpecs(config))
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


class ModelRefError(Exception):
    """a model reference that names several followed models, or can't be used as given"""


def model_definitions(args, db: ActivityDB, parser: argparse.ArgumentParser):
    """the models a `model` run follows, and the keys of those this run added: the --json file's entries, or the slug
    given, each matched to a followed model by its key or either spelling of a slug. a followed model keeps its feed
    filter and tags; --make etc. and the file's names and years update it"""
    created = set()
    if args.json:
        try:
            entries = load_model_file(args.json)
        except (OSError, ValueError, KeyError, TypeError) as e:
            parser.error(f"--json: {e}")
        models = []
        for entry in entries:
            stored = {m.key: m for slug in entry.slugs for m in db.find_models(slug)}
            if len(stored) > 1:
                raise ModelRefError(f"--json entry {entry.key} names {len(stored)} followed models "
                                    f"({', '.join(sorted(stored))}); give it one model's slugs")
            if stored:
                known = next(iter(stored.values()))
                entry.key, entry.filters, entry.tag_slugs = known.key, known.filters, known.tag_slugs
                entry.slugs = list(dict.fromkeys(known.slugs + entry.slugs))
            else:
                created.add(entry.key)
            models.append(entry)
    else:
        slug = normalize_slug(args.slug)
        if not slug:
            parser.error(f"{args.slug!r} is not a model slug")
        matches = db.find_models(slug)
        if len(matches) > 1:
            raise ModelRefError(f"{args.slug!r} names {len(matches)} followed models "
                                f"({', '.join(m.key for m in matches)}); give one of those keys")
        if matches:
            model = matches[0]
            # one that never got a feed may just have had the wrong spelling: this one's page is tried too
            if not model.filters and slug not in model.slugs:
                model.slugs.append(slug)
        else:
            model = ModelDefinition(key=slug, slugs=[slug])
            created.add(slug)
        models = [model]

    for model in models:
        for attr in ('make', 'model_full', 'model_short', 'min_year', 'max_year'):
            if getattr(args, attr) is not None:
                setattr(model, attr, getattr(args, attr))
    if args.filter_url:
        model = models[0]
        target = next((s for s in model.slugs if args.slug and same_slug(s, args.slug)), None)
        if len(models) != 1 or (target is None and len(model.slugs) > 1):
            raise ModelRefError("--filter-url needs one model page: give its slug rather than --json or a model key")
        target = target or model.slugs[0]
        try:
            model.filters[target] = params_from_url(args.filter_url)
        except ValueError as e:
            parser.error(f"--filter-url: {e}")
        # kept until --refresh-filter, not replaced by the page's own a week later
        db.set_meta(page_scope(model, target) + 'manual', '1')
    for model in models:
        db.save_model(model)
        # a changed year range re-sorts the auctions already recorded for the model
        db.apply_year_range(model)
    return models, created


def run_model(args, pipeline: ActivityPipeline, models, created=frozenset()) -> int:
    """each model in turn: its auctions from its model page's feed (or titles), then their bid histories and
    bat history links, then its report. --limit is the whole run's budget of listing pages. a model this run
    added and found nothing for isn't kept"""
    db = pipeline.db
    discovery = ModelPipeline(pipeline, pipeline.client.base_url)
    remaining = args.limit
    status = 0
    fetched = failed = 0
    for n, model in enumerate(models, 1):
        names = ' '.join(x for x in (model.make, model.model_full) if x)
        print("=" * 70)
        print(f"[{n}/{len(models)}] {model.key}{f'  ({names})' if names else ''}")
        print("=" * 70)
        print("discovering the model's auctions...")
        try:
            found = discovery.discover(model, since_ts=args.since, max_pages=args.max_pages,
                                       refresh_filter=args.refresh_filter)
        except ModelSetupError as e:
            print(f"  {e}")
            if model.key in created and db.delete_model(model.key):
                print(f"  {model.key} isn't kept as a followed model")
            print()
            status = 1
            continue
        print(f"\n{found['pages']} page(s), {found['seen']} auctions seen, {found['new']} new\n")

        ids = db.model_listing_ids(model)
        if remaining is not None and remaining <= 0:
            print("the --limit budget is spent; the next run fetches the rest\n")
        else:
            print("fetching bid histories...")
            print("=" * 70)
            stats = pipeline.fetch(limit=remaining, since_ts=args.since, max_attempts=args.max_attempts,
                                   upgrade=args.upgrade, follow_history=not args.no_follow_history,
                                   listing_ids=ids, history_from=ids)
            print(f"\n{stats['fetched']} auction(s) saved, {stats['bids']} bids, "
                  f"{stats['followed']} history links followed, {stats['failed']} failed")
            print_vehicle_stats(stats['vehicles'])
            if remaining is not None:
                remaining -= stats['fetched'] + stats['failed'] + stats['not_final']
            fetched += stats['fetched']
            failed += stats['failed']
        if not args.upgrade:
            older = db.query(f"""
                SELECT COUNT(*) AS n FROM auctions WHERE fetched_at IS NOT NULL AND COALESCE(parser_version, 0) < ?
                  AND listing_id IN (SELECT value FROM json_each(?))
            """, (PARSER_VERSION, json.dumps(ids)))[0]['n']
            if older:
                print(f"{older:,} of the model's auctions were saved by an older parser, without engine, mileage and "
                      f"the like; --upgrade fetches them again\n")

        checked = db.classify_model_candidates(model)
        if checked['member'] or checked['other_model']:
            print(f"title matches checked: {checked['member']} are the model, {checked['other_model']} another model's")
            if checked['other_tags']:
                tags = ', '.join(f"{tag} ({n})" for tag, n in checked['other_tags'].most_common(8))
                print(f"  ruled out, tagged as: {tags}; if any of these are the model, add them to its slugs")
            print()
        if not args.no_report:
            report_model(db, argparse.Namespace(model=model.key, model_def=model, since=args.since, top=args.top))
    # as for fetch and sync: the whole run fetched nothing, and something failed
    if fetched == 0 and failed > 0:
        status = 1
    return status


def report_model(db: ActivityDB, args):
    """what a model's auctions sold for, then who sold, bid on and bought them"""
    report_prices(db, args)
    report_leaderboards(db, args)
    print()


def miles(value) -> str:
    return '-' if value is None else f"{value:,}"


def report_prices(db: ActivityDB, args):
    """sale prices of the scoped auctions (fetched, cars only, usd), overall and by period, model year,
    transmission and mileage, then the latest sales"""
    where, params = auction_filters(db, args, 'a')
    rows = [dict(r) for r in db.query(f"""
        SELECT a.listing_id, a.url, a.title, a.year, a.make, a.end_ts, a.result, a.high_bid, a.currency,
               a.transmission, a.mileage, a.exterior_color, a.fetched_at,
               s.display_name AS seller, w.display_name AS buyer
        FROM auctions a
        LEFT JOIN members s ON s.slug = a.seller_slug
        LEFT JOIN members w ON w.slug = a.winner_slug
        WHERE 1 = 1{where}
    """, params)]
    fetched = [r for r in rows if r['fetched_at']]
    split = split_rows(fetched)
    cars = split['cars']
    total = summarize(cars)

    print("=" * 70)
    print(f"{scope_name(args)} prices{' since ' + fmt_ts(args.since) if args.since else ''}")
    print("=" * 70)
    print(f"  auctions      : {total['auctions']:,} fetched: {total['sold']:,} sold, "
          f"{total['reserve_not_met']:,} reserve not met, {total['withdrawn']:,} withdrawn")
    print(f"  sell-through  : {pct(total['sell_through'])} (sold, of those sold or bid to the reserve)")
    # counted as sold above, but their prices can't be summed in dollars
    unpriced = [f"{n:,} sale(s) {what}" for n, what in (
        (split['other_currency'], 'in other currencies'), (split['no_price'], 'without a price')) if n]
    print(f"  sale prices   : median {money(total['median'])}, low {money(total['low'])}, "
          f"high {money(total['high'])} (USD{'; not counting ' + ' or '.join(unpriced) if unpriced else ''})")
    left_out = [f"{n:,} {what}" for n, what in (
        (split['parts'], 'parts listing(s)'),
        (len(rows) - len(fetched), "of the model's auctions not fetched yet"),
    ) if n]
    if left_out:
        print(f"  not counted   : {', '.join(left_out)}")
    if not cars:
        return

    columns = [('sold', 'sold', text), ('sell_through', 'sell-thru', pct), ('median', 'median', money),
               ('low', 'low', money), ('high', 'high', money)]
    by_month = monthly(cars)
    dated = [r for r in cars if r['end_ts']]
    print_table(f"by {'month' if by_month else 'quarter'} of sale", price_groups(dated, lambda r: sale_period(r['end_ts'], by_month)),
                [('group', 'month' if by_month else 'quarter', text)] + columns)
    print_table("by model year", price_groups(cars, lambda r: str(r['year']) if r['year'] else 'unknown',
                                              lambda g: (g == 'unknown', g)),
                [('group', 'year', text)] + columns)
    counts = {}
    for r in cars:
        counts[r['transmission'] or 'unknown'] = counts.get(r['transmission'] or 'unknown', 0) + 1
    print_table("by transmission", price_groups(cars, lambda r: r['transmission'] or 'unknown',
                                                lambda g: (g == 'unknown', -counts[g], g)),
                [('group', 'transmission', truncate(40))] + columns)
    print_table("by mileage", price_groups(cars, lambda r: mileage_band(r['mileage']), mileage_band_order),
                [('group', 'miles', text)] + columns)

    model_def = getattr(args, 'model_def', None)
    recent = sorted((r for r in cars if is_usd_sale(r)), key=lambda r: r['end_ts'] or 0, reverse=True)[:args.top]
    variant_columns = []
    if model_def and model_def.make and model_def.model_short:
        for r in recent:
            r['variant'] = extract_variant(r['title'] or '', model_def.make, model_def.model_short)
        variant_columns = [('variant', 'variant', truncate(24))]
    title = "recent sales" if variant_columns else "recent sales (the variant needs the model's --make and --model-short)"
    print_table(title, recent, [('end_ts', 'date', fmt_ts), ('year', 'year', text)] + variant_columns + [
        ('mileage', 'miles', miles), ('transmission', 'transmission', truncate(28)),
        ('exterior_color', 'exterior', truncate(24)), ('high_bid', 'price', money), ('seller', 'seller', text),
        ('buyer', 'buyer', text), ('url', 'url', text)
    ])
    print()


def resolve_model(db: ActivityDB, ref: str):
    """the followed model a --model names, None for a slug that isn't followed; ValueError if it names several"""
    matches = db.find_models(ref)
    if len(matches) > 1:
        raise ValueError(f"--model {ref} names {len(matches)} followed models: {', '.join(m.key for m in matches)}; "
                         "give one of those keys")
    return matches[0] if matches else None


EXPORT_SKIPS = {
    'non_usa': 'outside the USA', 'modified': "with 'modified' in the title", 'no_vin': 'without a 17-character vin', 'parts': 'parts listings', 'withdrawn': 'withdrawn',
}


def run_export(db: ActivityDB, args, config: dict) -> int:
    """a model's fetched auctions as the selenium scraper's <slug>_data.json, which normalize, ingest and the llm
    step read. by default it leaves out what that scraper left out; --all and the --include-* flags keep them"""
    try:
        model_def = resolve_model(db, args.model)
    except ValueError as e:
        print(e)
        return 1
    if not model_def:
        print(f"{args.model} isn't a followed model, so its auctions are picked by their model tags, and make and "
              f"model come from the listing pages")
    where, params = auction_filters(db, argparse.Namespace(model=args.model, model_def=model_def, since=args.since), 'a')
    keep = set(OLD_FILTERS) if args.all else {name for name in OLD_FILTERS if getattr(args, f"include_{name}")}
    result = export_rows(db, where, params, model_def, config['site']['source_name'], keep, withdrawn=args.all)
    rows = result['rows']

    waiting = db.query(f"SELECT COUNT(*) AS n FROM auctions a WHERE a.fetched_at IS NULL{where}", params)[0]['n']
    skipped = [f"{n:,} {EXPORT_SKIPS.get(reason, reason)}" for reason, n in sorted(result['skipped'].items())]
    if skipped:
        print(f"left out: {', '.join(skipped)}")
    if waiting:
        print(f"{waiting:,} of the model's auctions aren't fetched yet; `model {args.model}` fetches them")
    foreign = sum(1 for r in rows if r.row['result'] == 'sold' and r.row['currency'] != 'USD')
    if foreign:
        print(f"{foreign:,} sale(s) in other currencies are written without a price (the csv has the amount)")
    if not rows:
        print("nothing to export")
        return 1
    # ingest files a whole file under its first record's make and model
    tags = sorted({r.row['model_slug'] or '?' for r in rows})
    if not model_def and len(tags) > 1:
        print(f"{args.model} covers {len(tags)} models ({', '.join(tags[:6])}{', ...' if len(tags) > 6 else ''}); "
              f"export one of them, or follow the model with `model` first")
        return 1

    older = sum(1 for r in rows if (r.row['parser_version'] or 0) < PARSER_VERSION)
    if older:
        print(f"{older:,} of these were saved by an older parser, so their engine, mileage, colors and the like are "
              f"missing; `model {args.model} --upgrade` fetches them again")
    if model_def:
        missing = [flag for flag, value in (('--make', model_def.make), ('--model-full', model_def.model_full),
                                            ('--model-short', model_def.model_short)) if not value]
        if missing:
            print(f"{model_def.key} has no {', '.join(missing)}, so "
                  f"{'every variant is Standard and ' if '--model-short' in missing or '--make' in missing else ''}"
                  f"make and model come from the listing pages; `model {model_def.key} {' '.join(m + ' ...' for m in missing)}` "
                  f"sets them")

    name = model_def.output_name() if model_def else normalize_slug(args.model).replace('/', '-')
    path = args.output or os.path.join(EXPORT_DIR, f"{name}_data.{args.format}")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    (write_csv if args.format == 'csv' else write_json)(rows, path)
    sold = sum(1 for r in rows if r.listing.result == 'Sold')
    print(f"{len(rows):,} auction(s) ({sold:,} sold) written to {path}")
    return 0


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
    notes = []
    if stats['vin_conflicts']:
        notes.append(f"{stats['vin_conflicts']:,} linked by bat history despite differing vins")
    if stats.get('mixed'):
        notes.append(f"{stats['mixed']:,} whose listings disagree on the vin, chassis or model")
    if stats.get('dated'):
        notes.append(f"{stats['dated']:,} undated auction(s) dated from bat history")
    extra = ''.join(f", {n}" for n in notes)
    print(f"{stats['vehicles']:,} vehicles tracked, {stats['repeat_vehicles']:,} auctioned more than once{extra}\n")


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


def auction_filters(db: ActivityDB, args, alias: str):
    clauses, params = [], []
    model_def = getattr(args, 'model_def', None)
    if model_def:
        # a model followed with `model`: what its feed listed, plus auctions tagged as it
        condition, scope_params = db.model_scope(model_def, alias)
        clauses.append(condition)
        params += scope_params
    elif args.model:
        # a make takes in its models ("bmw" covers "bmw/e46-m3"); a range, unlike LIKE, can use the index. a short
        # model slug ("e46-m3") is the tag's last part
        model = normalize_slug(args.model)
        clauses.append(f"({alias}.model_slug = ? OR ({alias}.model_slug >= ? AND {alias}.model_slug < ?)"
                       f" OR substr({alias}.model_slug, -?) = ?)")
        params += [model, f"{model}/", f"{model}0", len(model) + 1, f"/{model}"]
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
    # the stored bids against the auction rows that describe them (participants sums the bids per listing,
    # and is rebuilt from them by `link`)
    bid_rows = db.query("""
        SELECT
            SUM(COALESCE(b.n, 0) != COALESCE(a.n_bids, 0)) AS count_differs,
            SUM(a.high_bid > b.top) AS price_above_top_bid
        FROM auctions a
        LEFT JOIN (SELECT listing_id, SUM(n_bids) AS n, MAX(max_bid) AS top FROM participants GROUP BY listing_id) b
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


def scope_name(args) -> str:
    model_def = getattr(args, 'model_def', None)
    return (model_def.model_full or model_def.key) if model_def else (args.model or 'all models')


def report_leaderboards(db: ActivityDB, args):
    where, params = auction_filters(db, args, 'a')
    scope = f" ({scope_name(args)}{', since ' + fmt_ts(args.since) if args.since else ''})"
    # the matching auctions in one pass, so no leaderboard walks an index a row at a time; member names are
    # looked up only for the rows shown
    scoped = f"""scoped AS MATERIALIZED (
        SELECT a.listing_id, a.seller_slug, a.seller_type, a.result, a.currency, a.high_bid, a.winner_slug, a.fetched_at
        FROM auctions a WHERE 1 = 1{where}
    )"""

    sellers = db.query(f"""
        WITH {scoped},
        top AS (
            SELECT seller_slug AS slug, MAX(seller_type) AS seller_type, COUNT(*) AS listed, SUM(result = 'sold') AS sold,
                   ROUND(1.0 * SUM(result = 'sold') / COUNT(*), 3) AS sell_through,
                   SUM(CASE WHEN result = 'sold' AND currency = 'USD' THEN high_bid END) AS sold_usd
            FROM scoped WHERE fetched_at IS NOT NULL AND seller_slug IS NOT NULL
            GROUP BY seller_slug ORDER BY listed DESC, sold_usd DESC, seller_slug LIMIT ?
        )
        SELECT top.*, m.display_name FROM top LEFT JOIN members m ON m.slug = top.slug
        ORDER BY top.listed DESC, top.sold_usd DESC, top.slug
    """, params + [args.top])
    print_table("top sellers" + scope, sellers, [
        ('display_name', 'member', text), ('seller_type', 'type', text), ('listed', 'listed', text),
        ('sold', 'sold', text), ('sell_through', 'sell-thru', pct), ('sold_usd', 'gross sold', money)
    ])

    # filtered, the bidders on the matching auctions; unfiltered, the participants table's own index has it
    if where:
        counted = f"WITH {scoped} SELECT p.* FROM scoped a CROSS JOIN participants p ON p.listing_id = a.listing_id"
    else:
        counted = "SELECT * FROM participants"
    bidders = db.query(f"""
        WITH top AS (
            SELECT slug, COUNT(*) AS auctions_bid, SUM(n_bids) AS bids, SUM(won) AS won,
                   ROUND(1.0 * SUM(won) / COUNT(*), 3) AS win_rate
            FROM ({counted})
            GROUP BY slug ORDER BY auctions_bid DESC, bids DESC, slug LIMIT ?
        )
        SELECT top.*, m.display_name FROM top LEFT JOIN members m ON m.slug = top.slug
        ORDER BY top.auctions_bid DESC, top.bids DESC, top.slug
    """, params + [args.top])
    print_table("most active bidders" + scope, bidders, [
        ('display_name', 'member', text), ('auctions_bid', 'auctions', text), ('bids', 'bids', text),
        ('won', 'won', text), ('win_rate', 'win rate', pct)
    ])

    winners = db.query(f"""
        WITH {scoped},
        top AS (
            SELECT winner_slug AS slug, COUNT(*) AS won,
                   SUM(CASE WHEN currency = 'USD' THEN high_bid END) AS won_usd,
                   MAX(CASE WHEN currency = 'USD' THEN high_bid END) AS top_usd
            FROM scoped WHERE result = 'sold' AND winner_slug IS NOT NULL
            GROUP BY winner_slug ORDER BY won DESC, won_usd DESC, winner_slug LIMIT ?
        )
        SELECT top.*, m.display_name FROM top LEFT JOIN members m ON m.slug = top.slug
        ORDER BY top.won DESC, top.won_usd DESC, top.slug
    """, params + [args.top])
    print_table("top buyers" + scope, winners, [
        ('display_name', 'member', text), ('won', 'won', text),
        ('won_usd', 'total spent', money), ('top_usd', 'biggest win', money)
    ])


def report_pairs(db: ActivityDB, args):
    where, params = auction_filters(db, args, 'a')
    min_auctions = args.min_auctions or 3
    scope = f", {scope_name(args)}{', since ' + fmt_ts(args.since) if args.since else ''}" if where else ''
    if where:
        pairs = f"""
            SELECT a.seller_slug, p.slug AS bidder_slug, COUNT(*) AS auctions_bid, SUM(p.won) AS won, SUM(p.n_bids) AS bids
            FROM auctions a CROSS JOIN participants p ON p.listing_id = a.listing_id
            WHERE a.seller_slug IS NOT NULL{where}
            GROUP BY a.seller_slug, p.slug
        """
    else:
        pairs = "SELECT * FROM seller_bidder_pairs"
    rows = db.query(f"""
        WITH top AS (
            SELECT * FROM ({pairs}) WHERE auctions_bid >= ?
            ORDER BY auctions_bid DESC, won DESC, seller_slug, bidder_slug LIMIT ?
        )
        SELECT s.display_name AS seller, b.display_name AS bidder, top.auctions_bid, top.bids, top.won
        FROM top
        LEFT JOIN members s ON s.slug = top.seller_slug
        LEFT JOIN members b ON b.slug = top.bidder_slug
        ORDER BY top.auctions_bid DESC, top.won DESC, top.seller_slug, top.bidder_slug
    """, params + [min_auctions, args.top])
    print_table(f"repeat seller/bidder pairs (>= {min_auctions} auctions together{scope})", rows, [
        ('seller', 'seller', text), ('bidder', 'bidder', text),
        ('auctions_bid', 'auctions', text), ('bids', 'bids', text), ('won', 'won', text)
    ])


def report_vehicle(db: ActivityDB, args):
    matches = db.find_vehicles(args.vehicle)
    if not matches:
        print(f"\nno tracked vehicle matches '{args.vehicle}' (use a vin, chassis number, listing url or listing id)")
        return
    if len(matches) > 1:
        print_table(f"'{args.vehicle}' matches {len(matches)} cars; pick one by listing url or listing id", matches, [
            ('listing_id', 'listing id', text), ('auctions', 'auctions', text), ('title', 'title', truncate(50))
        ])
        return
    vehicle_id = matches[0]['vehicle_id']

    rows = [dict(r) for r in db.query("""
        SELECT t.*, s.display_name AS seller, w.display_name AS buyer
        FROM vehicle_timeline t
        LEFT JOIN members s ON s.slug = t.seller_slug
        LEFT JOIN members w ON w.slug = t.winner_slug
        WHERE t.vehicle_id = ? ORDER BY t.seq
    """, (vehicle_id,))]
    # auctions of this car bat history knows about that aren't fetched, discovered or not
    missing = db.query("""
        SELECT COALESCE(r.url, l.related_url) AS url, MAX(COALESCE(r.end_ts, l.related_end_ts)) AS ended,
               MAX(l.summary) AS summary, MAX(r.listing_id IS NOT NULL) AS discovered
        FROM listing_links l
        JOIN auctions a ON a.listing_id = l.listing_id
        LEFT JOIN auctions r ON r.listing_id = l.related_listing_id
        WHERE a.vehicle_id = ? AND (l.related_listing_id IS NULL OR r.fetched_at IS NULL)
        GROUP BY COALESCE(CAST(l.related_listing_id AS TEXT), l.related_url)
        ORDER BY ended IS NULL, ended
    """, (vehicle_id,))

    # a label that compares across a missing auction may change once it's fetched
    gaps = [m['ended'] for m in missing if m['ended']]
    for row in rows:
        after = row['prev_end_ts'] if row['seq'] > 1 else float('-inf')
        before = row['end_ts'] if row['end_ts'] is not None else float('inf')
        if any((after or float('-inf')) < ts <= before for ts in gaps):
            row['transition'] += ' (provisional)'
    chassis = sorted({r['vin'] or r['chassis'] for r in rows if r['vin'] or r['chassis']})

    print("=" * 70)
    print(rows[-1]['title'])
    print(f"  chassis: {', '.join(chassis) or '?'}  |  {len(rows)} of {len(rows) + len(missing)} known auction(s) fetched")
    print("=" * 70)
    print_table("auction history", rows, [
        ('end_ts', 'ended', fmt_ts), ('result', 'result', label), ('high_bid', 'price', money),
        ('seller', 'seller', text), ('buyer', 'buyer', text), ('transition', 'what happened', label),
        ('days_since_prev', 'days since prev', days), ('change_since_prev', 'price change', signed_money)
    ])
    if missing:
        print_table("auctions of this car not fetched yet", missing, [
            ('ended', 'ended', fmt_ts), ('discovered', 'discovered', lambda v: 'yes' if v else 'no'),
            ('summary', 'summary', text), ('url', 'url', text)
        ])
        undated = len(missing) - len(gaps)
        if undated:
            print(f"  {undated} of them without a date, so any label here may change")


def report_resales(db: ActivityDB, args):
    # --since here means resold since, so only the model half of the usual filters applies to the auction
    where, params = auction_filters(db, argparse.Namespace(model=args.model, model_def=args.model_def, since=None), 'a')
    if args.since:
        where += " AND r.sold_ts >= ?"
        params.append(args.since)
    min_resales = args.min_auctions or 2
    scope = f" ({scope_name(args)}{', resold since ' + fmt_ts(args.since) if args.since else ''})"

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
    """one member's profile, read from the base tables by slug so it's quick however large the database"""
    slug = args.member.lower()
    member = db.query("SELECT slug, display_name, user_id FROM members WHERE slug = ?", (slug,))
    if not member:
        print(f"\nno member '{slug}' in the database")
        return
    m = member[0]

    selling = db.query("""
        SELECT COUNT(*) AS listed, COALESCE(SUM(result = 'sold'), 0) AS sold,
               COALESCE(SUM(CASE WHEN result = 'sold' AND currency = 'USD' THEN high_bid END), 0) AS sold_usd,
               MIN(end_ts) AS first_ts, MAX(end_ts) AS last_ts
        FROM auctions WHERE seller_slug = ? AND fetched_at IS NOT NULL
    """, (slug,))[0]
    bidding = db.query("""
        SELECT COUNT(*) AS auctions_bid, COALESCE(SUM(n_bids), 0) AS bids, COALESCE(SUM(won), 0) AS won_bid_on,
               MIN(first_bid_ts) AS first_ts, MAX(last_bid_ts) AS last_ts
        FROM participants WHERE slug = ?
    """, (slug,))[0]
    winning = db.query("""
        SELECT COUNT(*) AS won, COALESCE(SUM(CASE WHEN a.currency = 'USD' THEN a.high_bid END), 0) AS won_usd,
               COALESCE(SUM(p.listing_id IS NULL), 0) AS without_bid
        FROM auctions a LEFT JOIN participants p ON p.listing_id = a.listing_id AND p.slug = a.winner_slug
        WHERE a.winner_slug = ? AND a.result = 'sold'
    """, (slug,))[0]
    # the share of the auctions they bid on that they won
    win_rate = bidding['won_bid_on'] / bidding['auctions_bid'] if bidding['auctions_bid'] else None
    seen = [t for t in (selling['first_ts'], bidding['first_ts'], selling['last_ts'], bidding['last_ts']) if t]
    no_bid = f"; {winning['without_bid']} of them without a bid of theirs" if winning['without_bid'] else ''

    print("=" * 70)
    print(f"{m['display_name'] or slug}  (member/{slug}/, id {text(m['user_id'])})")
    print("=" * 70)
    print(f"  active       : {fmt_ts(min(seen) if seen else None)} to {fmt_ts(max(seen) if seen else None)}")
    print(f"  selling      : {selling['listed']} listed, {selling['sold']} sold, {money(selling['sold_usd'])} gross")
    print(f"  bidding      : {bidding['bids']} bids across {bidding['auctions_bid']} auctions")
    print(f"  winning      : {winning['won']} won ({pct(win_rate)} of auctions bid), {money(winning['won_usd'])} spent{no_bid}")

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
        SELECT a.end_ts, p.max_bid, a.high_bid, p.won, a.title, s.display_name AS seller
        FROM participants p
        JOIN auctions a ON a.listing_id = p.listing_id
        LEFT JOIN members s ON s.slug = a.seller_slug
        WHERE p.slug = ? ORDER BY a.end_ts DESC LIMIT ?
    """, (slug, args.top))
    print_table("bid on", bid_on, [
        ('end_ts', 'ended', fmt_ts), ('max_bid', 'their max', money), ('high_bid', 'final', money),
        ('won', 'won', lambda v: 'yes' if v else ''), ('title', 'title', truncate(40)), ('seller', 'seller', text)
    ])

    makes = db.query("""
        SELECT COALESCE(a.make, '?') AS make, COUNT(*) AS auctions_bid, SUM(p.won) AS won
        FROM participants p JOIN auctions a ON a.listing_id = p.listing_id
        WHERE p.slug = ?
        GROUP BY a.make ORDER BY auctions_bid DESC LIMIT ?
    """, (slug, args.top))
    print_table("makes bid on", makes, [('make', 'make', text), ('auctions_bid', 'auctions', text), ('won', 'won', text)])

    buys_from = db.query("""
        SELECT m.display_name AS seller, COUNT(*) AS auctions_bid, SUM(p.won) AS won
        FROM participants p LEFT JOIN members m ON m.slug = p.seller_slug
        WHERE p.slug = ? AND p.seller_slug IS NOT NULL
        GROUP BY p.seller_slug ORDER BY auctions_bid DESC, won DESC LIMIT ?
    """, (slug, args.top))
    print_table("sellers they bid on most", buys_from, [
        ('seller', 'seller', text), ('auctions_bid', 'auctions', text), ('won', 'won', text)
    ])

    bidders = db.query("""
        SELECT m.display_name AS bidder, COUNT(*) AS auctions_bid, SUM(p.won) AS won
        FROM participants p LEFT JOIN members m ON m.slug = p.slug
        WHERE p.seller_slug = ?
        GROUP BY p.slug ORDER BY auctions_bid DESC, won DESC LIMIT ?
    """, (slug, args.top))
    print_table("who bids on their cars", bidders, [
        ('bidder', 'bidder', text), ('auctions_bid', 'auctions', text), ('won', 'won', text)
    ])

    print_table("cars they won and later resold on bat", db.resales_of(slug, args.top), [
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

    model = sub.add_parser('model', help="follow a model: its auctions from its model page's feed, their bid "
                                         "histories, and a report")
    model.add_argument('slug', nargs='?', help="the model page's slug, as in its address: 'chevrolet/c8', or the old "
                                               "scraper's 'e46-m3'; a stored model's key also works")
    model.add_argument('--json', metavar='FILE', help='follow the models in this file (the cli/input/cars_*.json format)')
    model.add_argument('--make', help='make, for the export and for matching titles when the model page gives no feed')
    model.add_argument('--model-full', help="model name for the export's model field, e.g. 'C8 Corvette'")
    model.add_argument('--model-short', help="the model as titles write it, e.g. 'Corvette ' (variant and title matching)")
    model.add_argument('--min-year', type=int, help='skip auctions of model years before this')
    model.add_argument('--max-year', type=int, help='skip auctions of model years after this')
    model.add_argument('--filter-url', metavar='URL',
                       help="the listings-filter request the model page's 'show more' sends, copied from the browser's "
                            "network tab; used instead of reading the model page")
    model.add_argument('--refresh-filter', action='store_true', help='read the model page again for its feed filter')
    model.add_argument('--max-pages', type=positive_int, help='read at most this many feed pages')
    add_fetch_args(model)
    model.add_argument('--no-report', action='store_true', help="don't print the model's report at the end")
    model.add_argument('--top', type=positive_int, default=15, help='rows per report table')

    for p in (discover, fetch, sync, model):
        p.add_argument('--since', type=date_arg,
                       help='only auctions ending on or after YYYY-MM-DD (bat history links from them are still followed)')
        p.add_argument('--delay', type=non_negative_float, default=3.0,
                       help="seconds between requests, plus up to 1.5s jitter (robots.txt's crawl-delay is the floor)")
        p.add_argument('--daily-budget', type=budget_arg, metavar='N',
                       help='at most N requests per local day, remembered for later runs; 0 or off to remove')
        p.add_argument('--active-hours', type=active_hours_arg, metavar='HH:MM-HH:MM',
                       help='only send requests in this local window, e.g. 08:00-22:00, remembered for later runs; '
                            'off to remove')

    export = sub.add_parser('export', help="write a model's auctions as the selenium scraper's json, for normalize, "
                                           "ingest and the llm step (no network)")
    export.add_argument('--model', required=True, help="a followed model's key or slug, in either spelling")
    export.add_argument('--output', metavar='FILE',
                        help='where to write (default: data/json/output/raw/<slug>_data.json, or .csv)')
    export.add_argument('--format', choices=('json', 'csv'), default='json',
                        help="json: the old files' shape exactly; csv: the same fields plus the crawler's ids and counts")
    export.add_argument('--since', type=date_arg, help='only auctions ending on or after YYYY-MM-DD')
    export.add_argument('--all', action='store_true',
                        help="keep everything the old scraper left out: the filters below, and withdrawn auctions. the "
                             "model's year range always applies (change it with `model --min-year/--max-year`)")
    export.add_argument('--include-non-usa', action='store_true', help='keep listings outside the USA')
    export.add_argument('--include-modified', action='store_true', help="keep titles that say 'modified'")
    export.add_argument('--include-no-vin', action='store_true', help='keep listings without a 17-character vin')

    sub.add_parser('link', help='rebuild the participants table and regroup auctions into vehicles (no network)')
    reparse = sub.add_parser('reparse', help='run the current parser over stored pages again (no network)')
    add_scope_args(reparse, 'reparse')
    sub.add_parser('reset-errors',
                   help='clear failure counts so listings and history links that hit --max-attempts are tried again (no network)')

    report = sub.add_parser('report', help='summarize selling, bidding and winning activity')
    report.add_argument('--top', type=positive_int, default=15, help='rows per table')
    report.add_argument('--model', help='make or model slug, e.g. bmw or bmw/e46-m3 (leaderboards, --pairs, --resales)')
    report.add_argument('--since', type=date_arg,
                        help='only auctions ending on or after YYYY-MM-DD (leaderboards, --pairs, --resales)')
    mode = report.add_mutually_exclusive_group()
    mode.add_argument('--member', help='member slug for a single-member profile')
    mode.add_argument('--vehicle', help='vin, chassis number, listing url or listing id: every bat auction of that car')
    mode.add_argument('--resales', action='store_true', help='members who resell cars they won, with hold time and price change')
    mode.add_argument('--pairs', action='store_true', help='show repeat seller/bidder relationships')
    report.add_argument('--min-auctions', type=positive_int,
                        help='minimum shared auctions for --pairs (default 3) or resales for --resales (default 2)')

    args = parser.parse_args(argv)
    if args.command == 'model' and bool(args.slug) == bool(args.json):
        parser.error("model takes a slug or --json FILE, one of the two")
    if args.command == 'report':
        # say so rather than quietly ignore a filter
        if (args.member or args.vehicle) and (args.model or args.since):
            parser.error("--model and --since don't apply to --member or --vehicle")
        if args.min_auctions and not (args.pairs or args.resales):
            parser.error("--min-auctions only applies to --pairs and --resales")
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
            print(f"{db.rebuild_participants():,} bidder/auction rows rebuilt from the bids")
            print_vehicle_stats(db.rebuild_vehicles())
            return 0

        if args.command == 'reset-errors':
            reset_errors(db)
            return 0

        if args.command == 'export':
            return run_export(db, args, config)

        if args.command == 'report':
            try:
                args.model_def = resolve_model(db, args.model) if args.model else None
            except ValueError as e:
                print(e)
                return 1
            if args.vehicle:
                report_vehicle(db, args)
            elif args.member:
                report_member(db, args)
            elif args.resales:
                report_resales(db, args)
            elif args.pairs:
                report_pairs(db, args)
            elif args.model:
                report_model(db, args)
            else:
                # the overview describes the whole database, so a filtered report leaves it out
                if not (args.model or args.since):
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

        raw_store = RawStore(raw_path) if raw_path and args.command in ('fetch', 'sync', 'model') else None
        models, created = None, set()
        if args.command == 'model':
            try:
                models, created = model_definitions(args, db, parser)
            except ModelRefError as e:
                print(e)
                return 1
        pipeline = build_pipeline(args, db, config, raw_store)
        status = 0
        try:
            if args.command == 'model':
                status = run_model(args, pipeline, models, created)
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
