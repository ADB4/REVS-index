import re
import json
import time
from html import unescape
from typing import List, Optional, Tuple
from urllib.parse import urlparse
from bs4 import BeautifulSoup

from core.models.activity import AuctionSummary, AuctionDetail, Bid, Member, HistoryLink
from extractors.field_extractors.vin_extractor import VINExtractor
from sites.bringatrailer.listing_specs import ListingSpecs


# bump when the parser starts capturing new fields, so `fetch --upgrade` knows what to re-fetch
PARSER_VERSION = 4
# fragments stored by an older parser leave out elements this one reads (4: the excerpt), so reparsing them can
# only vouch for the version that stored them; raise this when a new field reads an element fragments didn't keep
FRAGMENTS_COMPLETE_SINCE = 4

# selectors parse_listing applies to the whole page; the rest are only read inside what these match
PAGE_LEVEL_SELECTORS = (
    'listing_id', 'title', 'country', 'result_info', 'bid_count', 'winner_link', 'essentials', 'group_link',
    'listing_details', 'history_item', 'ended_marker', 'canonical'
)

# selectors parse_listing can't work without; a missing one is a config error, not a bad page
REQUIRED_SELECTORS = (
    'comments_var', 'listing_id', 'title', 'country', 'result_info', 'bid_count', 'winner_link', 'essentials',
    'group_link', 'group_label', 'listing_details', 'history_item', 'history_summary', 'ended_marker', 'canonical'
)


class ListingParseError(Exception):
    pass


class LayoutError(ListingParseError):
    """a finished listing page without the parts every finished listing has, most likely a markup change"""


class NotFinal(Exception):
    """the page doesn't show a finished auction yet, so nothing on it is final"""

    def __init__(self, message: str, end_ts: Optional[int] = None, marker_missing: bool = True):
        super().__init__(message)
        self.end_ts = end_ts
        self.marker_missing = marker_missing


MONEY_RE = re.compile(r'([A-Z]{3})\s*[^\d\s]*\s*([\d,]+)')
MEMBER_URL_RE = re.compile(r'/member/([^/?#]+)/?')
YEAR_RE = re.compile(r'\b(19\d{2}|20\d{2})\b')
VIN_RE = re.compile(f"^{VINExtractor.VIN_PATTERN}$")
# a vin standing on its own, not the middle of a longer run of letters and digits
VIN_TOKEN_RE = re.compile(f"(?<![A-Z0-9]){VINExtractor.VIN_PATTERN}(?![A-Z0-9])")
# where a chassis number ends and a note about it starts: "9113600571 (see text)", "123, Engine: 456"
CHASSIS_NOTE_RE = re.compile(r'\(|,|;| / |ENGINE')
REPEATED_CHAR_RE = re.compile(r'^(.)\1+$')
SELLER_TYPE_RE = re.compile(r'^Private Party or Dealer\s*:\s*(Private Party|Dealer)\b')
# the buyer named at the end of a closing event: "Sold on 9/14/15 for $13,000 to NIACC."
BUYER_RE = re.compile(r'.*\bto\s+(.+?)[\s.!]*$', re.S)


def outer_selectors(selector: str) -> List[str]:
    """the leading compound selector of each part of a group: '#listing-bid .n, a.b > c' -> ['#listing-bid', 'a.b']"""
    return [re.split(r'\s*[>+~]\s*|\s+', part.strip(), maxsplit=1)[0] for part in selector.split(',') if part.strip()]


def normalize_chassis(raw: str) -> Optional[str]:
    chassis = re.sub(r'[^A-Z0-9]', '', (raw or '').upper())
    # drop placeholders like "N/A", "Withheld" or "00000000"
    if len(chassis) < 5 or not re.search(r'\d', chassis) or REPEATED_CHAR_RE.match(chassis):
        return None
    return chassis


def parse_chassis(value: str) -> Tuple[Optional[str], Optional[str]]:
    """(chassis, vin) from the text after "Chassis:"; notes after the number are dropped, never glued on"""
    upper = (value or '').upper()
    token = VIN_TOKEN_RE.search(upper)
    if token and normalize_chassis(token.group()):
        return token.group(), token.group()
    chassis = normalize_chassis(CHASSIS_NOTE_RE.split(upper, 1)[0])
    return chassis, (chassis if chassis and VIN_RE.match(chassis) else None)


def parse_result_text(text: str) -> Tuple[str, Optional[int], Optional[str]]:
    text = re.sub(r'<[^>]+>', ' ', text or '')
    text = re.sub(r'\s+', ' ', text).strip()
    lowered = text.lower()

    if lowered.startswith('sold'):
        result = 'sold'
    elif lowered.startswith('bid to'):
        result = 'reserve_not_met'
    elif 'withdrawn' in lowered:
        result = 'withdrawn'
    else:
        result = 'unknown'

    money = MONEY_RE.search(text)
    if not money:
        return result, None, None
    return result, int(money.group(2).replace(',', '')), money.group(1)


