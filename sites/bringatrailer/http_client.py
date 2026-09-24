import re
import time
import random
from typing import List, Optional
from urllib.parse import urlparse

import requests


DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/128.0 Safari/537.36'
)


class DisallowedPathError(Exception):
    pass


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
        user_agent: str = DEFAULT_USER_AGENT
    ):
        self.base_url = base_url.rstrip('/')
        # robots crawl-delay is a floor, never go below it
        self.delay = max(delay, crawl_delay)
        self.jitter = jitter
        self.max_retries = max_retries
        self.timeout = timeout
        self.disallowed = [self._compile_robots_pattern(p) for p in disallowed_paths]
        self.last_request_at = 0.0
        self.request_count = 0

        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': user_agent,
            'Accept-Language': 'en-US,en;q=0.9'
        })

    def get_json(self, path: str, params: Optional[dict] = None) -> dict:
        return self._get(path, params).json()

    def get_html(self, url: str) -> str:
        return self._get(url).text

    def is_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else '')
        return not any(pattern.match(path) for pattern in self.disallowed)

    def _get(self, path_or_url: str, params: Optional[dict] = None) -> requests.Response:
        url = path_or_url if path_or_url.startswith('http') else f"{self.base_url}{path_or_url}"
        if not self.is_allowed(url):
            raise DisallowedPathError(f"robots.txt disallows {url}")

        for attempt in range(self.max_retries + 1):
            self._wait_turn()
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                if attempt == self.max_retries:
                    raise
                self._backoff(attempt, f"network error: {e}")
                continue

            if response.status_code == 200:
                return response

            if response.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                retry_after = response.headers.get('Retry-After', '')
                wait = int(retry_after) if retry_after.isdigit() else None
                self._backoff(attempt, f"http {response.status_code}", wait)
                continue

            response.raise_for_status()

        raise RuntimeError(f"exhausted retries for {url}")

    def _wait_turn(self):
        target_gap = self.delay + random.uniform(0, self.jitter)
        elapsed = time.time() - self.last_request_at
        if elapsed < target_gap:
            time.sleep(target_gap - elapsed)
        self.last_request_at = time.time()
        self.request_count += 1

    def _backoff(self, attempt: int, reason: str, wait: Optional[int] = None):
        wait = wait or min(300, (2 ** attempt) * 15 + random.uniform(0, 5))
        print(f"    {reason}, backing off {wait:.0f}s")
        time.sleep(wait)

    @staticmethod
    def _compile_robots_pattern(pattern: str):
        return re.compile(re.escape(pattern).replace(r'\*', '.*'))
