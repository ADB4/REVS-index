import io
import unittest
import sys
import os
import time
from contextlib import redirect_stdout
from email.utils import formatdate

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from local_http import LocalServer, FakeClock, closed_port_url
from sites.bringatrailer.http_client import (
    BaTClient, DisallowedPathError, HTTPStatusError, RateLimited, RedirectError, ResponseTooLarge, SiteUnavailable,
    listing_url, parse_retry_after
)


DISALLOWED = ['/member/', '/account/', '/listing/*/carfax', '/*utm_source=', '/?q=']


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

    def test_percent_escapes_are_checked_as_sent(self):
        # requests un-escapes %6D to "m" on the wire
        self.assertFalse(self.client.is_allowed('https://bringatrailer.com/%6Dember/foo/'))

    def test_other_hosts_and_schemes_refused(self):
        self.assertFalse(self.client.is_allowed('https://evil.example/listing/x/'))
        self.assertFalse(self.client.is_allowed('//evil.example/listing/x/'))
        self.assertFalse(self.client.is_allowed('http://bringatrailer.com/listing/x/'))
        self.assertFalse(self.client.is_allowed('https://bringatrailer.com.evil.example/listing/x/'))

    def test_disallowed_request_never_sent(self):
        with self.assertRaises(DisallowedPathError):
            self.client.get_html('https://bringatrailer.com/member/just1more/')
        with self.assertRaises(DisallowedPathError):
            self.client.get_html('https://evil.example/listing/x/')
        self.assertEqual(self.client.request_count, 0)

    def test_delay_never_below_crawl_delay(self):
        self.assertEqual(self.client.delay, 1.0)


class TestListingUrl(unittest.TestCase):

    BASE = 'https://bringatrailer.com'
    WANT = 'https://bringatrailer.com/listing/2003-bmw-m3-coupe-301/'

    def test_accepted_forms(self):
        for value in (
            self.WANT,
            'https://bringatrailer.com/listing/2003-bmw-m3-coupe-301',
            'http://bringatrailer.com/listing/2003-bmw-m3-coupe-301/',
            'bringatrailer.com/listing/2003-bmw-m3-coupe-301/',
            'www.bringatrailer.com/listing/2003-bmw-m3-coupe-301/',
            'https://bringatrailer.com/listing/2003-bmw-m3-coupe-301/?utm_source=feed#comments',
            '2003-bmw-m3-coupe-301',
            'listing/2003-bmw-m3-coupe-301/',
            ' /listing/2003-bmw-m3-coupe-301/ ',
        ):
            self.assertEqual(listing_url(value, self.BASE), self.WANT, value)

    def test_rejected(self):
        for value in (
            'https://evil.example/listing/x/',
            'evil.example/listing/x/',
            'https://bringatrailer.com/member/x/',
            'https://bringatrailer.com/listing/x/carfax',
            'https://bringatrailer.com/',
            'https://bringatrailer.com:8443/listing/x/',
            'ftp://bringatrailer.com/listing/x/',
        ):
            with self.assertRaises(ValueError, msg=value):
                listing_url(value, self.BASE)


class TestRetryAfterParsing(unittest.TestCase):

    NOW = 1_790_000_000.0

    def test_forms(self):
        self.assertEqual(parse_retry_after('120', self.NOW), 120)
        self.assertEqual(parse_retry_after(' 0 ', self.NOW), 0)
        self.assertEqual(parse_retry_after(formatdate(self.NOW + 300, usegmt=True), self.NOW), 300)
        self.assertEqual(parse_retry_after(formatdate(self.NOW - 3600, usegmt=True), self.NOW), 0)

    def test_unreadable_is_ignored(self):
        for value in (None, '', 'soon', '-5', '1.5', '١٢٠', 'Wed, 99 Foo 2026'):
            self.assertIsNone(parse_retry_after(value, self.NOW), value)

    def test_huge_values_dont_overflow(self):
        self.assertGreater(parse_retry_after('9300000000', self.NOW), 1e9)
        self.assertGreater(parse_retry_after('9' * 5000, self.NOW), 1e9)


