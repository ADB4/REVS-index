import unittest
from bs4 import BeautifulSoup
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../..'))

from extractors.field_extractors.vin_extractor import VINExtractor


class TestVINExtractor(unittest.TestCase):
    
    def setUp(self):
        self.rules = [
            {
                'type': 'selector',
                'selector': '.essentials a',
                'attribute': 'text',
                'regex': r'([A-HJ-NPR-Z0-9]{17})',
                'validation': 'length_17'
            }
        ]
        self.extractor = VINExtractor(self.rules)
    
    def test_extract_valid_vin(self):
        html = '''
        <div class="essentials">
            <a href="/chassis">WBSBL93498CY13754</a>
        </div>
        '''
        soup = BeautifulSoup(html, 'html.parser')
        vin = self.extractor.extract(soup)
        self.assertEqual(vin, 'WBSBL93498CY13754')
    
    def test_extract_vin_with_text(self):
        html = '''
        <div class="essentials">
            <a href="/chassis">Chassis: WBSBL93498CY13754</a>
        </div>
        '''
        soup = BeautifulSoup(html, 'html.parser')
        vin = self.extractor.extract(soup)
        self.assertEqual(vin, 'WBSBL93498CY13754')
    
    def test_invalid_vin_length(self):
        html = '''
        <div class="essentials">
            <a href="/chassis">WBSBL93498</a>
        </div>
        '''
        soup = BeautifulSoup(html, 'html.parser')
        vin = self.extractor.extract(soup)
        self.assertIsNone(vin)
    
    def test_no_vin_found(self):
        html = '''
        <div class="essentials">
            <p>No VIN available</p>
        </div>
        '''
        soup = BeautifulSoup(html, 'html.parser')
        vin = self.extractor.extract(soup)
        self.assertIsNone(vin)
    
    def test_validate_vin(self):
        self.assertTrue(self.extractor._validate_vin('WBSBL93498CY13754'))
        self.assertFalse(self.extractor._validate_vin('WBSBL93498'))
        self.assertFalse(self.extractor._validate_vin(''))


if __name__ == '__main__':
    unittest.main()
