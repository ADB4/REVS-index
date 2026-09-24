import time
from datetime import datetime, timezone
from typing import List, Optional

from sites.bringatrailer.http_client import BaTClient, RateLimited, SiteUnavailable, challenge_marker
from sites.bringatrailer.activity_parser import (
    ActivityParser, ListingParseError, NotFinal, PARSER_VERSION, parse_results_page
)
from storage.activity_db import ActivityDB


# a block or an outage shows up as a run of site-level failures, a markup change as a run of pages
# that won't parse; either way, stop instead of burning through the queue
MAX_SITE_FAILURES_IN_A_ROW = 5
MAX_FAILURES_IN_A_ROW = 20
# the results feed only lists finished auctions, so finished ones without the ended marker mean
# the marker was renamed, and the finality check would otherwise skip every page from here on
MAX_UNMARKED_ENDED_IN_A_ROW = 5


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
        max_unmarked_ended: int = MAX_UNMARKED_ENDED_IN_A_ROW
    ):
        self.client = client
        self.parser = parser
        self.db = db
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
        if start_page is None:
            start_page = int(self.db.get_meta('backfill_next_page') or 1) if backfill else 1

        page = start_page
        pages_done = 0
        known_streak = 0
        new_total = 0
        seen_total = 0

        while not max_pages or pages_done < max_pages:
            data = self.client.get_json(self.endpoint, params={
                'page': page,
                'per_page': self.per_page,
                'get_items': 1,
                'get_stats': 0,
                'sort': self.sort
            })
            summaries = parse_results_page(data)
            if not summaries:
                print("  no more results")
                break

            new = self.db.upsert_summaries(summaries, int(time.time()))
            new_total += new
            seen_total += len(summaries)
            pages_done += 1

            end_times = [s.end_ts for s in summaries if s.end_ts]
            oldest = min(end_times) if end_times else None
            pages_total = data.get('pages_total') or page
            print(f"  page {page}/{pages_total}: {len(summaries)} auctions, {new} new (back to {fmt_ts(oldest)})")

            if backfill:
                self.db.set_meta('backfill_next_page', str(page + 1))

            if since_ts and oldest and oldest < since_ts:
                print(f"  reached --since {fmt_ts(since_ts)}")
                break

            if not backfill:
                known_streak = known_streak + 1 if new == 0 else 0
                if known_streak >= stop_after_known:
                    print(f"  {known_streak} page(s) with nothing new, caught up")
                    break

            if page >= pages_total:
                print("  reached last page")
                break
            page += 1

        return {'pages': pages_done, 'seen': seen_total, 'new': new_total}

    def fetch(
        self,
        limit: Optional[int] = None,
        since_ts: Optional[int] = None,
        max_attempts: int = 3,
        upgrade: bool = False,
        follow_history: bool = True,
        urls: Optional[List[str]] = None
    ) -> dict:
        if urls:
            queue = [{'listing_id': None, 'url': url, 'end_ts': None} for url in urls]
        else:
            rows = self.db.pending(limit, since_ts, max_attempts, upgrade_below=PARSER_VERSION if upgrade else None)
            queue = [dict(r) for r in rows]
            if follow_history:
                queue += [dict(r) for r in self.db.pending_history(max_attempts)]

        queued = set()
        unique = []
        for row in queue:
            if row['url'] not in queued:
                queued.add(row['url'])
                unique.append(row)
        queue = unique
        print(f"  {len(queue)} auction(s) waiting for bid history")

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
            while i < len(queue) and (not limit or i < limit):
                row = queue[i]
                i += 1
                now = time.time()

                try:
                    detail = self._fetch_listing(row['url'], now)

                    # never file one listing's page under another listing's id
                    if row['listing_id'] and detail.listing_id != row['listing_id']:
                        raise ListingMismatch(f"url serves listing {detail.listing_id}, not {row['listing_id']}")

                    for note in detail.notes:
                        print(f"    {row['url']}: {note}")
                    if detail.bids_reported is not None and detail.bids_reported != len(detail.bids):
                        print(f"    bid count mismatch for {row['url']}: page says {detail.bids_reported}, parsed {len(detail.bids)}")

                    self.db.save_detail(detail, int(now), PARSER_VERSION, requested_url=row['url'])
                    fetched += 1
                    bids += len(detail.bids)
                    site_failures = failures = unmarked_ended = 0

                    # earlier (or later) auctions of the same car, so its ownership chain is complete
                    if follow_history:
                        for link in detail.history:
                            if link.url not in queued and self.db.needs_fetch(link.url):
                                queue.append({'listing_id': None, 'url': link.url, 'end_ts': link.end_ts})
                                queued.add(link.url)
                                followed += 1

                except RateLimited:
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

    def _fetch_listing(self, url: str, now: float):
        page = self.client.get_page(url)
        try:
            # parsed against the url that answered, after any redirects
            return self.parser.parse_listing(page.text, page.url, now=now)
        except ListingParseError:
            # a challenge or block page comes back as a 200 that just doesn't parse
            marker = challenge_marker(page.text)
            if marker:
                raise SiteUnavailable(f"{url} served a challenge page ({marker!r})")
            raise

    def _record_failure(self, row: dict, error: Exception, count_attempt: bool):
        if row['listing_id']:
            self.db.mark_error(row['listing_id'], str(error), count_attempt)
        else:
            self.db.mark_url_error(row['url'], str(error), count_attempt)
