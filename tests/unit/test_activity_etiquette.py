import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from local_http import LocalServer, FakeClock, closed_port_url
from test_activity_parser import SELECTORS
from test_activity_pipeline import ACTIVITY_CONFIG, CliCase, summaries
import cli.commands.activity as cli
from pipelines.activity_pipeline import ActivityPipeline
from pipelines.crawl_budget import CrawlBudget, parse_active_hours
from sites.bringatrailer.activity_parser import ActivityParser
from sites.bringatrailer.http_client import (
    BaTClient, DisallowedPathError, RobotsDenied, RobotsUnavailable, SiteUnavailable
)
from sites.bringatrailer.robots import RobotsRules, normalize_path
from storage.activity_db import ActivityDB


TOKEN = 'REVS-index-activity'


class TestRobotsMatcher(unittest.TestCase):

    def rules(self, text, token=TOKEN):
        return RobotsRules.parse(text, token)

    def test_dollar_anchors_the_end(self):
        r = self.rules("User-agent: *\nDisallow: /*.pdf$\n")
        self.assertFalse(r.allows('/files/a.pdf'))
        self.assertTrue(r.allows('/files/a.pdf?download=1'))
        self.assertTrue(r.allows('/files/a.pdfx'))

    def test_longest_match_wins_and_allow_wins_a_tie(self):
        r = self.rules("User-agent: *\nDisallow: /a/\nAllow: /a/b/\nDisallow: /a/b/c/\nAllow: /x\nDisallow: /x\n")
        self.assertFalse(r.allows('/a/z'))
        self.assertTrue(r.allows('/a/b/z'))
        self.assertFalse(r.allows('/a/b/c/z'))
        self.assertTrue(r.allows('/x'))

    def test_paths_compare_percent_normalized(self):
        r = self.rules("User-agent: *\nDisallow: /member/\nDisallow: /caf%c3%a9/\nDisallow: /a/b\n")
        self.assertFalse(r.allows('/%6Dember/someone/'))
        self.assertFalse(r.allows('/café/menu'))
        self.assertFalse(r.allows('/CAF%C3%A9/menu'.replace('CAF', 'caf')))
        # an escaped slash is a different path
        self.assertTrue(r.allows('/a%2Fb'))
        self.assertEqual(normalize_path('/%7euser/%2f x'), '/~user/%2F%20x')

    def test_query_strings_are_matched(self):
        r = self.rules("User-agent: *\nDisallow: /*utm_source=\nDisallow: /?q=\n")
        self.assertFalse(r.allows('/listing/x/?utm_source=feed'))
        self.assertFalse(r.allows('/?q=m3'))
        self.assertTrue(r.allows('/listing/x/?page=2'))

    def test_our_group_replaces_the_star_group(self):
        text = ("User-agent: *\nDisallow: /\n\n"
                "User-agent: SomeOtherBot\nUser-agent: revs-index-activity/0.2\nDisallow: /private/\nCrawl-delay: 5\n\n"
                "User-agent: REVS-INDEX-ACTIVITY\nDisallow: /secret/\n")
        r = self.rules(text)
        self.assertEqual(r.group, 'token')
        self.assertTrue(r.allows('/listing/x/'))
        self.assertFalse(r.allows('/private/x'))
        self.assertFalse(r.allows('/secret/x'))
        self.assertEqual(r.crawl_delay, 5)
        # anyone else gets the * group
        self.assertFalse(self.rules(text, 'nobody').allows('/listing/x/'))

    def test_edge_cases(self):
        r = self.rules("Disallow: /before-any-agent/\n# a comment\nUser-agent: * # everyone\nDisallow:\n"
                       "Crawl-delay: soon\nDisallow: /tmp/ # scratch\n")
        self.assertTrue(r.allows('/before-any-agent/x'))
        self.assertFalse(r.allows('/tmp/x'))
        self.assertIsNone(r.crawl_delay)
        self.assertTrue(self.rules("User-agent: *\nDisallow: /\n").allows('/robots.txt'))
        self.assertEqual(self.rules("").group, 'none')
        self.assertTrue(self.rules("").allows('/anything'))


ROBOTS_HEADERS = {'Content-Type': 'text/plain'}


class ClientCase(unittest.TestCase):

    def setUp(self):
        self.server = LocalServer()
        self.clock = FakeClock()
        self.output = io.StringIO()
        self.quiet = redirect_stdout(self.output)
        self.quiet.__enter__()
        self.server.routes['/listing/x/'] = (200, {}, b'<html>listing</html>')
        self.server.routes['/api'] = (200, {'Content-Type': 'application/json'}, b'{"items": []}')

    def tearDown(self):
        self.quiet.__exit__(None, None, None)
        self.server.close()

    def robots(self, body, status=200):
        self.server.routes['/robots.txt'] = (status, ROBOTS_HEADERS, body.encode())

    def client(self, **kwargs):
        kwargs.setdefault('clock', self.clock)
        kwargs.setdefault('fetch_robots', True)
        return BaTClient(self.server.base_url, kwargs.pop('static', ['/member/']), crawl_delay=1.0, delay=3.0, **kwargs)


