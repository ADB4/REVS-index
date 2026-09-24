import re
import time
import random
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import requests
import urllib3


DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/128.0 Safari/537.36'
)

# worth another try after a pause: throttling, timeouts, server and cdn-to-origin errors
RETRY_STATUSES = {408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
# the url itself is wrong; the rest of the site is unaffected
BAD_URL_STATUSES = {400, 404, 410, 414}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 3
# never sleep longer than this on a server's say-so; stop the run instead
MAX_RETRY_AFTER = 900.0
MAX_BODY_BYTES = 10 * 1024 * 1024
READ_CHUNK = 64 * 1024

# text carried by edge/waf challenge and block pages. only checked on pages that failed to parse:
# real listing pages load recaptcha, so a bare "captcha" would match every one of them
CHALLENGE_MARKERS = [
    'just a moment...', 'checking your browser', 'cf-browser-verification', 'challenge-platform',
    'cf_chl_', 'cf-chl-', 'attention required! | cloudflare', '_incapsula_resource', 'incapsula incident',
    'captcha-delivery.com', 'datadome', 'px-captcha', '_pxappid', 'awswaf', 'aws-waf-token',
    'request unsuccessful', 'access denied', 'enable javascript and cookies to continue', 'ddos-guard',
]


class DisallowedPathError(Exception):
    """robots.txt or the host check refuses this url, so it is never requested"""


class RedirectError(Exception):
    """more redirects than MAX_REDIRECTS"""


class ResponseTooLarge(Exception):
    pass


class HTTPStatusError(Exception):
    """the url itself is bad (404, 410, ...); other urls on the site are unaffected"""

    def __init__(self, status: int, url: str):
        super().__init__(f"http {status} for {url}")
        self.status = status


class SiteUnavailable(Exception):
    """the site as a whole is refusing or failing requests: a block, an outage or a challenge page"""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class RateLimited(SiteUnavailable):
    """the server asked for a longer pause than we sleep in-process; resume_at is a unix timestamp"""

    def __init__(self, resume_at: float, message: str, status: Optional[int] = None):
        super().__init__(message, status)
        self.resume_at = resume_at


@dataclass
class Page:
    url: str
    text: str
    headers: dict


def parse_retry_after(value: Optional[str], now: float) -> Optional[float]:
    """seconds to wait, from either form of Retry-After (delta-seconds or an http-date); None if absent or unreadable"""
    value = (value or '').strip()
    if not value:
        return None
    if value.isascii() and value.isdigit():
        # past ~300 years it's just "a very long time", and time.sleep would overflow
        return float(value) if len(value) <= 10 else 1e10
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, when.timestamp() - now)


def challenge_marker(text: str) -> Optional[str]:
    lowered = (text or '').lower()
    return next((m for m in CHALLENGE_MARKERS if m in lowered), None)


def listing_url(value: str, base_url: str) -> str:
    """a listing url from a full url, one without a scheme, or a bare slug; ValueError for anything else"""
    base = urlparse(base_url)
    text = value.strip()
    if '://' not in text:
        lowered = text.lower()
        if lowered.startswith((base.netloc.lower() + '/', 'www.' + base.netloc.lower() + '/')):
            text = f"{base.scheme}://{text}"
        else:
            path = text.strip('/')
            text = f"{base.scheme}://{base.netloc}/{path if path.startswith('listing/') else 'listing/' + path}/"

    parsed = urlparse(text)
    host = parsed.netloc.lower()
    if host not in (base.netloc.lower(), 'www.' + base.netloc.lower()) or parsed.scheme not in ('http', 'https'):
        raise ValueError(f"{value!r} is not a {base.netloc} url")
    parts = [p for p in parsed.path.split('/') if p]
    if len(parts) != 2 or parts[0] != 'listing':
        raise ValueError(f"{value!r} is not a listing url (expected {base.scheme}://{base.netloc}/listing/<slug>/)")
    return f"{base.scheme}://{base.netloc}/listing/{parts[1]}/"