class LocalCase(unittest.TestCase):

    def setUp(self):
        self.server = LocalServer()
        self.clock = FakeClock()
        # when each request reached the server, on the client's fake clock
        self.hit_times = []
        # the client prints its backoffs
        self.output = io.StringIO()
        self.quiet = redirect_stdout(self.output)
        self.quiet.__enter__()

    def tearDown(self):
        self.quiet.__exit__(None, None, None)
        self.server.close()

    def client(self, **kwargs):
        kwargs.setdefault('clock', self.clock)
        return BaTClient(self.server.base_url, DISALLOWED, crawl_delay=1.0, delay=3.0, **kwargs)

    def timed(self, *responses):
        """a route that answers with each response in turn (the last one repeats) and records when it was hit"""
        responses = list(responses)

        def route(handler):
            self.hit_times.append(self.clock.monotonic())
            return responses.pop(0) if len(responses) > 1 else responses[0]
        return route


class TestStatusHandling(LocalCase):

    def test_200(self):
        self.server.routes['/listing/x/'] = (200, {'Content-Type': 'text/html; charset=utf-8'}, b'<html>ok</html>')
        page = self.client().get_page('/listing/x/')
        self.assertEqual(page.text, '<html>ok</html>')
        self.assertTrue(page.url.endswith('/listing/x/'))

    def test_403_is_site_level_and_not_retried(self):
        self.server.routes['/listing/x/'] = (403, {}, b'blocked')
        client = self.client()
        with self.assertRaises(SiteUnavailable) as ctx:
            client.get_html('/listing/x/')
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(self.server.hits, ['/listing/x/'])

    def test_202_costs_one_request_and_names_the_status(self):
        self.server.routes['/listing/x/'] = (202, {}, b'<html><script>challenge</script></html>')
        client = self.client()
        with self.assertRaises(SiteUnavailable) as ctx:
            client.get_html('/listing/x/')
        self.assertIn('202', str(ctx.exception))
        self.assertEqual((len(self.server.hits), client.request_count), (1, 1))

    def test_404_is_a_bad_url_not_a_site_problem(self):
        client = self.client()
        with self.assertRaises(HTTPStatusError) as ctx:
            client.get_html('/listing/gone/')
        self.assertEqual(ctx.exception.status, 404)
        self.assertNotIsInstance(ctx.exception, SiteUnavailable)
        self.assertEqual(len(self.server.hits), 1)

    def test_cdn_errors_are_retried_then_site_level(self):
        for status in (408, 520, 522, 524):
            self.server.hits.clear()
            self.server.routes['/listing/x/'] = (status, {}, b'')
            with self.assertRaises(SiteUnavailable):
                self.client(max_retries=2).get_html('/listing/x/')
            self.assertEqual(len(self.server.hits), 3, status)

    def test_network_errors_are_retried_then_site_level(self):
        client = BaTClient(closed_port_url(), [], delay=3.0, max_retries=2, clock=self.clock)
        with self.assertRaises(SiteUnavailable) as ctx:
            client.get_html('/listing/x/')
        self.assertIn('network error', str(ctx.exception))
        self.assertEqual(client.request_count, 3)

    def test_json_endpoint_answering_html_is_site_level(self):
        self.server.routes['/api'] = (200, {'Content-Type': 'text/html'}, b'<title>Just a moment...</title>')
        with self.assertRaises(SiteUnavailable) as ctx:
            self.client().get_json('/api', params={'page': 1})
        self.assertIn('just a moment', str(ctx.exception))

    def test_params_are_checked_against_robots(self):
        client = self.client()
        with self.assertRaises(DisallowedPathError):
            client.get_json('/api', params={'page': 1, 'utm_source': 'feed'})
        self.assertEqual(self.server.hits, [])

    def test_connections_are_reused(self):
        self.server.routes['/listing/x/'] = (200, {}, b'a' * 100_000)
        client = self.client()
        for _ in range(3):
            client.get_html('/listing/x/')
        self.assertEqual(self.server.connections, 1)