class TestLiveRobots(ClientCase):

    def test_the_live_file_replaces_the_copy(self):
        self.robots("User-agent: *\nDisallow: /private/\n")
        client = self.client()
        client.get_html('/listing/x/')
        self.assertEqual(self.server.paths(), ['/robots.txt', '/listing/x/'])
        with self.assertRaises(DisallowedPathError):
            client.get_html('/private/page/')
        # the copy said /member/ was off limits; the site's own file doesn't
        self.assertTrue(client.is_allowed('/member/someone/'))
        self.assertIn('differs from the copy in the yaml', self.output.getvalue())
        self.assertEqual(self.server.paths(), ['/robots.txt', '/listing/x/'])

    def test_a_disallowed_url_is_refused_before_robots_is_even_read(self):
        client = self.client()
        with self.assertRaises(DisallowedPathError):
            client.get_html('/member/someone/')
        self.assertEqual(self.server.hits, [])

    def test_a_4xx_means_no_rules(self):
        for status in (404, 403, 410):
            self.server.hits.clear()
            self.robots('nope', status)
            client = self.client(static=['/private/'])
            self.server.routes['/private/x/'] = (200, {}, b'ok')
            # the copy in the yaml holds until the site's file has been asked for
            client.get_html('/listing/x/')
            self.assertEqual(client.get_html('/private/x/'), 'ok')
            self.assertIn(f'http {status}: no rules apply', self.output.getvalue())

    def test_a_5xx_or_network_error_stops_the_run(self):
        self.robots('down', 503)
        with self.assertRaises(SiteUnavailable) as ctx:
            self.client(max_retries=1).get_html('/listing/x/')
        self.assertIn("can't read robots.txt", str(ctx.exception))
        self.assertEqual(self.server.paths(), ['/robots.txt', '/robots.txt'])

        client = BaTClient(closed_port_url(), [], fetch_robots=True, max_retries=0, clock=self.clock)
        with self.assertRaises(RobotsUnavailable):
            client.get_html('/listing/x/')
        # a read that failed isn't taken as done: the next request tries robots.txt again
        self.assertIsNone(client.robots_read_at)

    def test_robots_trouble_mid_run_stops_the_fetch_at_once(self):
        self.robots("User-agent: *\nDisallow:\n")
        db = ActivityDB(':memory:')
        db.upsert_summaries(summaries(self.server.base_url, 3), now=1)
        client = self.client(required_paths=('/listing/example/',))
        client.get_html('/listing/x/')
        # a day later the site's robots.txt shuts crawlers out
        self.robots("User-agent: *\nDisallow: /listing/\n")
        self.clock.sleep(25 * 3600)
        with self.assertRaises(RobotsDenied):
            ActivityPipeline(client, ActivityParser(SELECTORS), db, ACTIVITY_CONFIG).fetch(follow_history=False)
        self.assertEqual([p for p in self.server.paths() if p.startswith('/listing/car')], [])
        self.assertEqual({r['fetch_attempts'] for r in db.query("SELECT fetch_attempts FROM auctions")}, {0})
        db.close()

    def test_a_challenge_instead_of_robots_stops_the_run(self):
        self.robots('<html><title>Just a moment...</title></html>')
        with self.assertRaises(SiteUnavailable):
            self.client().get_html('/listing/x/')
        self.assertEqual(self.server.paths(), ['/robots.txt'])

    def test_crawl_delay_raises_the_gap(self):
        self.robots("User-agent: *\nCrawl-delay: 10\n")
        client = self.client(jitter=0)
        client.get_html('/listing/x/')
        client.get_html('/listing/x/')
        self.assertEqual(client.delay, 10)
        self.assertEqual(self.clock.sleeps, [10, 10])

    def test_read_again_after_a_day(self):
        self.robots("User-agent: *\nDisallow: /old/\n")
        client = self.client()
        client.get_html('/listing/x/')
        self.robots("User-agent: *\nDisallow: /listing/y/\n")
        self.clock.sleep(23 * 3600)
        client.get_html('/listing/x/')
        self.assertEqual(self.server.paths().count('/robots.txt'), 1)
        self.clock.sleep(3600)
        client.get_html('/listing/x/')
        self.assertEqual(self.server.paths().count('/robots.txt'), 2)
        self.assertFalse(client.is_allowed('/listing/y/'))

    def test_what_the_run_needs_being_disallowed_stops_it(self):
        self.robots("User-agent: *\nDisallow: /listing/\n")
        with self.assertRaises(RobotsDenied):
            self.client(required_paths=('/api', '/listing/example/')).get_json('/api')
        self.assertEqual(self.server.paths(), ['/robots.txt'])

    def test_params_and_redirect_hops_are_checked_against_the_live_rules(self):
        self.robots("User-agent: *\nDisallow: /*secret=\nDisallow: /hidden/\n")
        self.server.routes['/listing/moved/'] = (301, {'Location': '/hidden/x/'}, b'')
        client = self.client()
        with self.assertRaises(DisallowedPathError):
            client.get_json('/api', params={'secret': 1})
        with self.assertRaises(DisallowedPathError):
            client.get_html('/listing/moved/')
        self.assertEqual(self.server.paths(), ['/robots.txt', '/listing/moved/'])