def member_from_url(url: str, display_name: str = None, user_id: int = None) -> Optional[Member]:
    match = MEMBER_URL_RE.search(url or '')
    if not match:
        return None
    return Member(slug=match.group(1).lower(), display_name=display_name, user_id=user_id or None)


def member_from_comment(comment: dict) -> Optional[Member]:
    member = member_from_url(comment.get('authorUrl'), comment.get('authorName'), comment.get('authorId'))
    if member:
        return member

    # deleted accounts lose their profile url but keep a numeric id
    author_id = comment.get('authorId') or 0
    if author_id > 0:
        return Member(slug=f"id:{author_id}", display_name=comment.get('authorName'), user_id=author_id)
    return None


def parse_results_page(data: dict) -> List[AuctionSummary]:
    summaries = []
    for item in data.get('items', []):
        # one malformed item shouldn't cost the rest of the page
        try:
            listing_id = int(item['id'])
        except (KeyError, TypeError, ValueError):
            continue
        if not item.get('url'):
            continue

        result, amount, currency = parse_result_text(item.get('sold_text', ''))

        year = item.get('year')
        if not year:
            year_match = YEAR_RE.search(item.get('title', ''))
            year = int(year_match.group(1)) if year_match else None

        summaries.append(AuctionSummary(
            listing_id=listing_id,
            url=item['url'],
            title=unescape(item.get('title') or ''),
            result=result,
            # sold_text is bat's stated result; current_bid can be the last bid before a post-auction deal
            high_bid=amount if amount is not None else item.get('current_bid'),
            currency=item.get('currency') or currency,
            end_ts=item.get('timestamp_end') or item.get('sold_text_timestamp'),
            year=year,
            country_code=item.get('country_code_alpha3') or item.get('country_code'),
            no_reserve=bool(item.get('noreserve')),
            premium=bool(item.get('premium'))
        ))
    return summaries


