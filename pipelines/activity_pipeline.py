import time
from datetime import datetime, timezone
from typing import List, Optional

from sites.bringatrailer.http_client import BaTClient, RateLimited, SiteUnavailable, challenge_marker
from sites.bringatrailer.activity_parser import ActivityParser, ListingParseError, PARSER_VERSION, parse_results_page
from storage.activity_db import ActivityDB


# a block or an outage shows up as a run of site-level failures, a markup change as a run of pages
# that won't parse; either way, stop instead of burning through the queue
MAX_SITE_FAILURES_IN_A_ROW = 5
MAX_FAILURES_IN_A_ROW = 20


class CircuitOpen(Exception):
    """a fetch run stopped early because too many listings failed in a row"""

    def __init__(self, message: str, site_level: bool):
        super().__init__(message)
        # site-level failures don't use up attempts, so rerunning later picks up where this stopped
        self.site_level = site_level


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
        max_failures: int = MAX_FAILURES_IN_A_ROW
    ):
        self.client = client
        self.parser = parser
        self.db = db
        self.endpoint = activity_config['results_endpoint']
        self.per_page = activity_config['results_per_page']
        self.sort = activity_config['results_sort']
        self.max_site_failures = max_site_failures
        self.max_failures = max_failures

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
            queue = [{'listing_id': None, 'url': url} for url in urls]
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
        bids = 0
        followed = 0
        i = 0
        site_failures = 0
        failures = 0

        try:
            while i < len(queue) and (not limit or i < limit):
                row = queue[i]
                i += 1

                try:
                    detail = self._fetch_listing(row['url'])

                    if row['listing_id'] and detail.listing_id != row['listing_id']:
                        print(f"    listing id mismatch for {row['url']}: page says {detail.listing_id}")
                        detail.listing_id = row['listing_id']

                    if detail.bids_reported is not None and detail.bids_reported != len(detail.bids):
                        print(f"    bid count mismatch for {row['url']}: page says {detail.bids_reported}, parsed {len(detail.bids)}")

                    self.db.save_detail(detail, int(time.time()), PARSER_VERSION)
                    fetched += 1
                    bids += len(detail.bids)
                    site_failures = failures = 0

                    # earlier (or later) auctions of the same car, so its ownership chain is complete
                    if follow_history:
                        for link in detail.history:
                            if link.url not in queued and self.db.needs_fetch(link.url):
                                queue.append({'listing_id': None, 'url': link.url})
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
                        raise CircuitOpen(f"{site_failures} site-level failures in a row, last: {e}", site_level=True) from e
                    if failures >= self.max_failures:
                        raise CircuitOpen(f"{failures} failures in a row, last: {e}", site_level=False) from e

                # keep a multi-day crawl alive through one bad page; ctrl-c still stops it
                except Exception as e:
                    failed += 1
                    failures += 1
                    if row['listing_id']:
                        self.db.mark_error(row['listing_id'], str(e))
                    else:
                        self.db.mark_url_error(row['url'], str(e))
                    print(f"    failed {row['url']}: {e}")
                    if failures >= self.max_failures:
                        raise CircuitOpen(f"{failures} failures in a row, last: {e}", site_level=False) from e

                if i % 25 == 0 or i == len(queue):
                    print(f"  fetched {i}/{len(queue)} ({bids} bids so far, {followed} history links followed, {failed} failed)")
        finally:
            vehicles = self.db.rebuild_vehicles()

        return {'fetched': fetched, 'failed': failed, 'bids': bids, 'followed': followed, 'vehicles': vehicles}

    def _fetch_listing(self, url: str):
        page = self.client.get_page(url)
        try:
            return self.parser.parse_listing(page.text, url)
        except ListingParseError:
            # a challenge or block page comes back as a 200 that just doesn't parse
            marker = challenge_marker(page.text)
            if marker:
                raise SiteUnavailable(f"{url} served a challenge page ({marker!r})")
            raise
