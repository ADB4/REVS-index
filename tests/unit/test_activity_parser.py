import unittest
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from sites.bringatrailer.activity_parser import (
    ActivityParser, ListingParseError, parse_result_text, parse_results_page, member_from_comment, normalize_chassis
)


SELECTORS = {
    'comments_var': 'BAT_VMS',
    'listing_id': '[data-listing-currently]',
    'title': 'h1.post-title',
    'country': '.show-country-name',
    'result_info': '.listing-available-info',
    'bid_count': '#listing-bid .number-bids-value',
    'winner_link': "#listing-bid [data-listing-high-bidder-link] a[href*='/member/']",
    'essentials': '.essentials',
    'group_link': '.group-link',
    'group_label': '.group-title-label',
    'listing_details': '.essentials ul li',
    'history_item': '.history .items a.item',
    'history_summary': '.message em'
}

VIN_LI = '<li>Chassis: <a href="https://www.google.com/search?q=WBSBL93453JR22502">WBSBL93453JR22502</a></li>'

HISTORY = '''
    <div class="history"><div class="items">
      <a href="https://bringatrailer.com/listing/2003-bmw-m3-coupe-301/" class="item current">
        <div class="message"><div class="identifier">This Listing</div><em>Sold by <strong>Just1more</strong></em></div></a>
      <a href="https://bringatrailer.com/listing/2003-bmw-m3-coupe-12/" class="item">
        <div class="message"><em>Sold by <strong>Oldowner</strong> to <strong>Just1more</strong> for <strong>USD $19,000</strong></em><br />
        <span class="date-localize" data-timestamp="1600000000">September 13, 2020</span></div></a>
    </div></div>
'''


def comment(cid, kind, name, amount=0, ts=1000, author_id=None, url=None, content=''):
    return {
        'id': cid,
        'type': kind,
        'authorId': author_id if author_id is not None else cid,
        'authorName': name,
        'authorUrl': url if url is not None else f"https://bringatrailer.com/member/{name.lower()}/",
        'bidAmount': amount,
        'timestamp': ts,
        'post': '555',
        'content': content
    }


def listing_html(result_text='Sold for <strong>USD $25,500</strong>', comments=None, post_author='137244',
                 chassis_li=VIN_LI, history=HISTORY, winner_slug=None, listing_id=555):
    if comments is None:
        comments = [
            comment(1, 'comment', 'Just1more', author_id=137244, ts=900),
            comment(2, 'bat-bid', 'Bakerm3', amount=2003, ts=1000, author_id=279592),
            comment(3, 'bat-bid', 'Lummy1088', amount=20000, ts=2000, author_id=90296),
            comment(4, 'comment', 'Kibitzer', ts=2100),
            comment(5, 'bat-bid', 'Bakerm3', amount=21000, ts=2500, author_id=279592),
            comment(6, 'bat-bid', 'Lummy1088', amount=25500, ts=3000, author_id=90296),
            comment(7, 'bat-bid-reserve', 'Lummy1088', author_id=90296, ts=3001,
                    content='Sold on 12/31/25 for USD $25,500.00 to Lummy1088')
        ]
    vms = {'postAuthor': post_author, 'comments': comments}
    return f"""
    <html><body>
    <h1 class="post-title">2003 BMW M3 Coupe 6-Speed</h1>
    <span class="show-country-name">USA</span>
    <span class="listing-available-info"><span data-listing-currently="{listing_id}"></span>
      <span class="info-value">{result_text}
        <span class="date date-localize" data-timestamp="1767210841">on 12/31/25</span></span></span>
    <table id="listing-bid">
      <tr class="listing-stats-stat"><td class="listing-stats-label">Winning Bid</td><td class="listing-stats-value">
        <span data-listing-high-bidder-link="555">{f'by <a href="https://bringatrailer.com/member/{winner_slug}/">{winner_slug}</a>' if winner_slug else ''}</span></td></tr>
      <tr class="listing-stats-stat"><td class="listing-stats-label">Bids</td>
      <td class="listing-stats-value number-bids-value">{sum(1 for c in comments if c['type'] == 'bat-bid')}</td></tr></table>
    <div class="essentials">
      <div class="item item-seller"><strong>Seller</strong>: <a href="https://bringatrailer.com/member/just1more/">Just1more</a></div>
      <strong>Location</strong>: <a href="https://www.google.com/maps/place/Frederick">Frederick, Maryland 21704</a>
      <div class="item additional"><strong>Private Party or Dealer</strong>: Private Party</div>
      <div class="item"><strong>Listing Details</strong><ul>{chassis_li}<li>124k Miles</li></ul></div>
      <div class="item"><strong>Lot</strong> #225550</div>
    </div>
    {history}
    <div class="group-item"><a class="group-link" href="https://bringatrailer.com/bmw/"><strong class="group-title-label">Make</strong>BMW</a></div>
    <div class="group-item"><a class="group-link" href="https://bringatrailer.com/bmw/e46-m3/"><strong class="group-title-label">Model</strong>BMW E46 M3</a></div>
    <div class="group-item"><a class="group-link" href="https://bringatrailer.com/2000s/"><strong class="group-title-label">Era</strong>2000s</a></div>
    <div class="group-item"><a class="group-link" href="https://bringatrailer.com/german/"><strong class="group-title-label">Origin</strong>German</a></div>
    <script>
    var BAT_VMS = {json.dumps(vms)};
    </script>
    </body></html>
    """


