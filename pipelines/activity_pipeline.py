import time
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from sites.bringatrailer.http_client import (
    BaTClient, HTTPStatusError, RateLimited, RobotsUnavailable, SiteUnavailable, challenge_marker
)
from sites.bringatrailer.activity_parser import (
    ActivityParser, ListingParseError, NotFinal, PARSER_VERSION, parse_results_page
)
from storage.activity_db import ActivityDB
from storage.raw_store import RawStore


# a block or an outage shows up as a run of site-level failures, a markup change as a run of pages
# that won't parse; either way, stop instead of burning through the queue
MAX_SITE_FAILURES_IN_A_ROW = 5
MAX_FAILURES_IN_A_ROW = 20
# the results feed only lists finished auctions, so finished ones without the ended marker mean
# the marker was renamed, and the finality check would otherwise skip every page from here on
MAX_UNMARKED_ENDED_IN_A_ROW = 5
# incremental discovery only counts a known page toward its stop streak once the page is this far below the
# newest auction a completed run had seen: pages above that may have been stored by a run that was cut short
WATERMARK_MARGIN = 6 * 3600


class CircuitOpen(Exception):
    """a fetch run stopped early; kind is 'site' (a block or outage), 'failures' (pages failing one after
    another) or 'unmarked' (finished auctions whose pages don't say so)"""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


class ListingMismatch(Exception):
    """a queued url served a different listing"""


def fmt_ts(ts: Optional[int]) -> str:
    if not ts:
        return '?'
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d')


