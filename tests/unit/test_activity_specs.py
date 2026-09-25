import io
import os
import sys
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from test_activity_parser import SELECTORS, listing_html
from test_activity_pipeline import ACTIVITY_CONFIG, FakeClient, summaries
from pipelines.activity_pipeline import ActivityPipeline
from sites.bringatrailer.activity_parser import ActivityParser, PARSER_VERSION
from sites.bringatrailer.listing_specs import ListingSpecs
from storage.activity_db import ActivityDB
from storage.raw_store import RawStore


CONFIG = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), '../../config/sites/bringatrailer.yaml')))
SPECS = ListingSpecs(CONFIG)

# the essentials of a newer listing: the details list the old scraper's extractors read
SPEC_ESSENTIALS = '''
    <div class="essentials">
      <div class="item item-seller"><strong>Seller</strong>: <a href="https://bringatrailer.com/member/testseller/">TestSeller</a></div>
      <div class="item"><strong>Listing Details</strong><ul>
        {chassis_li}
        <li>121k Miles</li>
        <li>6.2-Liter LT2 V8</li>
        <li>Eight-Speed Dual-Clutch Automatic Transaxle</li>
        <li>Rapid Blue Paint</li>
        <li>Jet Black Leather Upholstery</li>
        <li>Z51 Performance Package</li>
      </ul></div>
      <div class="item"><strong>Lot</strong> #100001</div>
    </div>
'''

CATEGORY_GROUPS = '''
    <div class="group-item"><a class="group-link" href="https://bringatrailer.com/chevrolet/"><strong class="group-title-label">Make</strong>Chevrolet</a></div>
    <div class="group-item"><a class="group-link" href="https://bringatrailer.com/chevrolet/corvette-c8/"><strong class="group-title-label">Model</strong>C8 Corvette</a></div>
    <div class="group-item"><a class="group-link" href="https://bringatrailer.com/coupes/"><strong class="group-title-label">Category</strong>Coupes</a></div>
    <div class="group-item"><a class="group-link" href="https://bringatrailer.com/convertible/"><strong class="group-title-label">Category</strong>Convertibles</a></div>
'''

# the listing's excerpt comes first; the comment form carries its own .post-excerpt further down the page
EXCERPT = '''
    <div class="post-excerpt">
      <p>This 2022 Chevrolet Corvette Stingray was sold new in <a href="https://example.com/">Texas</a>, and it has 121k miles.</p>
      <p> </p>
      <p>Power is from a 6.2-liter&nbsp;LT2 V8.</p>
    </div>
    <div class="post-excerpt"><p>a later excerpt that isn't the listing's</p></div>
'''

URL = 'https://bringatrailer.com/listing/2022-chevrolet-corvette-stingray-9/'
FAR_FUTURE = 4_000_000_000
# a fetch time after the synthetic auction ended
FETCHED = 2_000_000_000


def spec_page(listing_id=555, groups=CATEGORY_GROUPS, essentials=SPEC_ESSENTIALS, excerpt=EXCERPT):
    return listing_html(listing_id=listing_id, history='', groups=groups, essentials=essentials, excerpt=excerpt)


