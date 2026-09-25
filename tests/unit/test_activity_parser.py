import unittest
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from sites.bringatrailer.activity_parser import (
    ActivityParser, LayoutError, ListingParseError, NotFinal, parse_chassis, parse_result_text, parse_results_page,
    member_from_comment, normalize_chassis
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
    'history_summary': '.message em',
    'ended_marker': '#listing-bid.ended, #listing-bid-container.listing-closed',
    'canonical': 'link[rel=canonical]'
}

VIN_LI = '<li>Chassis: <a href="https://www.google.com/search?q=WBSBL93453JR22502">WBSBL93453JR22502</a></li>'

ESSENTIALS = '''
    <div class="essentials">
      <div class="item item-seller"><strong>Seller</strong>: <a href="https://bringatrailer.com/member/just1more/">Just1more</a></div>
      <strong>Location</strong>: <a href="https://www.google.com/maps/place/Frederick">Frederick, Maryland 21704</a>
      <div class="item additional"><strong>Private Party or Dealer</strong>: Private Party</div>
      <div class="item"><strong>Listing Details</strong><ul>{chassis_li}<li>124k Miles</li></ul></div>
      <div class="item"><strong>Lot</strong> #225550</div>
    </div>
'''

# the layout listings used until about 2019-2021: location inside a <b>, seller type as bare text
OLD_ESSENTIALS = '''
    <div class="essentials">
      <div class="item item-seller"><b>Seller</b>: <a href="https://bringatrailer.com/member/oldseller/">oldseller</a></div>
      <div class="item"><b>Location: <a class="branded-text" href="https://www.google.com/maps/place/Springfield,%20OH">Springfield, OH</a></b></div>
      <div class="item"><strong>Listing Details</strong><ul>{chassis_li}<li>97,800 miles</li></ul></div>
      <div class="item additional">Private Party or Dealer: Dealer</div>
      <div class="item"><b>Lot</b> #553</div>
    </div>
'''

GROUPS = '''
    <div class="group-item"><a class="group-link" href="https://bringatrailer.com/bmw/"><strong class="group-title-label">Make</strong>BMW</a></div>
    <div class="group-item"><a class="group-link" href="https://bringatrailer.com/bmw/e46-m3/"><strong class="group-title-label">Model</strong>BMW E46 M3</a></div>
    <div class="group-item"><a class="group-link" href="https://bringatrailer.com/2000s/"><strong class="group-title-label">Era</strong>2000s</a></div>
    <div class="group-item"><a class="group-link" href="https://bringatrailer.com/german/"><strong class="group-title-label">Origin</strong>German</a></div>
'''

HISTORY = '''
    <div class="history"><div class="items">
      <a href="https://bringatrailer.com/listing/2003-bmw-m3-coupe-301/" class="item current">
        <div class="message"><div class="identifier">This Listing</div><em>Sold by <strong>Just1more</strong></em></div></a>
      <a href="https://bringatrailer.com/listing/2003-bmw-m3-coupe-12/" class="item">
        <div class="message"><em>Sold by <strong>Oldowner</strong> to <strong>Just1more</strong> for <strong>USD $19,000</strong></em><br />
        <span class="date-localize" data-timestamp="1600000000">September 13, 2020</span></div></a>
    </div></div>
'''


def comment(cid, kind, name, amount=0, ts=1000, author_id=None, url=None, content='', approved='1'):
    return {
        'id': cid,
        'type': kind,
        'authorId': author_id if author_id is not None else cid,
        'authorName': name,
        'authorUrl': url if url is not None else f"https://bringatrailer.com/member/{name.lower()}/",
        'bidAmount': amount,
        'timestamp': ts,
        'post': '555',
        'content': content,
        'approved': approved
    }