class TestRetryAfter(LocalCase):

    def gap_after(self, retry_after, status=429):
        """seconds between a throttled request and its retry"""
        headers = {} if retry_after is None else {'Retry-After': retry_after}
        self.server.routes['/listing/x/'] = self.timed((status, headers, b''), (200, {}, b'ok'))
        self.assertEqual(self.client().get_html('/listing/x/'), 'ok')
        self.assertEqual(len(self.hit_times), 2)
        return self.hit_times[1] - self.hit_times[0]

    def test_seconds(self):
        self.assertAlmostEqual(self.gap_after('120'), 120, places=3)

    def test_http_date(self):
        self.assertAlmostEqual(self.gap_after(formatdate(self.clock.time() + 300, usegmt=True), status=503), 300, places=3)

    def test_past_date_and_garbage_fall_back_to_backoff(self):
        for value in (formatdate(self.clock.time() - 3600, usegmt=True), 'soon', None):
            self.hit_times.clear()
            gap = self.gap_after(value)
            self.assertTrue(15 <= gap <= 20.5, (value, gap))

    def test_short_retry_after_is_only_a_floor(self):
        self.assertTrue(15 <= self.gap_after('1') <= 20.5)

    def test_long_pause_stops_instead_of_sleeping(self):
        for value in ('86400', '9300000000', formatdate(self.clock.time() + 7200, usegmt=True)):
            self.server.hits.clear()
            self.server.routes['/listing/x/'] = (429, {'Retry-After': value}, b'')
            client = self.client()
            with self.assertRaises(RateLimited) as ctx:
                client.get_html('/listing/x/')
            self.assertEqual(len(self.server.hits), 1, value)
            self.assertGreater(ctx.exception.resume_at, self.clock.time() + 900)
            self.assertFalse([s for s in self.clock.sleeps if s > 900], 'never sleeps past the cap')
        self.assertAlmostEqual(ctx.exception.resume_at, self.clock.time() + 7200, places=0)

    def test_requests_inside_a_long_pause_stop_without_sleeping(self):
        self.server.routes['/listing/x/'] = (429, {'Retry-After': '9300000000'}, b'')
        client = self.client()
        with self.assertRaises(RateLimited):
            client.get_html('/listing/x/')
        with self.assertRaises(RateLimited) as ctx:
            client.get_html('/listing/y/')
        self.assertEqual(self.server.paths(), ['/listing/x/'])
        self.assertGreater(ctx.exception.resume_at, self.clock.time() + 9e9)
        self.assertFalse([s for s in self.clock.sleeps if s > 900])

    def test_cooldown_carries_over_to_the_next_request(self):
        self.server.routes['/listing/x/'] = (429, {'Retry-After': '600'}, b'')
        self.server.routes['/listing/y/'] = self.timed((200, {}, b'ok'))
        client = self.client(max_retries=0)
        start = self.clock.monotonic()
        with self.assertRaises(SiteUnavailable):
            client.get_html('/listing/x/')
        client.get_html('/listing/y/')
        self.assertGreaterEqual(self.hit_times[0] - start, 600)


