import unittest
from bs4 import BeautifulSoup
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../..'))

from sites.bringatrailer.site import BringATrailerSite
from core.models.scrape_config import ScrapeConfig


class TestBringATrailerSiteExtraction(unittest.TestCase):
    
    def setUp(self):
        config_path = os.path.join(
            os.path.dirname(__file__),
            '../../../config/sites/bringatrailer.yaml'
        )
        self.site = BringATrailerSite(config_path)
    
    def test_extract_title(self):
        html = '<h1 class="post-title">2003 BMW E46 M3</h1>'
        soup = BeautifulSoup(html, 'html.parser')
        title = self.site._extract_title(soup)
        self.assertEqual(title, '2003 BMW E46 M3')
    
    def test_extract_year(self):
        title = '2003 BMW E46 M3'
        year = self.site._extract_year(title)
        self.assertEqual(year, 2003)
    
    def test_extract_year_none(self):
        title = 'BMW E46 M3'
        year = self.site._extract_year(title)
        self.assertIsNone(year)
    
    def test_extract_country(self):
        html = '<span class="show-country-name">USA</span>'
        soup = BeautifulSoup(html, 'html.parser')
        country = self.site._extract_country(soup)
        self.assertEqual(country, 'USA')
    
    def test_is_convertible_true(self):
        html = '<div class="group-title">Convertibles & Cabriolets</div>'
        soup = BeautifulSoup(html, 'html.parser')
        is_conv = self.site._is_convertible(soup)
        self.assertTrue(is_conv)
    
    def test_is_convertible_false(self):
        html = '<div class="group-title">Coupes</div>'
        soup = BeautifulSoup(html, 'html.parser')
        is_conv = self.site._is_convertible(soup)
        self.assertFalse(is_conv)
    
    def test_extract_variant(self):
        config = ScrapeConfig(
            slugs=['e46-m3'],
            make='BMW',
            model_full='E46 M3',
            model_short='M3'
        )
        
        title = '2003 BMW M3 Competition Package'
        variant = self.site._extract_variant(title, config)
        self.assertEqual(variant, 'Competition Package')
    
    def test_extract_variant_standard(self):
        config = ScrapeConfig(
            slugs=['e46-m3'],
            make='BMW',
            model_full='E46 M3',
            model_short='M3'
        )
        
        title = '2003 BMW M3'
        variant = self.site._extract_variant(title, config)
        self.assertEqual(variant, 'Standard')


if __name__ == '__main__':
    unittest.main()