class ActivityParser:

    def __init__(self, selectors: dict, specs: Optional[ListingSpecs] = None):
        """specs reads engine, transmission, mileage, colors, listing details and excerpt; without it those stay empty"""
        missing = [key for key in REQUIRED_SELECTORS if not selectors.get(key)]
        if missing:
            raise ValueError(f"activity selectors missing from config: {', '.join(missing)}")
        self.selectors = selectors
        self.specs = specs

    def parse_listing(self, html: str, url: str, now: Optional[float] = None) -> AuctionDetail:
        """everything a finished listing page says; raises NotFinal for a live page and LayoutError for one
        missing parts that every finished listing has"""
        return self._parse(html, url, now)[0]

    def parse_with_fragments(self, html: str, url: str, now: Optional[float] = None) -> Tuple[AuctionDetail, str]:
        """parse_listing, plus a much smaller document holding only what it read: the embedded comment data and
        the elements the page-level selectors match. parsing that document should give the same result"""
        detail, soup, vms = self._parse(html, url, now)
        return detail, self._fragments(soup, vms)

    def _fragments(self, soup: BeautifulSoup, vms: dict) -> str:
        selectors = [self.selectors[key] for key in PAGE_LEVEL_SELECTORS]
        if self.specs:
            selectors += self.specs.page_selectors()
        wanted = set()
        for selector in selectors:
            for css in outer_selectors(selector):
                wanted.update(id(elem) for elem in soup.select(css))

        # outermost matches only, in page order, so nothing is duplicated or reordered
        kept, parts = set(), []
        for elem in soup.find_all(True):
            if id(elem) in wanted and not any(id(parent) in kept for parent in elem.parents):
                kept.add(id(elem))
                parts.append(str(elem))

        data = json.dumps(vms).replace('</', '<\\/')
        body = '\n'.join(parts)
        return f"<html><body>\n{body}\n<script>var {self.selectors['comments_var']} = {data};</script>\n</body></html>\n"

    def _parse(self, html: str, url: str, now: Optional[float]) -> Tuple[AuctionDetail, BeautifulSoup, dict]:
        vms = self._extract_js_object(html, self.selectors['comments_var'])
        if vms is None or 'comments' not in vms:
            raise ListingParseError(f"no embedded comment data at {url}")

        soup = BeautifulSoup(html, 'html.parser')

        listing_id = self._extract_listing_id(soup, vms)
        if listing_id is None:
            raise ListingParseError(f"no listing id at {url}")

        detail = AuctionDetail(listing_id=listing_id, url=self._canonical_url(soup, url))
        detail.title = self._text(soup.select_one(self.selectors['title']))
        detail.country = self._text(soup.select_one(self.selectors['country']))

        self._apply_result(soup, detail)
        self._apply_essentials(soup, detail)
        self._apply_groups(soup, detail)
        self._apply_chassis(soup, detail)
        self._apply_history(soup, detail)
        if self.specs:
            self.specs.apply(soup, detail)

        # the listing's wordpress author is the consignor's account
        if detail.seller and str(vms.get('postAuthor', '')).isdigit():
            detail.seller.user_id = int(vms['postAuthor'])

        closing_event = self._apply_comments(vms['comments'], detail)
        self._apply_winner(soup, detail, closing_event)

        self._check_final(soup, detail, time.time() if now is None else now)
        return detail, soup, vms

    def _check_final(self, soup: BeautifulSoup, detail: AuctionDetail, now: float):
        # a live relist reached through bat history parses fine but has partial bids and no winner
        if not soup.select_one(self.selectors['ended_marker']):
            raise NotFinal(f"{detail.url} isn't marked as ended", detail.end_ts)
        if detail.end_ts and detail.end_ts > now:
            raise NotFinal(f"{detail.url} ends in the future", detail.end_ts, marker_missing=False)

        missing = [name for name, present in (
            ('result', detail.result != 'unknown'),
            ('seller', detail.seller),
            ('make or category', detail.make or detail.category),
            ('end time', detail.end_ts),
        ) if not present]
        if missing:
            raise LayoutError(f"{detail.url} has no {', '.join(missing)}")

    def _canonical_url(self, soup: BeautifulSoup, url: str) -> str:
        link = soup.select_one(self.selectors['canonical'])
        href = (link.get('href') or '').strip() if link else ''
        parsed = urlparse(href)
        if parsed.netloc == urlparse(url).netloc and parsed.path.startswith('/listing/'):
            return href
        return url

    def _extract_js_object(self, html: str, var_name: str) -> Optional[dict]:
        marker = f"var {var_name} = "
        start = html.find(marker)
        if start == -1:
            return None
        try:
            obj, _ = json.JSONDecoder().raw_decode(html, start + len(marker))
        except json.JSONDecodeError:
            return None
        return obj

    def _extract_listing_id(self, soup: BeautifulSoup, vms: dict) -> Optional[int]:
        elem = soup.select_one(self.selectors['listing_id'])
        if elem and elem.get('data-listing-currently', '').isdigit():
            return int(elem['data-listing-currently'])

        for comment in vms.get('comments', []):
            if str(comment.get('post', '')).isdigit():
                return int(comment['post'])
        return None

    def _apply_result(self, soup: BeautifulSoup, detail: AuctionDetail):
        info = soup.select_one(self.selectors['result_info'])
        if not info:
            return

        detail.result, detail.high_bid, detail.currency = parse_result_text(info.get_text(' ', strip=True))

        count_elem = soup.select_one(self.selectors['bid_count'])
        if count_elem and count_elem.get_text(strip=True).replace(',', '').isdigit():
            detail.bids_reported = int(count_elem.get_text(strip=True).replace(',', ''))

        date_elem = info.select_one('[data-timestamp]')
        if date_elem and date_elem['data-timestamp'].isdigit():
            detail.end_ts = int(date_elem['data-timestamp'])

    def _apply_essentials(self, soup: BeautifulSoup, detail: AuctionDetail):
        essentials = soup.select_one(self.selectors['essentials'])
        if not essentials:
            return

        # older listings use <b> where newer ones use <strong>
        for label_elem in essentials.select('strong, b'):
            label = label_elem.get_text(strip=True).rstrip(':').strip()
            parent = label_elem.parent

            if label == 'Seller' and not detail.seller:
                link = parent.select_one('a[href*="/member/"]')
                if link:
                    detail.seller = member_from_url(link['href'], link.get_text(strip=True))

            elif label == 'Lot' and not detail.lot_number:
                lot_match = re.search(r'#?(\d+)', parent.get_text(strip=True))
                if lot_match:
                    detail.lot_number = lot_match.group(1)

        # one maps link in both layouts: after <strong>Location</strong> on newer pages, inside <b>Location: ...</b> on older ones
        location = essentials.select_one('a[href*="google.com/maps"]')
        if location:
            detail.location = location.get_text(strip=True) or None

        # a <strong> label on newer pages, plain text in the item on older ones
        for item in essentials.select('.item'):
            match = SELLER_TYPE_RE.match(item.get_text(' ', strip=True))
            if match:
                detail.seller_type = match.group(1)
                break

    def _apply_groups(self, soup: BeautifulSoup, detail: AuctionDetail):
        for link in soup.select(self.selectors['group_link']):
            label_elem = link.select_one(self.selectors['group_label'])
            if not label_elem:
                continue

            label = label_elem.get_text(strip=True).lower()
            value = link.get_text(strip=True)[len(label_elem.get_text(strip=True)):].strip()

            if label == 'make' and not detail.make:
                detail.make = value
            elif label == 'model' and not detail.model:
                detail.model = value
                detail.model_slug = urlparse(link.get('href', '')).path.strip('/') or None
            elif label == 'era' and not detail.era:
                detail.era = value
            elif label == 'origin' and not detail.origin:
                detail.origin = value
            elif label == 'category' and value:
                detail.category = detail.category or value
                if value not in detail.categories:
                    detail.categories.append(value)
                # a listing can carry several category tags; any convertible one counts, as it did in site.py
                if 'convertible' in value.lower() or urlparse(link.get('href', '')).path.strip('/') == 'convertible':
                    detail.convertible = True

        # without category tags the page doesn't say, and a stored answer stands (save_detail keeps it)
        if detail.categories and not detail.convertible:
            detail.convertible = False

    def _apply_chassis(self, soup: BeautifulSoup, detail: AuctionDetail):
        for li in soup.select(self.selectors['listing_details']):
            text = li.get_text(' ', strip=True)
            if text.lower().startswith('chassis'):
                detail.chassis_raw = text.split(':', 1)[-1].strip() or None
                # the number is usually a search link; anything after it is a note
                link = li.select_one('a')
                if link:
                    detail.chassis, detail.vin = parse_chassis(link.get_text(strip=True))
                if not detail.chassis:
                    detail.chassis, detail.vin = parse_chassis(detail.chassis_raw)
                return

    def _apply_history(self, soup: BeautifulSoup, detail: AuctionDetail):
        # bat's own list of other auctions of this same vehicle; the current listing is marked
        for item in soup.select(self.selectors['history_item']):
            href = item.get('href', '')
            if 'current' in item.get('class', []) or '/listing/' not in href:
                continue

            ts_elem = item.select_one('[data-timestamp]')
            summary_elem = item.select_one(self.selectors['history_summary'])
            detail.history.append(HistoryLink(
                url=href,
                end_ts=int(ts_elem['data-timestamp']) if ts_elem and ts_elem['data-timestamp'].isdigit() else None,
                summary=summary_elem.get_text(' ', strip=True) if summary_elem else None
            ))

    def _apply_comments(self, comments: List[dict], detail: AuctionDetail) -> Optional[dict]:
        closing_event = None

        for comment in comments:
            kind = comment.get('type')
            unapproved = str(comment.get('approved')) == '0'

            if kind == 'comment':
                # held for moderation, so not shown on the page
                if unapproved:
                    continue
                detail.n_comments += 1
                if detail.seller and not detail.seller.user_id:
                    author = member_from_url(comment.get('authorUrl'))
                    if author and author.slug == detail.seller.slug:
                        detail.seller.user_id = comment.get('authorId') or None

            elif kind == 'bat-bid' and (comment.get('bidAmount') or 0) > 0:
                if unapproved:
                    detail.notes.append(f"bid {comment.get('id')} is marked unapproved; kept")
                bidder = member_from_comment(comment)
                if bidder:
                    detail.bids.append(Bid(
                        bid_id=int(comment['id']),
                        bidder=bidder,
                        amount=int(comment['bidAmount']),
                        ts=int(comment['timestamp'])
                    ))

            elif kind == 'bat-bid-reserve' and (comment.get('content') or '').lower().startswith('sold'):
                closing_event = comment

        detail.bids.sort(key=lambda b: (b.ts, b.amount))

        if detail.bids:
            top = max(detail.bids, key=lambda b: (b.amount, -b.ts))
            detail.high_bidder = top.bidder
            if detail.high_bid is None:
                detail.high_bid = top.amount

        return closing_event

    def _apply_winner(self, soup: BeautifulSoup, detail: AuctionDetail, closing_event: Optional[dict]):
        if detail.result != 'sold':
            return

        # reuse the bidder's member so the numeric user id comes along
        bidders = {b.bidder.slug: b.bidder for b in detail.bids}

        link = soup.select_one(self.selectors['winner_link'])
        winner = member_from_url(link['href'], link.get_text(strip=True)) if link else None
        if winner:
            detail.winner = bidders.get(winner.slug, winner)
            return

        # "Sold on 9/14/15 for $13,000 to NIACC"; on older listings the event is
        # posted by a staff account, so read the buyer from the text, not the author
        match = BUYER_RE.match((closing_event or {}).get('content') or '')
        if match:
            name = match.group(1).lower()
            by_name = {(b.bidder.display_name or '').lower(): b.bidder for b in detail.bids}
            # a named buyer who never bid is unknown to us, not the high bidder
            detail.winner = by_name.get(name) or bidders.get(name)
            return

        detail.winner = detail.high_bidder

    def _text(self, elem) -> Optional[str]:
        return elem.get_text(strip=True) if elem else None
