import os
import gzip
import json
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_pages (
    listing_id INTEGER PRIMARY KEY,
    url TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    parser_version INTEGER NOT NULL,
    -- 'fragments': the embedded comment data and the elements the parser reads; 'page': the whole page
    kind TEXT NOT NULL CHECK (kind IN ('fragments', 'page')),
    html BLOB NOT NULL
);
"""


def raw_db_path(db_path: str) -> Optional[str]:
    """where a database's pages are kept: data/db/bat_activity.db -> data/db/bat_activity_raw.db"""
    if db_path == ':memory:':
        return None
    root, _ = os.path.splitext(db_path)
    return f"{root}_raw.db"


@dataclass
class RawPage:
    listing_id: int
    url: str
    fetched_at: int
    parser_version: int
    kind: str
    html: str


class RawStore:
    """the latest page behind each fetched listing, gzipped, so the parser can be re-run without the network"""

    def __init__(self, path: str):
        if path != ':memory:':
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=60)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    def put(self, listing_id: int, url: str, fetched_at: int, parser_version: int, kind: str, html: str) -> None:
        with self.conn:
            self.conn.execute("""
                INSERT INTO raw_pages (listing_id, url, fetched_at, parser_version, kind, html)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(listing_id) DO UPDATE SET
                    url = excluded.url, fetched_at = excluded.fetched_at, parser_version = excluded.parser_version,
                    kind = excluded.kind, html = excluded.html
            """, (listing_id, url, fetched_at, parser_version, kind, gzip.compress(html.encode('utf-8'))))

    def get(self, listing_id: int) -> Optional[RawPage]:
        row = self.conn.execute("SELECT * FROM raw_pages WHERE listing_id = ?", (listing_id,)).fetchone()
        return self._page(row) if row else None

    def pages(self, listing_ids: Optional[Iterable[int]] = None) -> Iterator[RawPage]:
        if listing_ids is None:
            rows = self.conn.execute("SELECT * FROM raw_pages ORDER BY listing_id")
        else:
            rows = self.conn.execute("""
                SELECT * FROM raw_pages WHERE listing_id IN (SELECT value FROM json_each(?)) ORDER BY listing_id
            """, (json.dumps(list(listing_ids)),))
        for row in rows:
            yield self._page(row)

    def stats(self) -> dict:
        rows = self.conn.execute("SELECT kind, COUNT(*) AS n, SUM(LENGTH(html)) AS bytes FROM raw_pages GROUP BY kind")
        return {r['kind']: {'pages': r['n'], 'bytes': r['bytes']} for r in rows}

    @staticmethod
    def _page(row: sqlite3.Row) -> RawPage:
        return RawPage(
            listing_id=row['listing_id'], url=row['url'], fetched_at=row['fetched_at'],
            parser_version=row['parser_version'], kind=row['kind'], html=gzip.decompress(row['html']).decode('utf-8')
        )
