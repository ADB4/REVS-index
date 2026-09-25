import json
import time
import hashlib
from typing import Dict, List, Optional

from core.models.activity import AuctionSummary
from core.models.model_definition import ModelDefinition, normalize_slug
from pipelines.activity_pipeline import ActivityPipeline, fmt_ts
from sites.bringatrailer.http_client import DisallowedPathError, HTTPStatusError, SiteUnavailable, challenge_marker
from sites.bringatrailer.model_page import ModelPageError, parse_model_page


# a model page's filter is read again after this long, so sub-models bat adds to it are picked up
FILTER_MAX_AGE = 7 * 86400
# no model page lists this many auctions; a feed that does wasn't narrowed by the filter (the site-wide one is ~265k)
MAX_MODEL_FEED = 100_000


class ModelSetupError(Exception):
    """a model that can't be discovered as asked; raised before any feed is walked"""


def model_page_url(base_url: str, slug: str) -> str:
    """the old scraper's '/<slug>/', which also takes the tag spelling '/<make>/<model>/'"""
    return f"{base_url.rstrip('/')}/{normalize_slug(slug)}/"


def params_digest(params: dict) -> str:
    return hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:10]


def feed_scope(model: ModelDefinition, slug: str, params: dict) -> str:
    """meta key prefix for one model page's feed. a changed filter is a different feed, so it starts afresh
    rather than trusting a watermark and cursor that were kept for another one"""
    return f"model:{model.key}:{normalize_slug(slug)}:{params_digest(params)}:"


def page_scope(model: ModelDefinition, slug: str) -> str:
    """meta key prefix for what's known about a model page itself: when it was read, how many auctions it listed"""
    return f"model:{model.key}:{normalize_slug(slug)}:page:"


