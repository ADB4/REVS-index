"""robots.txt rules as RFC 9309 has them: the group for our product token (or *), the longest matching rule
wins with allow winning a tie, * matches anything, $ anchors the end, and paths compare percent-normalized"""
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional


UNRESERVED = set('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~')
HEX = set(b'0123456789ABCDEFabcdef')


def normalize_path(path: str) -> str:
    """one spelling per path: escaped unreserved characters decoded, other escapes upper-cased, and non-ascii,
    spaces and control characters escaped as utf-8"""
    data = path.encode('utf-8')
    out = []
    i = 0
    while i < len(data):
        byte = data[i]
        if byte == 0x25 and i + 2 < len(data) and data[i + 1] in HEX and data[i + 2] in HEX:
            value = int(data[i + 1:i + 3], 16)
            out.append(chr(value) if chr(value) in UNRESERVED else f"%{value:02X}")
            i += 3
            continue
        out.append(f"%{byte:02X}" if byte < 0x21 or byte > 0x7e else chr(byte))
        i += 1
    return ''.join(out)


@dataclass
class Rule:
    allow: bool
    pattern: str

    def __post_init__(self):
        self.pattern = normalize_path(self.pattern)
        anchored = self.pattern.endswith('$')
        body = self.pattern[:-1] if anchored else self.pattern
        self._regex = re.compile(re.escape(body).replace(r'\*', '.*') + ('$' if anchored else ''))

    def matches(self, path: str) -> bool:
        return self._regex.match(path) is not None


def _agent_matches(agent: str, token: str) -> bool:
    # "User-agent: REVS-index-activity" or "REVS-index-activity/0.2", compared without case
    return agent == token or agent.split('/', 1)[0].strip() == token


class RobotsRules:

    def __init__(self, rules: Iterable[Rule] = (), crawl_delay: Optional[float] = None, group: str = 'none'):
        self.rules: List[Rule] = list(rules)
        self.crawl_delay = crawl_delay
        # which group applied: 'token' (ours), '*', or 'none'
        self.group = group

    @classmethod
    def parse(cls, text: str, token: str) -> 'RobotsRules':
        groups = []
        current = None
        in_agents = False
        for raw in (text or '').splitlines():
            line = raw.split('#', 1)[0].strip()
            if ':' not in line:
                continue
            key, value = (part.strip() for part in line.split(':', 1))
            key = key.lower()

            if key == 'user-agent':
                # consecutive user-agent lines share one group
                if not in_agents:
                    current = {'agents': [], 'rules': [], 'delay': None}
                    groups.append(current)
                current['agents'].append(value.lower())
                in_agents = True
                continue
            in_agents = False
            if current is None:
                continue

            if key in ('allow', 'disallow') and value:
                current['rules'].append(Rule(allow=key == 'allow', pattern=value))
            elif key == 'crawl-delay':
                try:
                    current['delay'] = float(value)
                except ValueError:
                    pass

        token = token.lower()
        ours = [g for g in groups if any(_agent_matches(a, token) for a in g['agents'])]
        chosen = ours or [g for g in groups if '*' in g['agents']]
        delays = [g['delay'] for g in chosen if g['delay'] is not None]
        return cls(
            rules=[rule for g in chosen for rule in g['rules']],
            crawl_delay=max(delays) if delays else None,
            group='token' if ours else '*' if chosen else 'none'
        )

    @classmethod
    def from_disallows(cls, patterns: Iterable[str]) -> 'RobotsRules':
        return cls([Rule(allow=False, pattern=p) for p in patterns], group='*')

    def allows(self, path: str) -> bool:
        """path is the path plus any query string, as it will be requested"""
        path = normalize_path(path or '/')
        if path == '/robots.txt':
            return True
        best = None
        for rule in self.rules:
            if rule.matches(path):
                # the longest pattern wins; at equal length allow beats disallow
                key = (len(rule.pattern), rule.allow)
                if best is None or key > best:
                    best = key
        return best is None or best[1]

    def patterns(self, allow: bool) -> List[str]:
        return sorted({r.pattern for r in self.rules if r.allow == allow})