def listing_html(result_text='Sold for <strong>USD $25,500</strong>', comments=None, post_author='137244',
                 chassis_li=VIN_LI, history=HISTORY, winner_slug=None, listing_id=555, ended=True,
                 essentials=ESSENTIALS, groups=GROUPS, end_ts=1767210841, canonical=None):
    """a synthetic finished listing page with the structure of a real one; ended=False gives a live auction"""
    if comments is None:
        # comment ids are site-wide, so each listing gets its own
        n = listing_id * 10
        comments = [
            comment(n + 1, 'comment', 'Just1more', author_id=137244, ts=900),
            comment(n + 2, 'bat-bid', 'Bakerm3', amount=2003, ts=1000, author_id=279592),
            comment(n + 3, 'bat-bid', 'Lummy1088', amount=20000, ts=2000, author_id=90296),
            comment(n + 4, 'comment', 'Kibitzer', ts=2100, author_id=4),
            comment(n + 5, 'bat-bid', 'Bakerm3', amount=21000, ts=2500, author_id=279592),
            comment(n + 6, 'bat-bid', 'Lummy1088', amount=25500, ts=3000, author_id=90296),
            comment(n + 7, 'bat-bid-reserve', 'Lummy1088', author_id=90296, ts=3001,
                    content='Sold on 12/31/25 for USD $25,500.00 to Lummy1088')
        ]
    vms = {'postAuthor': post_author, 'comments': comments}
    date = f'<span class="date date-localize" data-timestamp="{end_ts}">on 12/31/25</span>' if end_ts else ''
    head = f'<link rel="canonical" href="{canonical}" />' if canonical else ''
    return f"""
    <html><head>{head}</head><body>
    <h1 class="post-title">2003 BMW M3 Coupe 6-Speed</h1>
    <span class="show-country-name">USA</span>
    <span class="listing-available-info"><span data-listing-currently="{listing_id}"></span>
      <span class="info-value">{result_text} {date}</span></span>
    <div id="listing-bid-container" class="{'listing-closed' if ended else 'listing-open'}">
    <table class="listing-stats{' ended' if ended else ''}" id="listing-bid">
      <tr class="listing-stats-stat"><td class="listing-stats-label">Winning Bid</td><td class="listing-stats-value">
        <span data-listing-high-bidder-link="555">{f'by <a href="https://bringatrailer.com/member/{winner_slug}/">{winner_slug}</a>' if winner_slug else ''}</span></td></tr>
      <tr class="listing-stats-stat"><td class="listing-stats-label">Bids</td>
      <td class="listing-stats-value number-bids-value">{sum(1 for c in comments if c['type'] == 'bat-bid')}</td></tr></table>
    </div>
    {essentials.format(chassis_li=chassis_li)}
    {history}
    {groups}
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

    def item(self, **overrides):
        item = {
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
        }
        item.update(overrides)
        return item

    def test_parses_items_and_falls_back_to_title_year(self):
        [summary] = parse_results_page({'items': [self.item()]})
        self.assertEqual(summary.listing_id, 121929409)
        self.assertEqual(summary.result, 'sold')
        self.assertEqual(summary.high_bid, 79500)
        self.assertEqual(summary.year, 2024)
        self.assertTrue(summary.no_reserve)

    def test_price_comes_from_sold_text_not_current_bid(self):
        # a post-auction deal: the last bid was 6,000, bat says it sold for 12,000
        [summary] = parse_results_page({'items': [self.item(sold_text='Sold for USD $12,000', current_bid=6000)]})
        self.assertEqual(summary.high_bid, 12000)

    def test_current_bid_when_sold_text_has_no_amount(self):
        [summary] = parse_results_page({'items': [self.item(sold_text='Withdrawn on 9/23/2026', current_bid=4000)]})
        self.assertEqual((summary.result, summary.high_bid), ('withdrawn', 4000))

    def test_titles_are_unescaped(self):
        [summary] = parse_results_page({'items': [self.item(title='Gilbert &#038; Barker 4&#215;4 Pump')]})
        self.assertEqual(summary.title, 'Gilbert & Barker 4×4 Pump')


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
        self.assertEqual(d.url, self.url)

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

    def test_closing_text_buyer_with_trailing_punctuation(self):
        comments = [
            comment(2, 'bat-bid', 'Topdog', amount=9000, ts=1000),
            comment(3, 'bat-bid', 'M555', amount=8000, ts=900),
            comment(4, 'bat-bid-reserve', 'Staffer', ts=2000, content='Sold on 9/14/15 for $8,000 to M555.')
        ]
        d = self.parser.parse_listing(listing_html(comments=comments), self.url)
        self.assertEqual(d.winner.slug, 'm555')

    def test_closing_text_buyer_matched_by_slug(self):
        comments = [
            comment(2, 'bat-bid', 'Top Dog', amount=9000, url='https://bringatrailer.com/member/topdog/'),
            comment(3, 'bat-bid-reserve', 'Staffer', ts=2000, content='Sold on 9/14/15 for $9,000 to topdog')
        ]
        d = self.parser.parse_listing(listing_html(comments=comments), self.url)
        self.assertEqual(d.winner.slug, 'topdog')

    def test_named_buyer_who_never_bid_is_not_the_high_bidder(self):
        comments = [
            comment(2, 'bat-bid', 'Topdog', amount=9000, ts=1000),
            comment(3, 'bat-bid-reserve', 'Staffer', ts=2000, content='Sold on 9/14/15 for $12,000 to Offlinebuyer')
        ]
        d = self.parser.parse_listing(listing_html(comments=comments), self.url)
        self.assertEqual(d.high_bidder.slug, 'topdog')
        self.assertIsNone(d.winner)

    def test_deleted_bidder_keeps_numeric_id(self):
        comments = [comment(2, 'bat-bid', 'Former', amount=1000, author_id=4242, url='')]
        d = self.parser.parse_listing(listing_html(comments=comments), self.url)
        self.assertEqual(d.bids[0].bidder.slug, 'id:4242')

    def test_unapproved_comments_are_not_counted_and_unapproved_bids_are_noted(self):
        comments = [
            comment(1, 'comment', 'Kibitzer', ts=900),
            comment(2, 'comment', 'Heldback', ts=950, approved='0'),
            comment(3, 'bat-bid', 'Topdog', amount=9000, ts=1000, approved='0'),
        ]
        d = self.parser.parse_listing(listing_html(comments=comments), self.url)
        self.assertEqual(d.n_comments, 1)
        self.assertEqual(len(d.bids), 1)
        self.assertEqual(len(d.notes), 1)
        self.assertIn('unapproved', d.notes[0])

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
        self.assertEqual(parse_chassis('00000000000000000'), (None, None))

    def test_chassis_notes_are_cut_off_not_glued_on(self):
        vin = 'WBSBR934X2EX23144'
        link = f'<a href="https://www.google.com/search?q={vin}">{vin}</a>'
        for li, want in (
            (f'<li>Chassis: {link} (see text)</li>', (vin, vin)),
            (f'<li>Chassis: {link} (Canadian-market)</li>', (vin, vin)),
            (f'<li>Chassis: {vin} (see text)</li>', (vin, vin)),
            (f'<li>Chassis: {vin}, Engine: S54B32</li>', (vin, vin)),
            ('<li>Chassis: WBS-BR934X2EX23144</li>', (vin, vin)),
            # ten digits plus "SEETEXT" would be 17 characters: never a vin
            ('<li>Chassis: 9113600571 (see text)</li>', ('9113600571', None)),
            ('<li>Chassis: 1234567 / Engine: 7654321</li>', ('1234567', None)),
            ('<li>Chassis: 1234567; see text</li>', ('1234567', None)),
        ):
            d = self.parser.parse_listing(listing_html(chassis_li=li, history=''), self.url)
            self.assertEqual((d.chassis, d.vin), want, li)
        self.assertEqual(d.chassis_raw, '1234567; see text')

    def test_old_layout_essentials(self):
        html = listing_html(essentials=OLD_ESSENTIALS, chassis_li='<li>Chassis: WBSBR934X2EX23144</li>', history='')
        d = self.parser.parse_listing(html, self.url)
        self.assertEqual(d.seller.slug, 'oldseller')
        self.assertEqual(d.location, 'Springfield, OH')
        self.assertEqual(d.seller_type, 'Dealer')
        self.assertEqual(d.lot_number, '553')
        self.assertEqual(d.vin, 'WBSBR934X2EX23144')

    def test_canonical_link_is_the_listing_url(self):
        canonical = 'https://bringatrailer.com/listing/2003-bmw-m3-coupe-301/'
        d = self.parser.parse_listing(listing_html(canonical=canonical), 'https://bringatrailer.com/listing/old-slug/')
        self.assertEqual(d.url, canonical)
        # a canonical link to anywhere else is ignored
        d = self.parser.parse_listing(listing_html(canonical='https://elsewhere.example/listing/x/'), self.url)
        self.assertEqual(d.url, self.url)

    def test_anonymous_comment_is_not_a_member(self):
        self.assertIsNone(member_from_comment(comment(9, 'bat-bid-reserve', 'Anonymous', author_id=0, url='')))

    def test_selectors_missing_from_config(self):
        with self.assertRaises(ValueError):
            ActivityParser({k: v for k, v in SELECTORS.items() if k != 'ended_marker'})


class TestFinalityAndLayout(unittest.TestCase):

    def setUp(self):
        self.parser = ActivityParser(SELECTORS)
        self.url = 'https://bringatrailer.com/listing/2003-bmw-m3-coupe-301/'

    def test_live_auction_is_not_final(self):
        live = listing_html(result_text='Current Bid: <strong>USD $6,000</strong>', ended=False, end_ts=None)
        with self.assertRaises(NotFinal) as ctx:
            self.parser.parse_listing(live, self.url)
        self.assertTrue(ctx.exception.marker_missing)

    def test_either_ended_marker_is_enough(self):
        html = listing_html().replace('class="listing-stats ended"', 'class="listing-stats"')
        self.assertEqual(self.parser.parse_listing(html, self.url).result, 'sold')

    def test_end_time_in_the_future_is_not_final(self):
        with self.assertRaises(NotFinal) as ctx:
            self.parser.parse_listing(listing_html(end_ts=1767210841), self.url, now=1767210841 - 60)
        self.assertFalse(ctx.exception.marker_missing)

    def test_missing_result_block_is_a_layout_error(self):
        html = listing_html().replace('listing-available-info', 'listing-info-v2')
        with self.assertRaises(LayoutError) as ctx:
            self.parser.parse_listing(html, self.url)
        self.assertIn('result', str(ctx.exception))

    def test_missing_seller_is_a_layout_error(self):
        html = listing_html().replace('class="essentials"', 'class="essentials-v2"')
        with self.assertRaises(LayoutError) as ctx:
            self.parser.parse_listing(html, self.url)
        self.assertIn('seller', str(ctx.exception))

    def test_missing_make_and_category_is_a_layout_error(self):
        with self.assertRaises(LayoutError) as ctx:
            self.parser.parse_listing(listing_html(groups=''), self.url)
        self.assertIn('make or category', str(ctx.exception))

    def test_category_alone_is_enough(self):
        groups = ('<div class="group-item"><a class="group-link" href="https://bringatrailer.com/parts/">'
                  '<strong class="group-title-label">Category</strong>Parts</a></div>')
        self.assertEqual(self.parser.parse_listing(listing_html(groups=groups), self.url).category, 'Parts')

    def test_missing_end_time_is_a_layout_error(self):
        with self.assertRaises(LayoutError) as ctx:
            self.parser.parse_listing(listing_html(end_ts=None), self.url)
        self.assertIn('end time', str(ctx.exception))

    def test_layout_errors_are_parse_errors(self):
        self.assertTrue(issubclass(LayoutError, ListingParseError))
        self.assertFalse(issubclass(NotFinal, ListingParseError))


if __name__ == '__main__':
    unittest.main()