class TestRedirects(LocalCase):

    def test_redirect_into_disallowed_path_refused_after_one_request(self):
        self.server.routes['/listing/a/'] = (301, {'Location': '/member/foo/'}, b'')
        self.server.routes['/member/foo/'] = (200, {}, b'member page')
        client = self.client()
        with self.assertRaises(DisallowedPathError):
            client.get_html('/listing/a/')
        self.assertEqual((self.server.hits, client.request_count), (['/listing/a/'], 1))

    def test_redirect_to_another_host_refused(self):
        other = LocalServer()
        try:
            other.routes['/listing/x/'] = (200, {}, b'other host')
            self.server.routes['/listing/c/'] = (302, {'Location': other.base_url + '/listing/x/'}, b'')
            client = self.client()
            with self.assertRaises(DisallowedPathError):
                client.get_html('/listing/c/')
            with self.assertRaises(DisallowedPathError):
                client.get_html(other.base_url + '/listing/x/')
            self.assertEqual(other.hits, [])
            self.assertEqual(client.request_count, 1)
        finally:
            other.close()

    def test_redirect_loop_is_capped_and_not_retried(self):
        self.server.routes['/listing/loop/'] = (302, {'Location': '/listing/loop/'}, b'')
        client = self.client()
        with self.assertRaises(RedirectError):
            client.get_html('/listing/loop/')
        self.assertEqual((len(self.server.hits), client.request_count), (4, 4))
        # pacing only: no backoff
        self.assertTrue(all(s <= 4.5 for s in self.clock.sleeps), self.clock.sleeps)

    def test_same_site_redirect_is_followed_paced_and_counted(self):
        self.server.routes['/listing/old/'] = self.timed((301, {'Location': '/listing/new/'}, b''))
        self.server.routes['/listing/new/'] = self.timed((200, {}, b'new'))
        client = self.client()
        page = client.get_page('/listing/old/')
        self.assertTrue(page.url.endswith('/listing/new/'))
        self.assertEqual((page.text, client.request_count), ('new', 2))
        self.assertGreaterEqual(self.hit_times[1] - self.hit_times[0], 3.0)

    def test_redirect_without_location_is_site_level(self):
        self.server.routes['/listing/x/'] = (302, {}, b'')
        with self.assertRaises(SiteUnavailable) as ctx:
            self.client().get_html('/listing/x/')
        self.assertIn('302', str(ctx.exception))
        self.assertEqual(len(self.server.hits), 1)


class TestResponseLimits(LocalCase):

    def test_declared_length_over_cap(self):
        self.server.routes['/listing/x/'] = (200, {}, b'a' * 2000)
        with self.assertRaises(ResponseTooLarge):
            self.client(max_body_bytes=1000).get_html('/listing/x/')

    def test_streamed_body_over_cap(self):
        def chunked(handler):
            handler.send_response(200)
            handler.send_header('Transfer-Encoding', 'chunked')
            handler.end_headers()
            for _ in range(5):
                handler.wfile.write(b'3e8\r\n' + b'a' * 1000 + b'\r\n')
            handler.wfile.write(b'0\r\n\r\n')
        self.server.routes['/listing/x/'] = chunked
        with self.assertRaises(ResponseTooLarge):
            self.client(max_body_bytes=1000).get_html('/listing/x/')

    def test_trickling_body_hits_the_deadline(self):
        def drip(handler):
            handler.send_response(200)
            handler.send_header('Content-Length', '40')
            handler.end_headers()
            try:
                for _ in range(40):
                    handler.wfile.write(b'x')
                    handler.wfile.flush()
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                pass
        self.server.routes['/listing/x/'] = drip
        client = BaTClient(self.server.base_url, [], crawl_delay=0, delay=0, jitter=0, max_retries=0,
                           timeout=5.0, deadline=0.5)
        started = time.monotonic()
        with self.assertRaises(SiteUnavailable) as ctx:
            client.get_html('/listing/x/')
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIn('longer than', str(ctx.exception))


class TestPacing(unittest.TestCase):

    def test_wall_clock_steps_dont_change_the_gap(self):
        clock = FakeClock()
        client = BaTClient('http://127.0.0.1:9', [], crawl_delay=1.0, delay=3.0, jitter=0.0, clock=clock)
        client._wait_turn()
        clock.wall -= 3600
        client._wait_turn()
        clock.wall += 7200
        client._wait_turn()
        self.assertEqual(clock.sleeps, [3.0, 3.0])


if __name__ == '__main__':
    unittest.main()