class TestListingSpecs(unittest.TestCase):

    def setUp(self):
        self.parser = ActivityParser(SELECTORS, SPECS)

    def test_specs_come_from_the_listing_details(self):
        d = self.parser.parse_listing(spec_page(), URL)
        self.assertEqual(d.listing_details[1:], [
            '121k Miles', '6.2-Liter LT2 V8', 'Eight-Speed Dual-Clutch Automatic Transaxle', 'Rapid Blue Paint',
            'Jet Black Leather Upholstery', 'Z51 Performance Package'
        ])
        # words inside a link keep their spaces
        self.assertEqual(d.listing_details[0], 'Chassis: WBSBL93453JR22502')
        self.assertEqual(d.engine, '6.2-Liter LT2 V8')
        self.assertEqual(d.transmission, 'Eight-Speed Dual-Clutch Automatic Transaxle')
        self.assertEqual(d.mileage, 121000)
        self.assertEqual((d.exterior_color, d.interior_color), ('Rapid Blue Paint', 'Jet Black Leather Upholstery'))

    def test_excerpt_is_the_listings_own_paragraphs(self):
        d = self.parser.parse_listing(spec_page(), URL)
        self.assertEqual(d.excerpt, [
            'This 2022 Chevrolet Corvette Stingray was sold new in Texas, and it has 121k miles.',
            'Power is from a 6.2-liter LT2 V8.'
        ])

    def test_every_category_tag_is_kept_and_any_convertible_one_counts(self):
        d = self.parser.parse_listing(spec_page(), URL)
        self.assertEqual(d.categories, ['Coupes', 'Convertibles'])
        self.assertEqual(d.category, 'Coupes')
        self.assertIs(d.convertible, True)
        self.assertEqual(d.model_slug, 'chevrolet/corvette-c8')

    def test_a_convertible_is_known_by_its_tag_link_too(self):
        groups = CATEGORY_GROUPS.replace('Convertibles</a>', 'Cabriolets &amp; Roadsters</a>')
        self.assertIs(self.parser.parse_listing(spec_page(groups=groups), URL).convertible, True)

    def test_tags_without_a_convertible_one_say_so(self):
        groups = CATEGORY_GROUPS.replace('https://bringatrailer.com/convertible/', 'https://bringatrailer.com/trucks/')
        groups = groups.replace('Convertibles</a>', 'Trucks</a>')
        d = self.parser.parse_listing(spec_page(groups=groups), URL)
        self.assertEqual((d.categories, d.convertible), (['Coupes', 'Trucks'], False))

    def test_no_category_tags_leaves_convertible_unknown(self):
        # make and model tags alone say nothing about the body
        d = self.parser.parse_listing(spec_page(groups=CATEGORY_GROUPS.rsplit('<div class="group-item">', 2)[0]), URL)
        self.assertEqual((d.make, d.categories, d.convertible), ('Chevrolet', [], None))

    def test_a_spec_that_wont_read_is_left_empty_with_a_note(self):
        with mock.patch.object(SPECS.engine, 'extract', side_effect=ValueError('bad pattern')):
            d = self.parser.parse_listing(spec_page(), URL)
        self.assertIsNone(d.engine)
        self.assertEqual(d.mileage, 121000)
        self.assertIn('EngineExtractor failed: bad pattern', d.notes)

    def test_an_implausible_mileage_is_dropped(self):
        essentials = SPEC_ESSENTIALS.replace('<li>121k Miles</li>', '<li>' + '9' * 400 + ' Miles</li>')
        self.assertIsNone(self.parser.parse_listing(spec_page(essentials=essentials), URL).mileage)

    def test_a_listing_with_no_details_or_excerpt_has_empty_specs(self):
        essentials = SPEC_ESSENTIALS.split('<div class="item"><strong>Listing Details</strong>')[0] + '</div>'
        d = self.parser.parse_listing(spec_page(essentials=essentials, excerpt=''), URL)
        self.assertEqual((d.listing_details, d.excerpt, d.engine, d.mileage, d.exterior_color), ([], [], None, None, None))

    def test_mileage_from_the_title_when_the_details_have_none(self):
        essentials = SPEC_ESSENTIALS.replace('<li>121k Miles</li>', '')
        page = spec_page(essentials=essentials).replace('2003 BMW M3 Coupe 6-Speed', '8k-Mile 2022 Chevrolet Corvette')
        self.assertEqual(self.parser.parse_listing(page, URL).mileage, 8000)

    def test_a_parser_without_specs_leaves_them_empty(self):
        d = ActivityParser(SELECTORS).parse_listing(spec_page(), URL)
        self.assertEqual((d.engine, d.mileage, d.listing_details, d.excerpt), (None, None, [], []))
        # the category tags are the parser's own, specs or not
        self.assertEqual(d.categories, ['Coupes', 'Convertibles'])

    def test_fragments_keep_everything_the_specs_read(self):
        detail, fragments = self.parser.parse_with_fragments(spec_page(), URL)
        self.assertIn('post-excerpt', fragments)
        self.assertEqual(self.parser.parse_listing(fragments, URL), detail)
        self.assertEqual(detail.excerpt[1], 'Power is from a 6.2-liter LT2 V8.')


