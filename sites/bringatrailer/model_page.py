import re
import json
from dataclasses import dataclass, field
from html import unescape
from typing import List, Optional
from urllib.parse import parse_qsl, urlparse

from core.models.model_definition import normalize_slug
from sites.bringatrailer.activity_parser import extract_js_object


# the completed auctions a model page lists, and the base filter its "show more" sends to the results feed
INITIAL_DATA_VAR = 'auctionsCompletedInitialData'
CANONICAL_RE = re.compile(r'<link[^>]+rel=["\']canonical["\'][^>]*>', re.I)
# a model page's sub-models (the keyword pages its filter names), as json in an attribute
MODEL_LIST_RE = re.compile(r'<section[^>]*class=["\'][^"\']*\bmodel-list\b[^>]*data-items=(["\'])(.*?)\1', re.I | re.S)
HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
# a listings-filter request's own paging, order and nonces: not part of what narrows it to a model
FEED_CONTROL_PARAMS = {'page', 'per_page', 'get_items', 'get_stats', 'sort', '_', '_wpnonce'}


class ModelPageError(Exception):
    """a model page without the feed filter this crawler reads, most likely a markup change. slugs are the tag
    spellings the page still gave (its own and its sub-models'), which title matching can use"""

    def __init__(self, message: str, slugs: Optional[List[str]] = None):
        super().__init__(message)
        self.slugs = slugs or []


@dataclass
class ModelPage:
    url: str
    # the page's own spelling, which listings' model tags link to: 'chevrolet/c8'
    slug: str
    # the listings-filter parameters that narrow the results feed to this model
    params: dict
    items_total: Optional[int] = None
    # the sub-model pages the filter covers ('chevrolet/corvette-c8-z06'), which listings' model tags link to
    sub_slugs: List[str] = field(default_factory=list)


def filter_params(base_filter: dict, prefix: str = 'base_filter') -> dict:
    """{'keyword_pages': [1, 2]} -> {'base_filter[keyword_pages][]': [1, 2]}: nested data written the way the
    page's script (jquery's $.param) writes it, which requests sends as repeated keys"""
    params = {}
    for key, value in base_filter.items():
        name = f"{prefix}[{key}]"
        if isinstance(value, dict):
            params.update(filter_params(value, name))
        # requests leaves out an empty list, so it would narrow nothing
        elif isinstance(value, (list, tuple)) and value:
            params[f"{name}[]"] = list(value)
        elif not isinstance(value, (list, tuple)) and value not in (None, ''):
            params[name] = value
    return params


def sub_model_slugs(html: str) -> List[str]:
    match = MODEL_LIST_RE.search(html)
    if not match:
        return []
    try:
        items = json.loads(unescape(match.group(2)))
    except ValueError:
        return []
    return [normalize_slug(i['url']) for i in items if isinstance(i, dict) and i.get('url') and normalize_slug(i['url'])]


def parse_model_page(html: str, url: str) -> ModelPage:
    canonical = CANONICAL_RE.search(html)
    href = HREF_RE.search(canonical.group(0)) if canonical else None
    page_url = href.group(1) if href and urlparse(href.group(1)).netloc == urlparse(url).netloc else url
    sub_slugs = sub_model_slugs(html)

    data = extract_js_object(html, INITIAL_DATA_VAR)
    base_filter = data.get('base_filter') if isinstance(data, dict) else None
    params = filter_params(base_filter) if isinstance(base_filter, dict) else {}
    if not params:
        raise ModelPageError(f"{url} has no {INITIAL_DATA_VAR}.base_filter to narrow the results feed with",
                             [normalize_slug(page_url)] + sub_slugs if canonical else sub_slugs)

    total = data.get('items_total')
    return ModelPage(url=page_url, slug=normalize_slug(page_url), params=params,
                     items_total=int(total) if str(total).isdigit() else None, sub_slugs=sub_slugs)


def params_from_url(url: str) -> dict:
    """the filter of a listings-filter request copied from the browser's network tab: everything in its query
    but paging, order and nonces. repeated keys become lists"""
    params = {}
    for key, value in parse_qsl(urlparse(url).query, keep_blank_values=True):
        if key in FEED_CONTROL_PARAMS:
            continue
        if key.endswith('[]'):
            params.setdefault(key, []).append(value)
        else:
            params[key] = value
    if not params:
        raise ValueError(f"no filter in {url!r}: its query has only paging and sort")
    return params