class TestResultText(unittest.TestCase):

    def test_sold(self):
        self.assertEqual(parse_result_text('Sold for USD $124,999 <span> on 9/23/2026 </span>'), ('sold', 124999, 'USD'))

    def test_reserve_not_met(self):
        self.assertEqual(parse_result_text('Bid to USD $22,250 <span> on 9/23/2026</span>'), ('reserve_not_met', 22250, 'USD'))

    def test_withdrawn(self):
        self.assertEqual(parse_result_text('Withdrawn on 9/23/2026'), ('withdrawn', None, None))


class TestResultsPage(unittest.TestCase):

    def test_parses_items_and_falls_back_to_title_year(self):
        data = {'items': [{
            'id': 121929409,
            'url': 'https://bringatrailer.com/listing/2024-ineos-grenadier-58/',
            'title': 'Overland-Modified 2024 INEOS Grenadier',
            'sold_text': 'Sold for USD $79,500 <span> on 9/23/2026 </span>',
            'current_bid': 79500,
            'currency': 'USD',
            'timestamp_end': 1790191317,
            'year': None,
            'country_code_alpha3': 'USA',
            'noreserve': True,
            'premium': False
        }]}
        [summary] = parse_results_page(data)
        self.assertEqual(summary.listing_id, 121929409)
        self.assertEqual(summary.result, 'sold')
        self.assertEqual(summary.high_bid, 79500)
        self.assertEqual(summary.year, 2024)
        self.assertTrue(summary.no_reserve)