class ActivityPipeline:

    def __init__(
        self,
        client: BaTClient,
        parser: ActivityParser,
        db: ActivityDB,
        activity_config: dict,
        max_site_failures: int = MAX_SITE_FAILURES_IN_A_ROW,
        max_failures: int = MAX_FAILURES_IN_A_ROW,
        max_unmarked_ended: int = MAX_UNMARKED_ENDED_IN_A_ROW,
        raw_store: Optional[RawStore] = None
    ):
        self.client = client
        self.parser = parser
        self.db = db
        self.raw_store = raw_store
        self.endpoint = activity_config['results_endpoint']
        self.per_page = activity_config['results_per_page']
        self.sort = activity_config['results_sort']
        self.max_site_failures = max_site_failures
        self.max_failures = max_failures
        self.max_unmarked_ended = max_unmarked_ended

    def discover(
        self,
        start_page: Optional[int] = None,
        max_pages: Optional[int] = None,
        since_ts: Optional[int] = None,
        stop_after_known: int = 2,
        backfill: bool = False
    ) -> dict:
        cursor = int(self.db.get_meta('backfill_next_page') or 1)
        if start_page is None:
            start_page = cursor if backfill else 1
        elif backfill and start_page > cursor:
            print(f"  --start-page {start_page} is past the backfill cursor ({cursor}), so the cursor stays put")
        watermark = self.db.get_meta('discover_watermark')
        watermark = int(watermark) if watermark else None

        page = start_page
        pages_done = 0
        known_streak = 0
        new_total = 0
        seen_ids = []
        # the newest auction on page 1, and whether this run walked far enough to vouch for everything below it
        top = None
        complete = False

        while max_pages is None or pages_done < max_pages:
            try:
                data = self.client.get_json(self.endpoint, params={
                    'page': page,
                    'per_page': self.per_page,
                    'get_items': 1,
                    'get_stats': 0,
                    'sort': self.sort
                })
            except HTTPStatusError as e:
                # asking for a page past the last one can get a 400 instead of an empty list
                if e.status == 400 and page > 1:
                    print(f"  page {page}: http 400, taken as past the last page")
                    complete = True
                    break
                raise

            summaries = parse_results_page(data)
            if not summaries:
                print("  no more results")
                complete = True
                break

            skipped = []
            new = self.db.upsert_summaries(summaries, int(time.time()), skipped)
            unreadable = len(data.get('items', [])) - len(summaries)
            if unreadable:
                print(f"    {unreadable} item(s) on page {page} had no usable id or url")
            for listing_id, error in skipped:
                print(f"    couldn't store listing {listing_id}: {error}")
            new_total += new
            seen_ids += [s.listing_id for s in summaries]
            pages_done += 1
            if data.get('items_total'):
                self.db.set_meta('feed_items_total', str(data['items_total']))

            end_times = [s.end_ts for s in summaries if s.end_ts]
            oldest = min(end_times) if end_times else None
            if page == 1 and end_times:
                top = max(end_times)
                # the backfill covers everything below its first page; incremental runs cover what ends after it
                if backfill and watermark is None:
                    self.db.set_meta('discover_watermark', str(top))
            pages_total = data.get('pages_total')
            print(f"  page {page}/{pages_total or '?'}: {len(summaries)} auctions, {new} new (back to {fmt_ts(oldest)})")

            # only a page that continues where the saved cursor stopped moves it, so a jump ahead skips nothing
            if backfill and page == cursor:
                cursor = page + 1
                self.db.set_meta('backfill_next_page', str(cursor))

            below_watermark = watermark is None or (oldest is not None and oldest < watermark - WATERMARK_MARGIN)
            if since_ts and oldest and oldest < since_ts:
                print(f"  reached --since {fmt_ts(since_ts)}")
                complete = below_watermark
                break

            if not backfill:
                known_streak = known_streak + 1 if new == 0 and below_watermark else 0
                if known_streak >= stop_after_known:
                    print(f"  {known_streak} page(s) with nothing new, caught up")
                    complete = True
                    break

            # a missing pages_total means walking on until a page comes back empty
            if pages_total and page >= pages_total:
                print("  reached last page")
                complete = True
                break
            page += 1

        # an interrupted run, or one cut short by --max-pages, leaves the watermark where it was
        if not backfill and start_page == 1 and complete and top:
            self.db.set_meta('discover_watermark', str(top))

        return {'pages': pages_done, 'seen': len(seen_ids), 'new': new_total, 'seen_ids': seen_ids, 'complete': complete}

    def fetch(
        self,
        limit: Optional[int] = None,
        since_ts: Optional[int] = None,
        max_attempts: int = 3,
        upgrade: bool = False,
        follow_history: bool = True,
        urls: Optional[List[str]] = None,
        listing_ids: Optional[List[int]] = None,
        due_refetches: bool = False
    ) -> dict:
        """listing_ids limits the queue to those listings (what a sync just discovered, or a scoped re-fetch), and
        due_refetches adds already-fetched listings whose re-fetch is due. links found on fetched pages are always
        followed, straight after the page that listed them, and count against limit like any other fetch"""
        upgrade_below = PARSER_VERSION if upgrade else None
        if urls:
            queue = [{'listing_id': None, 'url': url, 'end_ts': None, 'source': 'url'} for url in urls]
            waiting = None
        else:
            # links left over from earlier runs go first, so a --limit budget finishes the chains it started
            history = self.db.pending_history(max_attempts) if follow_history and listing_ids is None else []
            # a sync's queue is bounded by what its discovery saw, so it's read whole and counted exactly;
            # the full backlog is only counted
            whole = limit is None or due_refetches
            rows = list(self.db.pending(None if whole else limit, since_ts, max_attempts, upgrade_below, listing_ids))
            if due_refetches:
                rows += self.db.pending(None, since_ts, max_attempts, fetched_only=True)
            queue = [dict(r, source='history') for r in history] + [dict(r, source='queue') for r in rows]
            waiting = None if whole else len(history) + self.db.pending_count(since_ts, max_attempts, upgrade_below,
                                                                                listing_ids)

        queued = set()
        unique = []
        for row in queue:
            if row['url'] not in queued:
                queued.add(row['url'])
                unique.append(row)
        queue = unique
        if waiting is None:
            waiting = len(queue)
        budget = f", this run fetches up to {limit}" if limit is not None and limit < waiting else ''
        print(f"  {waiting} auction(s) waiting for bid history{budget}")

        fetched = 0
        failed = 0
        not_final = 0
        bids = 0
        followed = 0
        i = 0
        site_failures = 0
        failures = 0
        unmarked_ended = 0

        try:
            while i < len(queue) and (limit is None or i < limit):
                row = queue[i]
                i += 1
                now = time.time()

                try:
                    page, detail, fragments = self._fetch_listing(row['url'], now)

                    # never file one listing's page under another listing's id
                    if row['listing_id'] and detail.listing_id != row['listing_id']:
                        raise ListingMismatch(f"url serves listing {detail.listing_id}, not {row['listing_id']}")

                    for note in detail.notes:
                        print(f"    {row['url']}: {note}")
                    mismatch = None
                    if detail.bids_reported is not None and detail.bids_reported != len(detail.bids):
                        mismatch = f"bid count mismatch: page says {detail.bids_reported}, parsed {len(detail.bids)}"
                        print(f"    {mismatch} at {row['url']}")

                    self._store_raw(page, detail, fragments, now)
                    self.db.save_detail(detail, int(now), PARSER_VERSION, requested_url=row['url'], note=mismatch)
                    fetched += 1
                    bids += len(detail.bids)
                    if row['source'] == 'history':
                        followed += 1
                    site_failures = failures = unmarked_ended = 0

                    # earlier (or later) auctions of the same car, so its ownership chain is complete
                    if follow_history:
                        links = [link for link in detail.history
                                 if link.url not in queued and self.db.needs_fetch(link.url, max_attempts)]
                        for offset, link in enumerate(links):
                            queue.insert(i + offset, {'listing_id': None, 'url': link.url, 'end_ts': link.end_ts,
                                                      'source': 'history'})
                            queued.add(link.url)

                # the server asked us to stop for a while, or robots.txt can't be read or now says no
                except (RateLimited, RobotsUnavailable):
                    raise

                # a block or outage isn't this listing's fault, so it doesn't use up one of its attempts
                except SiteUnavailable as e:
                    failed += 1
                    site_failures += 1
                    failures += 1
                    print(f"    site unavailable at {row['url']}: {e}")
                    if site_failures >= self.max_site_failures:
                        raise CircuitOpen(f"{site_failures} site-level failures in a row, last: {e}", 'site') from e
                    if failures >= self.max_failures:
                        raise CircuitOpen(f"{failures} failures in a row, last: {e}", 'failures') from e

                # a live auction: nothing saved, no attempt used, its history links not followed yet
                except NotFinal as e:
                    not_final += 1
                    print(f"    not over yet, nothing saved: {e}")
                    ended_ts = row.get('end_ts') or e.end_ts
                    if e.marker_missing and ended_ts and ended_ts < now:
                        unmarked_ended += 1
                        if unmarked_ended >= self.max_unmarked_ended:
                            raise CircuitOpen(
                                f"{unmarked_ended} auctions in a row that ended have no ended marker "
                                f"({self.parser.selectors['ended_marker']}), last: {row['url']}", 'unmarked'
                            ) from e

                # the page came back but doesn't read like a finished listing: likely a markup change,
                # so it's recorded without using up an attempt
                except ListingParseError as e:
                    failed += 1
                    failures += 1
                    self._record_failure(row, e, count_attempt=False)
                    print(f"    failed {row['url']}: {e}")
                    if failures >= self.max_failures:
                        raise CircuitOpen(f"{failures} failures in a row, last: {e}", 'failures') from e

                # keep a multi-day crawl alive through one bad page; ctrl-c still stops it
                except Exception as e:
                    failed += 1
                    failures += 1
                    self._record_failure(row, e, count_attempt=True)
                    print(f"    failed {row['url']}: {e}")
                    if failures >= self.max_failures:
                        raise CircuitOpen(f"{failures} failures in a row, last: {e}", 'failures') from e

                if i % 25 == 0 or i == len(queue):
                    print(f"  fetched {i}/{len(queue)} ({bids} bids so far, {followed} history links followed, "
                          f"{failed} failed, {not_final} not over yet)")
        finally:
            vehicles = self.db.rebuild_vehicles()

        return {'fetched': fetched, 'failed': failed, 'not_final': not_final, 'bids': bids, 'followed': followed,
                'vehicles': vehicles}

    def reparse(self, listing_ids: Optional[List[int]] = None) -> dict:
        """run the current parser over stored pages, offline; each listing keeps the time it was fetched"""
        reparsed = 0
        failed = 0
        try:
            for raw in self.raw_store.pages(listing_ids):
                try:
                    detail = self.parser.parse_listing(raw.html, raw.url, now=raw.fetched_at)
                    if detail.listing_id != raw.listing_id:
                        raise ListingMismatch(f"stored page is listing {detail.listing_id}")
                    self.db.save_detail(detail, raw.fetched_at, PARSER_VERSION)
                    reparsed += 1
                except Exception as e:
                    failed += 1
                    print(f"    listing {raw.listing_id} ({raw.kind}): {e}")
                if (reparsed + failed) % 1000 == 0:
                    print(f"  reparsed {reparsed + failed} ({failed} failed)")
        finally:
            vehicles = self.db.rebuild_vehicles()
        return {'reparsed': reparsed, 'failed': failed, 'vehicles': vehicles}

    def _fetch_listing(self, url: str, now: float):
        page = self.client.get_page(url)
        try:
            # parsed against the url that answered, after any redirects
            if self.raw_store:
                detail, fragments = self.parser.parse_with_fragments(page.text, page.url, now=now)
            else:
                detail, fragments = self.parser.parse_listing(page.text, page.url, now=now), None
            return page, detail, fragments
        except ListingParseError:
            # a challenge or block page comes back as a 200 that just doesn't parse
            marker = challenge_marker(page.text)
            if marker:
                raise SiteUnavailable(f"{url} served a challenge page ({marker!r})")
            raise

    def _store_raw(self, page, detail, fragments: Optional[str], now: float):
        """keep what the parser read, so a later parser fix can be applied without downloading the page again.
        the fragments are kept only if parsing them gives exactly this result; otherwise the whole page is"""
        if not self.raw_store:
            return
        try:
            same = self.parser.parse_listing(fragments, page.url, now=now) == detail
        except Exception:
            same = False
        kind, html = ('fragments', fragments) if same else ('page', page.text)
        self.raw_store.put(detail.listing_id, page.url, int(now), PARSER_VERSION, kind, html)

    def _record_failure(self, row: dict, error: Exception, count_attempt: bool):
        # a busy or broken database here mustn't end the run; the listing just stays queued as it was
        try:
            if row['listing_id']:
                self.db.mark_error(row['listing_id'], str(error), count_attempt)
            else:
                self.db.mark_url_error(row['url'], str(error), count_attempt)
        except sqlite3.Error as e:
            print(f"    couldn't record that failure ({e})")