class TestSpecsStorage(unittest.TestCase):

    def setUp(self):
        self.db = ActivityDB(':memory:')
        self.parser = ActivityParser(SELECTORS, SPECS)

    def tearDown(self):
        self.db.close()

    def row(self):
        return self.db.query("SELECT * FROM auctions WHERE listing_id = 555")[0]

    def test_specs_are_stored(self):
        self.db.save_detail(self.parser.parse_listing(spec_page(), URL), 100, PARSER_VERSION)
        row = self.row()
        self.assertEqual((row['engine'], row['transmission'], row['mileage']),
                         ('6.2-Liter LT2 V8', 'Eight-Speed Dual-Clutch Automatic Transaxle', 121000))
        self.assertEqual((row['exterior_color'], row['interior_color']), ('Rapid Blue Paint', 'Jet Black Leather Upholstery'))
        self.assertEqual((json.loads(row['categories']), row['convertible']), (['Coupes', 'Convertibles'], 1))
        self.assertEqual(json.loads(row['listing_details'])[1], '121k Miles')
        self.assertEqual(len(json.loads(row['excerpt'])), 2)

    def test_a_worse_parse_never_blanks_stored_specs(self):
        self.db.save_detail(self.parser.parse_listing(spec_page(), URL), 100, PARSER_VERSION)
        before = dict(self.row())
        essentials = SPEC_ESSENTIALS.split('<div class="item"><strong>Listing Details</strong>')[0] + '</div>'
        worse = self.parser.parse_listing(spec_page(essentials=essentials, excerpt=''), URL)
        self.db.save_detail(worse, 200, PARSER_VERSION)
        # and one whose category tags are gone, say after a markup change
        untagged = self.parser.parse_listing(spec_page(groups=CATEGORY_GROUPS.rsplit('<div class="group-item">', 2)[0]), URL)
        self.db.save_detail(untagged, 200, PARSER_VERSION)
        after = dict(self.row())
        for column in ('engine', 'transmission', 'mileage', 'exterior_color', 'interior_color', 'categories',
                       'convertible', 'listing_details', 'excerpt'):
            self.assertEqual(after[column], before[column], column)
        self.assertEqual(after['fetched_at'], 200)

    def test_a_database_from_before_specs_gains_the_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'v3.db')
            ActivityDB(path).close()
            conn = sqlite3.connect(path)
            for column in ('categories', 'convertible', 'engine', 'transmission', 'mileage', 'exterior_color',
                           'interior_color', 'listing_details', 'excerpt'):
                conn.execute(f"ALTER TABLE auctions DROP COLUMN {column}")
            conn.commit()
            conn.close()

            db = ActivityDB(path)
            try:
                db.save_detail(self.parser.parse_listing(spec_page(), URL), 100, PARSER_VERSION)
                self.assertEqual(db.query("SELECT mileage FROM auctions")[0]['mileage'], 121000)
            finally:
                db.close()


class TestSpecsRawStore(unittest.TestCase):

    def setUp(self):
        self.db = ActivityDB(':memory:')
        self.raw = RawStore(':memory:')
        self.db.upsert_summaries(summaries('https://bringatrailer.com', 1), now=1)
        self.parser = ActivityParser(SELECTORS, SPECS)
        self.page = spec_page(listing_id=5000)

    def tearDown(self):
        self.db.close()
        self.raw.close()

    def fetch(self):
        pipeline = ActivityPipeline(FakeClient([self.page]), self.parser, self.db, ACTIVITY_CONFIG, raw_store=self.raw)
        with redirect_stdout(io.StringIO()):
            return pipeline.fetch(follow_history=False)

    def reparse(self):
        pipeline = ActivityPipeline(None, self.parser, self.db, ACTIVITY_CONFIG, raw_store=self.raw)
        with redirect_stdout(io.StringIO()):
            return pipeline.reparse()

    def version(self):
        return self.db.query("SELECT parser_version FROM auctions WHERE listing_id = 5000")[0]['parser_version']

    def test_fragments_are_kept_when_they_reparse_to_the_same_specs(self):
        self.fetch()
        stored = self.raw.get(5000)
        self.assertEqual(stored.kind, 'fragments')
        self.assertEqual(self.parser.parse_listing(stored.html, stored.url).excerpt[0][:17], 'This 2022 Chevrol')

    def test_the_whole_page_is_kept_when_fragments_lose_the_excerpt(self):
        dropped = lambda soup, vms: ActivityParser._fragments(self.parser, soup, vms).replace('post-excerpt', 'x')
        with mock.patch.object(self.parser, '_fragments', dropped):
            self.fetch()
        stored = self.raw.get(5000)
        self.assertEqual((stored.kind, stored.html), ('page', self.page))

    def test_reparsing_fragments_an_older_parser_kept_leaves_the_listing_due_for_upgrade(self):
        # what parser 3 kept: no excerpt
        detail, fragments = ActivityParser(SELECTORS).parse_with_fragments(self.page, URL, now=FAR_FUTURE)
        self.assertNotIn('post-excerpt', fragments)
        self.raw.put(5000, URL, FETCHED, 3, 'fragments', fragments)
        self.db.save_detail(detail, FETCHED, 3)

        self.assertEqual(self.reparse()['reparsed'], 1)
        row = self.db.query("SELECT engine, mileage, excerpt FROM auctions WHERE listing_id = 5000")[0]
        self.assertEqual((row['engine'], row['mileage'], row['excerpt']), ('6.2-Liter LT2 V8', 121000, None))
        self.assertEqual(self.version(), 3)
        self.assertEqual([r['listing_id'] for r in self.db.pending(upgrade_below=PARSER_VERSION)], [5000])

    def test_reparsing_a_whole_page_brings_the_listing_up_to_date(self):
        self.raw.put(5000, URL, FETCHED, 3, 'page', self.page)
        self.db.save_detail(ActivityParser(SELECTORS).parse_listing(self.page, URL), FETCHED, 3)
        self.reparse()
        self.assertEqual(self.version(), PARSER_VERSION)
        self.assertEqual(len(json.loads(self.db.query("SELECT excerpt FROM auctions")[0]['excerpt'])), 2)


if __name__ == '__main__':
    unittest.main()