class TestListingParser(unittest.TestCase):

    def setUp(self):
        self.parser = ActivityParser(SELECTORS)
        self.url = 'https://bringatrailer.com/listing/2003-bmw-m3-coupe-301/'

    def test_sold_listing(self):
        d = self.parser.parse_listing(listing_html(), self.url)

        self.assertEqual(d.listing_id, 555)
        self.assertEqual((d.result, d.high_bid, d.currency, d.end_ts), ('sold', 25500, 'USD', 1767210841))
        self.assertEqual(d.seller.slug, 'just1more')
        self.assertEqual(d.seller.user_id, 137244)
        self.assertEqual(d.seller_type, 'Private Party')
        self.assertEqual(d.lot_number, '225550')
        self.assertEqual(d.location, 'Frederick, Maryland 21704')
        self.assertEqual((d.make, d.model, d.model_slug, d.era, d.origin), ('BMW', 'BMW E46 M3', 'bmw/e46-m3', '2000s', 'German'))
        self.assertEqual(d.n_comments, 2)
        self.assertEqual(d.bids_reported, 4)
        self.assertEqual([b.amount for b in d.bids], [2003, 20000, 21000, 25500])
        self.assertEqual(d.high_bidder.slug, 'lummy1088')
        self.assertEqual(d.winner.slug, 'lummy1088')
        self.assertEqual(d.winner.user_id, 90296)

    def test_reserve_not_met_has_high_bidder_but_no_winner(self):
        comments = [
            comment(2, 'bat-bid', 'Bakerm3', amount=400000, ts=1000),
            comment(3, 'bat-bid', 'Spurs100', amount=471900, ts=2000),
            comment(4, 'bat-bid-reserve', 'Anonymous', author_id=0, url='', ts=2001,
                    content='Reserve not met on 9/23/26 at USD $471,900')
        ]
        d = self.parser.parse_listing(listing_html('Bid to <strong>USD $471,900</strong>', comments), self.url)

        self.assertEqual(d.result, 'reserve_not_met')
        self.assertEqual(d.high_bidder.slug, 'spurs100')
        self.assertIsNone(d.winner)

    def test_sold_without_closing_event_falls_back_to_top_bid(self):
        comments = [comment(2, 'bat-bid', 'Bakerm3', amount=1000), comment(3, 'bat-bid', 'Topdog', amount=5000, ts=2000)]
        d = self.parser.parse_listing(listing_html(comments=comments), self.url)
        self.assertEqual(d.winner.slug, 'topdog')

    def test_winner_from_stats_link(self):
        d = self.parser.parse_listing(listing_html(winner_slug='bakerm3'), self.url)
        self.assertEqual(d.winner.slug, 'bakerm3')
        self.assertEqual(d.winner.user_id, 279592)

    def test_winner_from_closing_text_when_staff_posted_it(self):
        # 2015-era listings: "Sold ... to NIACC" is authored by a bat staff account
        comments = [
            comment(2, 'bat-bid', 'Guidepatch', amount=12500, ts=1000),
            comment(3, 'bat-bid', 'NIACC', amount=12000, ts=900),
            comment(4, 'bat-bid-reserve', 'Al_Durham', ts=2000, content='Sold on 9/14/15 for $12,000 to NIACC')
        ]
        d = self.parser.parse_listing(listing_html(comments=comments), self.url)
        self.assertEqual(d.winner.slug, 'niacc')

    def test_deleted_bidder_keeps_numeric_id(self):
        comments = [comment(2, 'bat-bid', 'Former', amount=1000, author_id=4242, url='')]
        d = self.parser.parse_listing(listing_html(comments=comments), self.url)
        self.assertEqual(d.bids[0].bidder.slug, 'id:4242')

    def test_missing_embedded_data_raises(self):
        with self.assertRaises(ListingParseError):
            self.parser.parse_listing('<html><body>gone</body></html>', self.url)

    def test_vin_and_history_links(self):
        d = self.parser.parse_listing(listing_html(), self.url)

        self.assertEqual((d.chassis, d.vin), ('WBSBL93453JR22502', 'WBSBL93453JR22502'))
        self.assertEqual(len(d.history), 1)
        self.assertEqual(d.history[0].url, 'https://bringatrailer.com/listing/2003-bmw-m3-coupe-12/')
        self.assertEqual(d.history[0].end_ts, 1600000000)
        self.assertEqual(d.history[0].summary, 'Sold by Oldowner to Just1more for USD $19,000')

    def test_short_chassis_is_kept_without_vin(self):
        d = self.parser.parse_listing(listing_html(chassis_li='<li>Chassis: 911 310 1234</li>', history=''), self.url)
        self.assertEqual((d.chassis, d.vin), ('9113101234', None))
        self.assertEqual(d.history, [])

    def test_chassis_placeholders_ignored(self):
        self.assertIsNone(normalize_chassis('Withheld'))
        self.assertIsNone(normalize_chassis('N/A'))
        self.assertIsNone(normalize_chassis('00000000000000000'))
        self.assertEqual(normalize_chassis(' wbsbl93453jr22502 '), 'WBSBL93453JR22502')

    def test_anonymous_comment_is_not_a_member(self):
        self.assertIsNone(member_from_comment(comment(9, 'bat-bid-reserve', 'Anonymous', author_id=0, url='')))


if __name__ == '__main__':
    unittest.main()