class BaTClient:

    def __init__(
        self,
        base_url: str,
        disallowed_paths: List[str],
        crawl_delay: float = 1.0,
        delay: float = 3.0,
        jitter: float = 1.5,
        max_retries: int = 4,
        timeout: float = 30.0,
        deadline: float = 120.0,
        max_body_bytes: int = MAX_BODY_BYTES,
        user_agent: str = DEFAULT_USER_AGENT,
        clock=None
    ):
        self.base_url = base_url.rstrip('/')
        base = urlparse(self.base_url)
        self.scheme = base.scheme
        self.host = base.netloc.lower()
        # robots crawl-delay is a floor, never go below it
        self.delay = max(delay, crawl_delay)
        self.jitter = jitter
        self.max_retries = max_retries
        # timeout bounds each socket read; deadline bounds a whole response, however slowly it trickles in
        self.timeout = timeout
        self.deadline = deadline
        self.max_body_bytes = max_body_bytes
        self.disallowed = [self._compile_robots_pattern(p) for p in disallowed_paths]
        # anything with time(), monotonic() and sleep(); pacing uses monotonic so clock steps don't matter
        self.clock = clock if clock is not None else time
        self.last_request_at = float('-inf')
        # a server-requested pause holds every later request, not just the retry
        self.next_allowed_at = float('-inf')
        self.request_count = 0

        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': user_agent,
            'Accept-Language': 'en-US,en;q=0.9'
        })

    def get_json(self, path: str, params: Optional[dict] = None) -> dict:
        response = self._get(path, params)
        try:
            return response.json()
        except ValueError as e:
            marker = challenge_marker(response.text)
            raise SiteUnavailable(f"{response.url} answered 200 without json ({marker or 'unexpected body'})") from e

    def get_page(self, url: str) -> Page:
        response = self._get(url)
        return Page(url=response.url, text=response.text, headers=response.headers)

    def get_html(self, url: str) -> str:
        return self.get_page(url).text

    def is_allowed(self, url: str) -> bool:
        try:
            self._checked_url(url)
        except DisallowedPathError:
            return False
        return True

    def _checked_url(self, path_or_url: str, params: Optional[dict] = None) -> str:
        """the exact url that will go on the wire, after the host and robots checks"""
        url = urljoin(self.base_url + '/', path_or_url)
        parsed = urlparse(url)
        if parsed.scheme != self.scheme or parsed.netloc.lower() != self.host:
            raise DisallowedPathError(f"{url} is not on {self.base_url}")

        # prepare like requests will, so params and percent-escapes are checked as they'll be sent
        try:
            url = requests.Request('GET', url, params=params).prepare().url
        except requests.RequestException as e:
            raise DisallowedPathError(f"can't build a url from {path_or_url!r}: {e}") from e
        parsed = urlparse(url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else '')
        if any(pattern.match(path) for pattern in self.disallowed):
            raise DisallowedPathError(f"robots.txt disallows {url}")
        return url

    def _get(self, path_or_url: str, params: Optional[dict] = None) -> requests.Response:
        url = self._checked_url(path_or_url, params)

        for hop in range(MAX_REDIRECTS + 1):
            response = self._send(url)
            status = response.status_code
            if status == 200:
                return response

            location = response.headers.get('Location')
            if status in REDIRECT_STATUSES and location:
                if hop == MAX_REDIRECTS:
                    raise RedirectError(f"more than {MAX_REDIRECTS} redirects from {path_or_url}")
                # every hop gets the host and robots checks the first url got
                url = self._checked_url(urljoin(url, location))
                continue

            if status in BAD_URL_STATUSES:
                raise HTTPStatusError(status, url)
            raise SiteUnavailable(f"http {status} from {url}", status)

    def _send(self, url: str) -> requests.Response:
        """one url, retried after throttling, server errors and network errors; redirects come back as-is"""
        for attempt in range(self.max_retries + 1):
            last_try = attempt == self.max_retries
            self._wait_turn()
            try:
                response = self._request(url)
            except (requests.ConnectionError, requests.Timeout) as e:
                # the pause holds the next request whether or not it's a retry of this one
                self._pause(self._backoff_wait(attempt), f"network error: {e}")
                if last_try:
                    raise SiteUnavailable(f"network error for {url} after {attempt + 1} tries: {e}") from e
                continue

            status = response.status_code
            if status not in RETRY_STATUSES:
                return response

            self._pause(self._retry_wait(attempt, response, url), f"http {status}")
            if last_try:
                raise SiteUnavailable(f"http {status} from {url} after {attempt + 1} tries", status)

    def _request(self, url: str) -> requests.Response:
        started = self.clock.monotonic()
        response = self.session.get(url, timeout=self.timeout, stream=True, allow_redirects=False)
        try:
            response._content = self._read_body(response, url, started + self.deadline)
            response._content_consumed = True
        finally:
            # a fully read body leaves the connection open for the next request
            response.close()
        return response

    def _read_body(self, response: requests.Response, url: str, deadline: float) -> bytes:
        declared = response.headers.get('Content-Length', '')
        if declared.isdigit() and int(declared) > self.max_body_bytes:
            raise ResponseTooLarge(f"{url} declares {int(declared):,} bytes")

        body = bytearray()
        try:
            for chunk in self._chunks(response):
                body += chunk
                if len(body) > self.max_body_bytes:
                    raise ResponseTooLarge(f"{url} sent more than {self.max_body_bytes:,} bytes")
                if self.clock.monotonic() > deadline:
                    raise requests.exceptions.ReadTimeout(f"{url} took longer than {self.deadline:g}s")
        except urllib3.exceptions.ReadTimeoutError as e:
            raise requests.exceptions.ReadTimeout(str(e)) from e
        except (urllib3.exceptions.ProtocolError, urllib3.exceptions.SSLError, requests.exceptions.ChunkedEncodingError) as e:
            raise requests.ConnectionError(str(e)) from e
        return bytes(body)

    @staticmethod
    def _chunks(response: requests.Response):
        raw = response.raw
        if isinstance(raw, urllib3.response.HTTPResponse) and hasattr(raw, 'read1'):
            # one socket read per chunk, so the deadline is checked even while bytes trickle in
            while True:
                chunk = raw.read1(READ_CHUNK, decode_content=True)
                if not chunk:
                    return
                yield chunk
        else:
            yield from response.iter_content(READ_CHUNK)

    def _retry_wait(self, attempt: int, response: requests.Response, url: str) -> float:
        backoff = self._backoff_wait(attempt)
        retry_after = parse_retry_after(response.headers.get('Retry-After'), self.clock.time())
        if retry_after is None:
            return backoff
        if retry_after > MAX_RETRY_AFTER:
            self.next_allowed_at = max(self.next_allowed_at, self.clock.monotonic() + retry_after)
            raise RateLimited(
                self.clock.time() + retry_after,
                f"http {response.status_code} from {url} asks for a {retry_after:,.0f}s pause",
                response.status_code
            )
        # a floor: never come back sooner than the server asked
        return max(backoff, retry_after)

    def _backoff_wait(self, attempt: int) -> float:
        return min(300, (2 ** attempt) * 15 + random.uniform(0, 5))

    def _pause(self, wait: float, reason: str):
        print(f"    {reason}, backing off {wait:.0f}s")
        self.next_allowed_at = max(self.next_allowed_at, self.clock.monotonic() + wait)

    def _wait_turn(self):
        gap = self.delay + random.uniform(0, self.jitter)
        ready_at = max(self.last_request_at + gap, self.next_allowed_at)
        wait = ready_at - self.clock.monotonic()
        if wait > MAX_RETRY_AFTER:
            raise RateLimited(self.clock.time() + wait, f"still inside a server-requested pause, {wait:,.0f}s left")
        if wait > 0:
            self.clock.sleep(wait)
        self.last_request_at = self.clock.monotonic()
        self.request_count += 1

    @staticmethod
    def _compile_robots_pattern(pattern: str):
        return re.compile(re.escape(pattern).replace(r'\*', '.*'))
