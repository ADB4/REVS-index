import unittest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from sites.bringatrailer.http_client import BaTClient, DisallowedPathError


class TestRobotsGuard(unittest.TestCase):

    def setUp(self):
        self.client = BaTClient(
            base_url='https://bringatrailer.com',
            disallowed_paths=['/member/', '/listing/*/carfax', '/*utm_source=', '/?q='],
            crawl_delay=1.0,
            delay=0.5
        )

    def test_listing_and_results_api_allowed(self):
        self.assertTrue(self.client.is_allowed('https://bringatrailer.com/listing/2003-bmw-m3-coupe-301/'))
        self.assertTrue(self.client.is_allowed('https://bringatrailer.com/wp-json/bringatrailer/1.0/data/listings-filter'))

    def test_disallowed_paths_blocked(self):
        self.assertFalse(self.client.is_allowed('https://bringatrailer.com/member/just1more/'))
        self.assertFalse(self.client.is_allowed('https://bringatrailer.com/listing/2003-bmw-m3-coupe-301/carfax'))
        self.assertFalse(self.client.is_allowed('https://bringatrailer.com/listing/x/?utm_source=feed'))
        self.assertFalse(self.client.is_allowed('https://bringatrailer.com/?q=m3'))

    def test_disallowed_request_never_sent(self):
        with self.assertRaises(DisallowedPathError):
            self.client.get_html('https://bringatrailer.com/member/just1more/')
        self.assertEqual(self.client.request_count, 0)

    def test_delay_never_below_crawl_delay(self):
        self.assertEqual(self.client.delay, 1.0)


if __name__ == '__main__':
    unittest.main()