class ModelPipeline:
    """what `activity.py model` does before fetching: finds a model's auctions and records them in model_listings.
    source A pages the results feed with the filter the model page embeds, so the site says what belongs; source B,
    for a model whose page can't be read, walks the site-wide feed back to --since and guesses from titles, which
    fetching the pages then confirms or rules out"""

    def __init__(self, pipeline: ActivityPipeline, base_url: str):
        self.pipeline = pipeline
        self.db = pipeline.db
        self.client = pipeline.client
        self.base_url = base_url

    def discover(self, model: ModelDefinition, since_ts: Optional[int] = None, max_pages: Optional[int] = None,
                 refresh_filter: bool = False) -> dict:
        feeds = self.feeds(model, refresh_filter)
        if feeds:
            return self._discover_pages(model, feeds, since_ts, max_pages)
        return self._discover_titles(model, since_ts, max_pages)

    def learn_tags(self, model: ModelDefinition, slug: str, tags: List[str]) -> bool:
        """the tag spellings a model page gave (its own, its sub-models'), which listings of the model carry"""
        added = [t for t in tags if t and t not in model.tag_slugs and t != normalize_slug(slug)]
        model.tag_slugs += added
        return bool(added)

    def feeds(self, model: ModelDefinition, refresh: bool = False) -> Dict[str, dict]:
        """each model page's feed filter, kept on the model's row and read again once it's a week old. a page
        that can't be read now keeps the filter it had"""
        changed = False
        now = int(time.time())
        for slug in model.slugs:
            read_at = int(self.db.get_meta(page_scope(model, slug) + 'read_at') or 0)
            # a filter given with --filter-url stands until --refresh-filter
            manual = self.db.get_meta(page_scope(model, slug) + 'manual') == '1'
            if slug in model.filters and not refresh and (manual or now - read_at < FILTER_MAX_AGE):
                continue
            page, learned = self._read_model_page(model, slug)
            changed = changed or learned
            if page is None:
                continue
            if model.filters.get(slug) not in (None, page.params):
                print(f"  the page's filter changed, so its feed is walked afresh")
            model.filters[slug] = page.params
            # listings' model tags link to the page itself or to one of its sub-models
            self.learn_tags(model, slug, [page.slug] + page.sub_slugs)
            self.db.set_meta(page_scope(model, slug) + 'read_at', str(now))
            self.db.delete_meta(page_scope(model, slug) + 'manual')
            if page.items_total is not None:
                self.db.set_meta(page_scope(model, slug) + 'items_total', str(page.items_total))
            print(f"  {page.url}: {page.items_total if page.items_total is not None else '?'} completed auctions, "
                  f"filter {json.dumps(page.params)}")
            changed = True
        if changed:
            self.db.save_model(model)
        return {slug: model.filters[slug] for slug in model.slugs if model.filters.get(slug)}

    def _read_model_page(self, model: ModelDefinition, slug: str):
        """(the parsed page or None, whether the model learned a tag spelling from a page without a filter)"""
        url = model_page_url(self.base_url, slug)
        try:
            page = self.client.get_page(url)
        except DisallowedPathError as e:
            print(f"  not reading the model page: {e}")
            return None, False
        except HTTPStatusError as e:
            print(f"  no model page at {url} ({e}); check the slug against the page's address in a browser")
            return None, False
        try:
            return parse_model_page(page.text, page.url), False
        except ModelPageError as e:
            # a challenge or block page isn't a redesign: stop, rather than fall back to walking the whole site
            marker = challenge_marker(page.text)
            if marker:
                raise SiteUnavailable(f"{url} served a challenge page ({marker!r})") from e
            print(f"  {e}")
            return None, self.learn_tags(model, slug, e.slugs)

    def _feed_check(self, model: ModelDefinition, slug: str):
        """a check for each page of a model's feed: one that lists far more auctions than the model page said, or
        about as many as the whole site, wasn't narrowed by the filter, and nothing in it may be recorded as the
        model's"""
        expected = self.db.get_meta(page_scope(model, slug) + 'items_total')
        site_total = self.db.get_meta('feed_items_total')

        def check(data: dict):
            total = data.get('items_total')
            if not str(total).isdigit():
                return
            total = int(total)
            said = f"the model page said {int(expected):,}" if expected else "no model page lists that many"
            if (total > MAX_MODEL_FEED or (expected and total > 2 * int(expected) + 100)
                    or (site_total and total >= 0.9 * int(site_total))):
                raise ModelSetupError(
                    f"{model.key}'s feed lists {total:,} auctions, but {said}: the filter isn't narrowing the results "
                    f"feed, so nothing from it was recorded. check the filter the model page sends (--filter-url)")
        return check

    def _discover_pages(self, model: ModelDefinition, feeds: Dict[str, dict], since_ts: Optional[int],
                        max_pages: Optional[int]) -> dict:
        """source A: each model page's own feed; everything it lists is the model's, if its year is in range"""
        def record(summaries: List[AuctionSummary]):
            rows = [(s.listing_id, 'member' if model.year_ok(s.year) else 'out_of_years') for s in summaries]
            self.db.record_model_listings(model.key, rows, 'feed')

        totals = {'source': 'model page', 'pages': 0, 'seen': 0, 'new': 0, 'complete': True}
        missing = [slug for slug in model.slugs if slug not in feeds]
        if missing:
            print(f"  no feed for {', '.join(missing)}: only auctions tagged as the model are found for it")
            totals['complete'] = False

        # two slugs for one page share a feed; a page budget goes to the feed walked least recently first
        unique = {}
        for slug, params in feeds.items():
            unique.setdefault(params_digest(params), slug)
        order = sorted(unique.values(), key=lambda slug: int(
            self.db.get_meta(feed_scope(model, slug, feeds[slug]) + 'walked_at') or 0))

        remaining = max_pages
        whole_feeds = []
        for slug in order:
            if remaining is not None and remaining <= 0:
                totals['complete'] = False
                break
            params = feeds[slug]
            print(f"  {model_page_url(self.base_url, slug)} feed")
            scope = feed_scope(model, slug, params)
            stats = self.pipeline.discover_feed(since_ts=since_ts, max_pages=remaining, params=params, scope=scope,
                                                on_page=record, check=self._feed_check(model, slug))
            self.db.set_meta(scope + 'walked_at', str(int(time.time())))
            if stats['complete'] and self.db.get_meta(scope + 'backfill_reached') == '0':
                whole_feeds.append(int(self.db.get_meta(scope + 'feed_items_total') or 0))
            for key in ('pages', 'seen', 'new'):
                totals[key] += stats[key]
            totals['complete'] = totals['complete'] and stats['complete']
            if remaining is not None:
                remaining -= stats['pages']

        # a feed that pages differently than asked (a smaller per_page cap, say) could end early without saying so
        if whole_feeds and len(whole_feeds) == len(order):
            stored = self.db.query("SELECT COUNT(*) AS n FROM model_listings WHERE model_key = ? AND source = 'feed'",
                                   (model.key,))[0]['n']
            if stored < max(whole_feeds):
                print(f"  note: the model's feed says it lists {max(whole_feeds):,} auctions, but only {stored:,} "
                      f"came back from paging through it")
        return totals

    def fallback_problem(self, model: ModelDefinition, since_ts: Optional[int]) -> Optional[str]:
        """why source B can't run for this model, or None"""
        if not (model.make and model.model_short):
            return (f"{model.key} has no readable model page and no make and model_short to match titles with; "
                    f"give --make and --model-short, or a --json entry")
        if not since_ts:
            total = self.db.get_meta('feed_items_total')
            per_page = self.pipeline.per_page
            size = (f"about {-(-int(total) // per_page):,} pages at {per_page} per page" if total
                    else "thousands of pages")
            return (f"{model.key} has no readable model page, so its auctions would come from the site-wide results "
                    f"feed, which without --since means walking all of it ({size}). give --since YYYY-MM-DD")
        return None

    def _discover_titles(self, model: ModelDefinition, since_ts: Optional[int], max_pages: Optional[int]) -> dict:
        """source B: the site-wide feed back to since_ts, then every stored auction since then whose title looks
        like the model. fetching them settles it: classify_model_candidates keeps those tagged as the model"""
        problem = self.fallback_problem(model, since_ts)
        if problem:
            raise ModelSetupError(problem)
        print(f"  site-wide feed back to {fmt_ts(since_ts)}, matching titles on {model.make!r} and "
              f"{model.model_short.strip()!r}")
        stats = self.pipeline.discover_feed(since_ts=since_ts, max_pages=max_pages)
        candidates = self.db.title_candidates(model, since_ts)
        added = self.db.record_model_listings(model.key, [(i, 'unchecked') for i in candidates], 'title')
        print(f"  {added:,} new title match(es) to check")
        return {'source': 'titles', 'pages': stats['pages'], 'seen': stats['seen'], 'new': stats['new'],
                'complete': stats['complete'], 'candidates': added}
