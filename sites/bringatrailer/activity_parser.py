import re
import json
from typing import List, Optional, Tuple
from urllib.parse import urlparse
from bs4 import BeautifulSoup

from core.models.activity import AuctionSummary, AuctionDetail, Bid, Member, HistoryLink
from extractors.field_extractors.vin_extractor import VINExtractor


# bump when the parser starts capturing new fields, so `fetch --upgrade` knows what to re-fetch
PARSER_VERSION = 2


class ListingParseError(Exception):
    pass


MONEY_RE = re.compile(r'([A-Z]{3})\s*[^\d\s]*\s*([\d,]+)')
MEMBER_URL_RE = re.compile(r'/member/([^/?#]+)/?')
YEAR_RE = re.compile(r'\b(19\d{2}|20\d{2})\b')
VIN_RE = re.compile(f"^{VINExtractor.VIN_PATTERN}$")
REPEATED_CHAR_RE = re.compile(r'^(.)\1+$')


def normalize_chassis(raw: str) -> Optional[str]:
    chassis = re.sub(r'[^A-Z0-9]', '', (raw or '').upper())
    # drop placeholders like "N/A", "Withheld" or "00000000"
    if len(chassis) < 5 or not re.search(r'\d', chassis) or REPEATED_CHAR_RE.match(chassis):
        return None
    return chassis


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
        result, amount, currency = parse_result_text(item.get('sold_text', ''))

        year = item.get('year')
        if not year:
            year_match = YEAR_RE.search(item.get('title', ''))
            year = int(year_match.group(1)) if year_match else None

        summaries.append(AuctionSummary(
            listing_id=int(item['id']),
            url=item['url'],
            title=item.get('title', ''),
            result=result,
            high_bid=item.get('current_bid') or amount,
            currency=item.get('currency') or currency,
            end_ts=item.get('timestamp_end') or item.get('sold_text_timestamp'),
            year=year,
            country_code=item.get('country_code_alpha3') or item.get('country_code'),
            no_reserve=bool(item.get('noreserve')),
            premium=bool(item.get('premium'))
        ))
    return summaries


class ActivityParser:

    def __init__(self, selectors: dict):
        self.selectors = selectors

    def parse_listing(self, html: str, url: str) -> AuctionDetail:
        vms = self._extract_js_object(html, self.selectors['comments_var'])
        if vms is None or 'comments' not in vms:
            raise ListingParseError(f"no embedded comment data at {url}")

        soup = BeautifulSoup(html, 'html.parser')

        listing_id = self._extract_listing_id(soup, vms)
        if listing_id is None:
            raise ListingParseError(f"no listing id at {url}")

        detail = AuctionDetail(listing_id=listing_id, url=url)
        detail.title = self._text(soup.select_one(self.selectors['title']))
        detail.country = self._text(soup.select_one(self.selectors['country']))

        self._apply_result(soup, detail)
        self._apply_essentials(soup, detail)
        self._apply_groups(soup, detail)
        self._apply_chassis(soup, detail)
        self._apply_history(soup, detail)

        # the listing's wordpress author is the consignor's account
        if detail.seller and str(vms.get('postAuthor', '')).isdigit():
            detail.seller.user_id = int(vms['postAuthor'])

        closing_event = self._apply_comments(vms['comments'], detail)
        self._apply_winner(soup, detail, closing_event)

        return detail

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

        for strong in essentials.select('strong'):
            label = strong.get_text(strip=True)
            parent = strong.parent

            if label == 'Seller':
                link = parent.select_one('a[href*="/member/"]')
                if link:
                    detail.seller = member_from_url(link['href'], link.get_text(strip=True))

            elif label == 'Location':
                link = parent.select_one('a[href*="google.com/maps"]')
                if link:
                    detail.location = link.get_text(strip=True)

            elif label == 'Private Party or Dealer':
                value = parent.get_text(strip=True).split(':', 1)[-1].strip()
                if value in ('Private Party', 'Dealer'):
                    detail.seller_type = value

            elif label == 'Lot':
                lot_match = re.search(r'#?(\d+)', parent.get_text(strip=True))
                if lot_match:
                    detail.lot_number = lot_match.group(1)

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
            elif label == 'category' and not detail.category:
                detail.category = value

    def _apply_chassis(self, soup: BeautifulSoup, detail: AuctionDetail):
        for li in soup.select(self.selectors['listing_details']):
            text = li.get_text(' ', strip=True)
            if text.lower().startswith('chassis'):
                detail.chassis = normalize_chassis(text.split(':', 1)[-1])
                if detail.chassis and VIN_RE.match(detail.chassis):
                    detail.vin = detail.chassis
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

            if kind == 'comment':
                detail.n_comments += 1
                if detail.seller and not detail.seller.user_id:
                    author = member_from_url(comment.get('authorUrl'))
                    if author and author.slug == detail.seller.slug:
                        detail.seller.user_id = comment.get('authorId') or None

            elif kind == 'bat-bid' and (comment.get('bidAmount') or 0) > 0:
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
        match = re.search(r'\bto\s+(\S+)\s*$', (closing_event or {}).get('content') or '')
        if match:
            by_name = {(b.bidder.display_name or '').lower(): b.bidder for b in detail.bids}
            if match.group(1).lower() in by_name:
                detail.winner = by_name[match.group(1).lower()]
                return

        detail.winner = detail.high_bidder

    def _text(self, elem) -> Optional[str]:
        return elem.get_text(strip=True) if elem else None
