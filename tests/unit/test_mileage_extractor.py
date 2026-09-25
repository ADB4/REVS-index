import unittest
import sys
import os

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from extractors.field_extractors.mileage_extractor import MileageExtractor


CONFIG_PATH = os.path.join(os.path.dirname(__file__), '../../config/sites/bringatrailer.yaml')


class TestMileageExtractor(unittest.TestCase):
    """the rules both the selenium scraper and the activity crawler use"""

    def setUp(self):
        with open(CONFIG_PATH) as f:
            self.extractor = MileageExtractor(yaml.safe_load(f)['extraction_rules']['mileage'])

    def miles(self, *details, title=''):
        return self.extractor.extract(None, None, {'listing_details': list(details), 'title': title})

    def test_thousands_written_with_a_k(self):
        self.assertEqual(self.miles('121k Miles'), 121000)
        self.assertEqual(self.miles('11k Miles'), 11000)
        self.assertEqual(self.miles('6K miles'), 6000)
        self.assertEqual(self.miles('121 k miles'), 121000)

    def test_fractional_thousands(self):
        self.assertEqual(self.miles('1.5k Miles'), 1500)

    def test_miles_written_out(self):
        self.assertEqual(self.miles('97,800 miles'), 97800)
        self.assertEqual(self.miles('250 Miles Since Rebuild'), 250)

    def test_the_first_detail_with_miles_wins(self):
        self.assertEqual(self.miles('Chassis: WBSBL93453JR22502', '98k Miles', 'Service 5k Miles Ago'), 98000)

    def test_indicated_and_chassis_miles(self):
        self.assertEqual(self.miles('67k Indicated Miles', 'Service 5k Miles Ago'), 67000)
        self.assertEqual(self.miles('Three Owners, 101k Indicated Miles'), 101000)
        self.assertEqual(self.miles('89k Chassis Miles Shown'), 89000)

    def test_long_numbers_stay_exact(self):
        self.assertEqual(self.miles('12345678901234567890 Miles'), 12345678901234567890)
        self.assertEqual(self.miles('9' * 400 + ' Miles'), int('9' * 400))

    def test_kilometers_are_not_miles(self):
        self.assertIsNone(self.miles('32k Kilometers'))
        self.assertEqual(self.miles('12,000 Kilometers (~7k Miles)'), 7000)

    def test_title_when_no_detail_gives_miles(self):
        self.assertEqual(self.miles('TMU', title='59K-Mile 2006 BMW M3 Convertible 6-Speed'), 59000)
        self.assertEqual(self.miles('TMU', title='32.3k-Mile 2004 BMW M3'), 32300)
        self.assertIsNone(self.miles('TMU', title='2006 BMW M3'))


if __name__ == '__main__':
    unittest.main()