class TestHeaders(ClientCase):

    def test_honest_user_agent_and_an_accept_header_per_call(self):
        self.robots("User-agent: *\nDisallow:\n")
        client = self.client(user_agent='REVS-index-activity/0.2 (+https://example.test/crawler)')
        client.get_json('/api')
        client.get_html('/listing/x/')
        sent = dict(zip(self.server.paths(), self.server.headers))
        self.assertEqual(sent['/robots.txt']['Accept'], 'text/plain')
        self.assertEqual(sent['/api']['Accept'], 'application/json')
        self.assertEqual(sent['/listing/x/']['Accept'], 'text/html')
        for headers in self.server.headers:
            self.assertEqual(headers['User-Agent'], 'REVS-index-activity/0.2 (+https://example.test/crawler)')
            self.assertFalse([h for h in headers if h.lower().startswith(('sec-ch-', 'sec-fetch-'))])
            self.assertNotIn('X-WP-Nonce', headers)

    def test_default_user_agent_is_not_a_browser(self):
        self.robots("")
        self.client().get_html('/listing/x/')
        self.assertEqual(self.server.headers[0]['User-Agent'], 'REVS-index-activity/0.2')

    def test_first_response_edge_headers_are_logged_once(self):
        self.robots("")
        self.server.routes['/robots.txt'] = (200, {'Server': 'cloudflare', 'CF-Ray': 'abc123', 'Set-Cookie': '__cf_bm=x; Path=/'}, b'')
        client = self.client()
        client.get_html('/listing/x/')
        client.get_html('/listing/x/')
        log = self.output.getvalue()
        self.assertEqual(log.count('first response:'), 1)
        first = next(line for line in log.splitlines() if 'first response:' in line)
        self.assertIn('http 200', first)
        self.assertIn('cloudflare', first)
        self.assertIn('CF-Ray=abc123', first)
        self.assertIn('sets cookies __cf_bm', first)
        self.assertNotIn('__cf_bm=x', log)


class TestPacingExtras(ClientCase):

    def test_long_pauses_now_and_then(self):
        self.robots("")
        client = self.client(jitter=0, long_pause_every=(3, 3), long_pause_seconds=(100, 100))
        for _ in range(6):
            client.get_html('/listing/x/')
        # robots plus six pages: a break before the 4th request, and again three requests later
        self.assertEqual(self.clock.sleeps.count(100), 2)
        self.assertIn('taking a 100s break', self.output.getvalue())


def at(text):
    """a fake clock at a utc time like '2026-09-24 10:00'"""
    return FakeClock(datetime.strptime(text, '%Y-%m-%d %H:%M').replace(tzinfo=timezone.utc).timestamp())


class MetaDict(dict):
    def get_meta(self, key):
        return self.get(key)

    def set_meta(self, key, value):
        self[key] = value


class TestCrawlBudget(unittest.TestCase):

    def budget(self, clock, meta=None, **kwargs):
        meta = meta if meta is not None else MetaDict()
        return CrawlBudget(meta.get_meta, meta.set_meta, clock=clock, tz=timezone.utc, **kwargs), meta

    def spend(self, budget, n):
        with redirect_stdout(io.StringIO()):
            for _ in range(n):
                budget.wait()
                budget.spend()

    def test_a_used_up_day_pauses_until_midnight_then_resumes(self):
        clock = at('2026-09-24 10:00')
        budget, meta = self.budget(clock, daily=3)
        self.spend(budget, 3)
        self.assertEqual(clock.sleeps, [])
        self.spend(budget, 1)
        self.assertEqual(datetime.fromtimestamp(clock.time(), timezone.utc).strftime('%Y-%m-%d %H:%M'), '2026-09-25 00:00')
        self.assertEqual((meta['budget_day'], meta['budget_used']), ('2026-09-25', '1'))

    def test_the_allowance_is_shared_between_runs(self):
        clock = at('2026-09-24 10:00')
        first, meta = self.budget(clock, daily=5)
        self.spend(first, 4)
        second, _ = self.budget(clock, meta, daily=5)
        self.assertIn('4 used today', second.describe())
        self.spend(second, 2)
        self.assertEqual(len(clock.sleeps), 1)

    def test_outside_active_hours_pauses_until_they_start(self):
        for start, resumes in (('2026-09-24 23:30', '2026-09-25 08:00'), ('2026-09-24 07:00', '2026-09-24 08:00')):
            clock = at(start)
            budget, _ = self.budget(clock, active_hours=parse_active_hours('08:00-22:00'))
            self.spend(budget, 1)
            self.assertEqual(datetime.fromtimestamp(clock.time(), timezone.utc).strftime('%Y-%m-%d %H:%M'), resumes)
        clock = at('2026-09-24 12:00')
        budget, _ = self.budget(clock, active_hours=parse_active_hours('08:00-22:00'))
        self.spend(budget, 5)
        self.assertEqual(clock.sleeps, [])

    def test_a_window_can_wrap_past_midnight(self):
        hours = parse_active_hours('22:00-06:00')
        for start, sleeps in (('2026-09-24 23:00', False), ('2026-09-24 03:00', False), ('2026-09-24 07:00', True)):
            clock = at(start)
            budget, _ = self.budget(clock, active_hours=hours)
            self.spend(budget, 1)
            self.assertEqual(bool(clock.sleeps), sleeps, start)

    def test_no_budget_counts_nothing(self):
        clock = at('2026-09-24 10:00')
        budget, meta = self.budget(clock)
        self.spend(budget, 50)
        self.assertEqual((clock.sleeps, dict(meta)), ([], {}))

    def test_bad_windows(self):
        self.assertIsNone(parse_active_hours('off'))
        for bad in ('8-22', '08:00', '25:00-26:00', '08:00-08:00', '08:61-09:00'):
            with self.assertRaises(ValueError, msg=bad):
                parse_active_hours(bad)

    def test_the_client_waits_on_the_budget(self):
        server = LocalServer()
        try:
            server.routes['/listing/x/'] = (200, {}, b'ok')
            clock = at('2026-09-24 21:59')
            budget, _ = self.budget(clock, daily=100, active_hours=parse_active_hours('08:00-22:00'))
            client = BaTClient(server.base_url, [], delay=40.0, jitter=0, clock=clock, budget=budget)
            with redirect_stdout(io.StringIO()):
                for _ in range(3):
                    client.get_html('/listing/x/')
            # two requests before 22:00, the third after 08:00 the next morning
            self.assertEqual(len([s for s in clock.sleeps if s > 3600]), 1)
            self.assertEqual(datetime.fromtimestamp(clock.time(), timezone.utc).strftime('%H:%M'), '08:00')
        finally:
            server.close()


class TestCliBudgetFlags(CliCase):

    def test_budget_settings_are_remembered(self):
        self.seed(1)
        self.serve_listings(1)
        status, out = self.run_cli('fetch', '--daily-budget', '5000', '--active-hours', '00:00-24:00')
        self.assertEqual(status, 0, out)
        self.assertIn('daily budget 5,000 requests', out)
        status, out = self.run_cli('fetch')
        self.assertIn('daily budget 5,000 requests (2 used today), active 00:00-24:00', out)
        status, out = self.run_cli('fetch', '--daily-budget', 'off', '--active-hours', 'off')
        self.assertIn('no daily budget or active hours', out)

    def test_contact_goes_into_the_user_agent(self):
        with open(cli.CONFIG_PATH) as f:
            config = yaml.safe_load(f)
        config['activity']['http']['contact'] = 'https://example.test/crawler'
        with open(cli.CONFIG_PATH, 'w') as f:
            yaml.safe_dump(config, f)
        self.seed(1)
        self.serve_listings(1)
        self.assertEqual(self.run_cli('fetch')[0], 0)
        self.assertEqual({h['User-Agent'] for h in self.server.headers},
                         {'REVS-index-activity/0.2 (+https://example.test/crawler)'})

    def test_bad_budget_flags(self):
        for argv in (('fetch', '--daily-budget', '-5'), ('fetch', '--active-hours', '8pm-9pm')):
            with redirect_stdout(io.StringIO()), mock.patch('sys.stderr', io.StringIO()), \
                    self.assertRaises(SystemExit):
                self.run_cli(*argv)



if __name__ == '__main__':
    unittest.main()
