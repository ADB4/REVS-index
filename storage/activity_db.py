import os
import json
import time
import sqlite3
from collections import defaultdict
from typing import Iterable, List, Optional

from core.models.activity import AuctionSummary, AuctionDetail, Member


# a parts listing can quote its donor car's vin; only bat history ties one to a car
PARTS_MAKE = 'Parts and Automobilia'


def vehicle_key(vin: Optional[str], chassis: Optional[str], make: Optional[str]) -> Optional[str]:
    if make == PARTS_MAKE:
        return None
    if vin:
        return vin
    # short pre-1981 chassis numbers are only unique within a make
    if chassis and make:
        return f"{make.lower()}|{chassis}"
    return None


# the listing id is the key; a url can move between listings (renames, relists), so it isn't unique
AUCTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS auctions (
    listing_id INTEGER PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT,
    year INTEGER,
    make TEXT,
    model TEXT,
    model_slug TEXT,
    era TEXT,
    origin TEXT,
    category TEXT,
    chassis TEXT,
    chassis_raw TEXT,
    vin TEXT,
    vehicle_id INTEGER,
    country_code TEXT,
    country TEXT,
    location TEXT,
    no_reserve INTEGER,
    premium INTEGER,
    result TEXT,
    high_bid INTEGER,
    currency TEXT,
    end_ts INTEGER,
    lot_number TEXT,
    seller_slug TEXT REFERENCES members(slug),
    seller_type TEXT,
    high_bidder_slug TEXT REFERENCES members(slug),
    winner_slug TEXT REFERENCES members(slug),
    n_bids INTEGER,
    bids_reported INTEGER,
    n_comments INTEGER,
    discovered_at INTEGER,
    fetched_at INTEGER,
    parser_version INTEGER,
    -- discovery saw the result change after the page was fetched
    refetch INTEGER NOT NULL DEFAULT 0,
    fetch_attempts INTEGER NOT NULL DEFAULT 0,
    fetch_error TEXT
);
"""

TABLES = """
CREATE TABLE IF NOT EXISTS members (
    slug TEXT PRIMARY KEY,
    display_name TEXT,
    user_id INTEGER
);
""" + AUCTIONS_TABLE + """
CREATE TABLE IF NOT EXISTS bids (
    bid_id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES auctions(listing_id),
    bidder_slug TEXT NOT NULL REFERENCES members(slug),
    amount INTEGER NOT NULL,
    ts INTEGER NOT NULL
);

-- "bat history" links from a listing to other auctions of the same vehicle
CREATE TABLE IF NOT EXISTS listing_links (
    listing_id INTEGER NOT NULL REFERENCES auctions(listing_id),
    related_url TEXT NOT NULL,
    related_listing_id INTEGER,
    related_end_ts INTEGER,
    summary TEXT,
    follow_attempts INTEGER NOT NULL DEFAULT 0,
    follow_error TEXT,
    PRIMARY KEY (listing_id, related_url)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# columns added after databases already existed; ALTERed in on open
ADDED_COLUMNS = [
    ('auctions', 'bids_reported', 'INTEGER'),
    ('auctions', 'chassis', 'TEXT'),
    ('auctions', 'vin', 'TEXT'),
    ('auctions', 'vehicle_id', 'INTEGER'),
    ('auctions', 'parser_version', 'INTEGER'),
    ('auctions', 'chassis_raw', 'TEXT'),
    ('auctions', 'refetch', 'INTEGER NOT NULL DEFAULT 0'),
]

# 2: auctions.url no longer UNIQUE
SCHEMA_VERSION = 2

# a reserve-not-met auction can still sell in a post-auction deal; look once more after this long
REFETCH_GRACE = 10 * 86400
# a page saved before, or just as, its auction ended may not be final
FROZEN_MARGIN = 600

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_auctions_seller ON auctions(seller_slug);
CREATE INDEX IF NOT EXISTS idx_auctions_winner ON auctions(winner_slug);
CREATE INDEX IF NOT EXISTS idx_auctions_end ON auctions(end_ts);
CREATE INDEX IF NOT EXISTS idx_auctions_model ON auctions(model_slug);
CREATE INDEX IF NOT EXISTS idx_auctions_pending ON auctions(fetched_at, fetch_attempts);
CREATE INDEX IF NOT EXISTS idx_auctions_vin ON auctions(vin);
CREATE INDEX IF NOT EXISTS idx_auctions_vehicle ON auctions(vehicle_id);
CREATE INDEX IF NOT EXISTS idx_auctions_url ON auctions(url);
CREATE INDEX IF NOT EXISTS idx_bids_bidder ON bids(bidder_slug);
CREATE INDEX IF NOT EXISTS idx_bids_listing ON bids(listing_id);
CREATE INDEX IF NOT EXISTS idx_links_related_url ON listing_links(related_url);
CREATE INDEX IF NOT EXISTS idx_links_related_id ON listing_links(related_listing_id);
"""

# views are dropped and recreated on open so they always match this file
VIEWS = {
    # one row per member per auction they bid on
    'auction_participants': """
        SELECT
            b.listing_id,
            b.bidder_slug AS slug,
            COUNT(*) AS n_bids,
            MAX(b.amount) AS max_bid,
            MIN(b.ts) AS first_bid_ts,
            MAX(b.ts) AS last_bid_ts,
            COALESCE(a.winner_slug = b.bidder_slug, 0) AS won,
            a.seller_slug,
            a.make,
            a.model,
            a.model_slug,
            a.result,
            a.high_bid,
            a.currency,
            a.end_ts
        FROM bids b
        JOIN auctions a ON a.listing_id = b.listing_id
        GROUP BY b.listing_id, b.bidder_slug
    """,

    # selling, bidding and winning totals per member (money columns are USD-only)
    'member_activity': """
        WITH selling AS (
            SELECT
                seller_slug AS slug,
                COUNT(*) AS listed,
                SUM(result = 'sold') AS sold,
                SUM(CASE WHEN result = 'sold' AND currency = 'USD' THEN high_bid END) AS sold_usd,
                MIN(end_ts) AS first_listed_ts,
                MAX(end_ts) AS last_listed_ts
            FROM auctions
            WHERE fetched_at IS NOT NULL AND seller_slug IS NOT NULL
            GROUP BY seller_slug
        ),
        bidding AS (
            SELECT
                bidder_slug AS slug,
                COUNT(*) AS bids,
                COUNT(DISTINCT listing_id) AS auctions_bid,
                MIN(ts) AS first_bid_ts,
                MAX(ts) AS last_bid_ts
            FROM bids
            GROUP BY bidder_slug
        ),
        winning AS (
            SELECT
                winner_slug AS slug,
                COUNT(*) AS won,
                SUM(CASE WHEN currency = 'USD' THEN high_bid END) AS won_usd
            FROM auctions
            WHERE result = 'sold' AND winner_slug IS NOT NULL
            GROUP BY winner_slug
        )
        SELECT
            m.slug,
            m.display_name,
            m.user_id,
            COALESCE(s.listed, 0) AS listed,
            COALESCE(s.sold, 0) AS sold,
            COALESCE(s.sold_usd, 0) AS sold_usd,
            COALESCE(b.auctions_bid, 0) AS auctions_bid,
            COALESCE(b.bids, 0) AS bids,
            COALESCE(w.won, 0) AS won,
            COALESCE(w.won_usd, 0) AS won_usd,
            ROUND(1.0 * COALESCE(w.won, 0) / NULLIF(b.auctions_bid, 0), 3) AS win_rate,
            MIN(COALESCE(s.first_listed_ts, b.first_bid_ts), COALESCE(b.first_bid_ts, s.first_listed_ts)) AS first_seen_ts,
            MAX(COALESCE(s.last_listed_ts, b.last_bid_ts), COALESCE(b.last_bid_ts, s.last_listed_ts)) AS last_seen_ts
        FROM members m
        LEFT JOIN selling s ON s.slug = m.slug
        LEFT JOIN bidding b ON b.slug = m.slug
        LEFT JOIN winning w ON w.slug = m.slug
    """,

    # how often each bidder shows up on a given seller's auctions
    'seller_bidder_pairs': """
        SELECT
            seller_slug,
            slug AS bidder_slug,
            COUNT(*) AS auctions_bid,
            SUM(won) AS won,
            SUM(n_bids) AS bids
        FROM auction_participants
        WHERE seller_slug IS NOT NULL
        GROUP BY seller_slug, slug
    """,

    # every auction of every tracked vehicle, in order, with what changed since the previous one
    'vehicle_timeline': """
        SELECT
            t.*,
            CASE
                WHEN t.seq = 1 THEN 'first_seen'
                WHEN t.prev_result = 'sold' AND t.seller_slug = t.prev_winner_slug THEN 'resold_by_buyer'
                WHEN t.prev_result = 'sold' AND t.seller_slug = t.prev_seller_slug THEN 'relisted_after_sale'
                WHEN t.seller_slug = t.prev_seller_slug THEN 'relisted_unsold'
                ELSE 'new_seller'
            END AS transition,
            ROUND((t.end_ts - t.prev_end_ts) / 86400.0, 1) AS days_since_prev,
            CASE WHEN t.currency = t.prev_currency THEN t.high_bid - t.prev_high_bid END AS change_since_prev
        FROM (
            SELECT
                a.vehicle_id, a.listing_id, a.url, a.title, a.vin, a.chassis, a.make, a.model_slug,
                a.end_ts, a.result, a.high_bid, a.currency, a.seller_slug, a.winner_slug,
                ROW_NUMBER() OVER w AS seq,
                COUNT(*) OVER (PARTITION BY a.vehicle_id) AS n_auctions,
                LAG(a.end_ts) OVER w AS prev_end_ts,
                LAG(a.result) OVER w AS prev_result,
                LAG(a.high_bid) OVER w AS prev_high_bid,
                LAG(a.currency) OVER w AS prev_currency,
                LAG(a.seller_slug) OVER w AS prev_seller_slug,
                LAG(a.winner_slug) OVER w AS prev_winner_slug
            FROM auctions a
            WHERE a.vehicle_id IS NOT NULL AND a.fetched_at IS NOT NULL
            WINDOW w AS (PARTITION BY a.vehicle_id ORDER BY a.end_ts, a.listing_id)
        ) t
    """,

    # cars a member won and later sold again on bat themselves (gross prices, before fees)
    'member_resales': """
        SELECT
            s.winner_slug AS slug,
            s.vehicle_id,
            s.vin,
            s.make,
            s.title,
            s.listing_id AS bought_listing_id,
            s.end_ts AS bought_ts,
            s.high_bid AS bought_price,
            s.next_listing_id AS sold_listing_id,
            s.next_end_ts AS sold_ts,
            s.next_high_bid AS sold_price,
            ROUND((s.next_end_ts - s.end_ts) / 86400.0, 1) AS days_held,
            CASE WHEN s.currency = s.next_currency THEN s.next_high_bid - s.high_bid END AS price_change,
            CASE WHEN s.currency = s.next_currency THEN ROUND(1.0 * (s.next_high_bid - s.high_bid) / s.high_bid, 3) END AS pct_change,
            s.currency
        FROM (
            SELECT
                a.*,
                LEAD(a.listing_id) OVER w AS next_listing_id,
                LEAD(a.end_ts) OVER w AS next_end_ts,
                LEAD(a.high_bid) OVER w AS next_high_bid,
                LEAD(a.currency) OVER w AS next_currency,
                LEAD(a.seller_slug) OVER w AS next_seller_slug
            FROM auctions a
            WHERE a.vehicle_id IS NOT NULL AND a.result = 'sold' AND a.fetched_at IS NOT NULL
            WINDOW w AS (PARTITION BY a.vehicle_id ORDER BY a.end_ts, a.listing_id)
        ) s
        WHERE s.next_seller_slug = s.winner_slug
    """,
}


class ActivityDB:

    def __init__(self, path: str):
        if path != ':memory:':
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA foreign_keys=ON')

        self.conn.executescript(TABLES)
        with self.conn:
            self._add_missing_columns()
        self._migrate()
        with self.conn:
            self.conn.executescript(INDEXES)
            for name, sql in VIEWS.items():
                self.conn.execute(f"DROP VIEW IF EXISTS {name}")
                self.conn.execute(f"CREATE VIEW {name} AS {sql}")

    def close(self):
        self.conn.close()

    def upsert_summaries(self, summaries: List[AuctionSummary], now: int, skipped: Optional[list] = None) -> int:
        """store discovered auctions; returns how many were new. an item that can't be stored is appended to
        skipped as (listing_id, error) instead of rolling back the rest of the page"""
        known = self.known_listing_ids([s.listing_id for s in summaries])
        stored = []

        with self.conn:
            self.conn.execute("BEGIN")
            for s in summaries:
                self.conn.execute("SAVEPOINT summary")
                try:
                    # once a page has been fetched its own title, result, price and end time win:
                    # discovery only fills gaps and flags a later change to "sold" for a re-fetch
                    self.conn.execute("""
                        INSERT INTO auctions (
                            listing_id, url, title, year, country_code, no_reserve, premium,
                            result, high_bid, currency, end_ts, discovered_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(listing_id) DO UPDATE SET
                            url = excluded.url,
                            title = CASE WHEN auctions.fetched_at IS NULL THEN excluded.title
                                         ELSE COALESCE(auctions.title, excluded.title) END,
                            year = COALESCE(auctions.year, excluded.year),
                            country_code = excluded.country_code,
                            no_reserve = excluded.no_reserve,
                            premium = excluded.premium,
                            result = CASE WHEN auctions.fetched_at IS NULL THEN COALESCE(NULLIF(excluded.result, 'unknown'), auctions.result, excluded.result)
                                          ELSE COALESCE(auctions.result, excluded.result) END,
                            high_bid = CASE WHEN auctions.fetched_at IS NULL THEN COALESCE(excluded.high_bid, auctions.high_bid)
                                            ELSE COALESCE(auctions.high_bid, excluded.high_bid) END,
                            currency = CASE WHEN auctions.fetched_at IS NULL THEN COALESCE(excluded.currency, auctions.currency)
                                            ELSE COALESCE(auctions.currency, excluded.currency) END,
                            end_ts = CASE WHEN auctions.fetched_at IS NULL THEN COALESCE(excluded.end_ts, auctions.end_ts)
                                          ELSE COALESCE(auctions.end_ts, excluded.end_ts) END,
                            refetch = CASE WHEN auctions.fetched_at IS NOT NULL AND excluded.result = 'sold'
                                                AND COALESCE(auctions.result, '') != 'sold' THEN 1
                                           ELSE auctions.refetch END
                    """, (
                        s.listing_id, s.url, s.title, s.year, s.country_code, int(s.no_reserve), int(s.premium),
                        s.result, s.high_bid, s.currency, s.end_ts, now
                    ))
                    self.conn.execute("""
                        UPDATE listing_links SET related_listing_id = ?
                        WHERE related_url = ? AND related_listing_id IS NULL
                    """, (s.listing_id, s.url))
                except sqlite3.Error as e:
                    self.conn.execute("ROLLBACK TO summary")
                    if skipped is not None:
                        skipped.append((s.listing_id, str(e)))
                else:
                    stored.append(s)
                self.conn.execute("RELEASE summary")

        return len([s for s in stored if s.listing_id not in known])

    def save_detail(self, detail: AuctionDetail, now: int, parser_version: int, requested_url: Optional[str] = None) -> None:
        """store a parsed listing; requested_url is the url fetched, when a redirect or canonical link differs"""
        members = [detail.seller, detail.high_bidder, detail.winner] + [b.bidder for b in detail.bids]

        with self.conn:
            for member in members:
                if member:
                    self._upsert_member(member)

            self.conn.execute("""
                INSERT INTO auctions (listing_id, url, discovered_at) VALUES (?, ?, ?)
                ON CONFLICT(listing_id) DO NOTHING
            """, (detail.listing_id, detail.url, now))

            # a field the page didn't yield never blanks one an earlier fetch stored
            self.conn.execute("""
                UPDATE auctions SET
                    title = COALESCE(?, title),
                    make = COALESCE(?, make), model = COALESCE(?, model), model_slug = COALESCE(?, model_slug),
                    era = COALESCE(?, era), origin = COALESCE(?, origin), category = COALESCE(?, category),
                    chassis = COALESCE(?, chassis), chassis_raw = COALESCE(?, chassis_raw), vin = COALESCE(?, vin),
                    country = COALESCE(?, country), location = COALESCE(?, location),
                    result = COALESCE(NULLIF(?, 'unknown'), result),
                    high_bid = COALESCE(?, high_bid),
                    currency = COALESCE(?, currency),
                    end_ts = COALESCE(?, end_ts),
                    lot_number = COALESCE(?, lot_number),
                    seller_slug = COALESCE(?, seller_slug), seller_type = COALESCE(?, seller_type),
                    high_bidder_slug = ?, winner_slug = ?,
                    n_bids = ?, bids_reported = ?, n_comments = ?,
                    fetched_at = ?,
                    parser_version = ?,
                    refetch = 0,
                    fetch_attempts = 0,
                    fetch_error = NULL
                WHERE listing_id = ?
            """, (
                detail.title,
                detail.make, detail.model, detail.model_slug, detail.era, detail.origin, detail.category,
                detail.chassis, detail.chassis_raw, detail.vin,
                detail.country, detail.location,
                detail.result,
                detail.high_bid,
                detail.currency,
                detail.end_ts,
                detail.lot_number,
                detail.seller.slug if detail.seller else None, detail.seller_type,
                detail.high_bidder.slug if detail.high_bidder else None,
                detail.winner.slug if detail.winner else None,
                len(detail.bids), detail.bids_reported, detail.n_comments,
                now,
                parser_version,
                detail.listing_id
            ))

            self.conn.execute("DELETE FROM bids WHERE listing_id = ?", (detail.listing_id,))
            # a plain insert: a bid already stored under another listing is an error, never moved silently
            for b in detail.bids:
                try:
                    self.conn.execute(
                        "INSERT INTO bids (bid_id, listing_id, bidder_slug, amount, ts) VALUES (?, ?, ?, ?, ?)",
                        (b.bid_id, detail.listing_id, b.bidder.slug, b.amount, b.ts)
                    )
                except sqlite3.IntegrityError as e:
                    owner = self.conn.execute("SELECT listing_id FROM bids WHERE bid_id = ?", (b.bid_id,)).fetchone()
                    if owner is None:
                        raise
                    raise ValueError(f"bid {b.bid_id} is already stored under listing {owner['listing_id']}") from e

            # links the page no longer shows go; the rest keep their follow attempts
            self.conn.execute("""
                DELETE FROM listing_links
                WHERE listing_id = ? AND related_url NOT IN (SELECT value FROM json_each(?))
            """, (detail.listing_id, json.dumps([h.url for h in detail.history])))
            self.conn.executemany("""
                INSERT INTO listing_links (listing_id, related_url, related_listing_id, related_end_ts, summary)
                VALUES (?, ?, (SELECT listing_id FROM auctions WHERE url = ? ORDER BY fetched_at IS NULL, listing_id LIMIT 1), ?, ?)
                ON CONFLICT(listing_id, related_url) DO UPDATE SET
                    related_listing_id = COALESCE(excluded.related_listing_id, listing_links.related_listing_id),
                    related_end_ts = excluded.related_end_ts,
                    summary = excluded.summary
            """, [(detail.listing_id, h.url, h.url, h.end_ts, h.summary) for h in detail.history])

            # other listings may already point here, possibly under an older url for this listing
            self.conn.execute("""
                UPDATE listing_links SET related_listing_id = :id
                WHERE related_listing_id IS NULL
                  AND related_url IN (:url, :requested, (SELECT url FROM auctions WHERE listing_id = :id))
            """, {'id': detail.listing_id, 'url': detail.url, 'requested': requested_url or detail.url})

    def mark_error(self, listing_id: int, error: str, count_attempt: bool = True) -> None:
        """record why a fetch failed; count_attempt=False for failures that aren't the listing's fault"""
        with self.conn:
            self.conn.execute("""
                UPDATE auctions SET fetch_attempts = fetch_attempts + ?, fetch_error = ?
                WHERE listing_id = ?
            """, (int(count_attempt), error[:500], listing_id))

    def reset_errors(self) -> dict:
        """make listings and history links that ran out of attempts eligible again"""
        with self.conn:
            listings = self.conn.execute("""
                UPDATE auctions SET fetch_attempts = 0, fetch_error = NULL
                WHERE fetched_at IS NULL AND (fetch_attempts > 0 OR fetch_error IS NOT NULL)
            """).rowcount
            links = self.conn.execute("""
                UPDATE listing_links SET follow_attempts = 0, follow_error = NULL
                WHERE follow_attempts > 0 OR follow_error IS NOT NULL
            """).rowcount
        return {'listings': listings, 'links': links}

    def mark_url_error(self, url: str, error: str, count_attempt: bool = True) -> None:
        row = self.conn.execute("SELECT listing_id FROM auctions WHERE url = ?", (url,)).fetchone()
        if row:
            self.mark_error(row['listing_id'], error, count_attempt)
            return

        with self.conn:
            self.conn.execute("""
                UPDATE listing_links SET follow_attempts = follow_attempts + ?, follow_error = ?
                WHERE related_url = ?
            """, (int(count_attempt), error[:500], url))

    def pending(
        self,
        limit: Optional[int] = None,
        since_ts: Optional[int] = None,
        max_attempts: int = 3,
        upgrade_below: Optional[int] = None,
        listing_ids: Optional[Iterable[int]] = None,
        now: Optional[float] = None,
        grace: int = REFETCH_GRACE
    ) -> List[sqlite3.Row]:
        """listings to fetch: never fetched, flagged for a re-fetch, possibly saved before they were final,
        reserve-not-met ones whose deal window has passed, and (with upgrade_below) older parser versions"""
        stale = " OR COALESCE(parser_version, 0) < :upgrade" if upgrade_below else ""
        sql = f"""
            SELECT listing_id, url, end_ts FROM auctions
            WHERE fetch_attempts < :max_attempts AND (
                fetched_at IS NULL
                OR refetch = 1
                OR fetched_at < end_ts + :frozen
                OR (result = 'reserve_not_met' AND fetched_at < end_ts + :grace AND end_ts < :now - :grace)
                {stale}
            )
        """
        params = {'max_attempts': max_attempts, 'upgrade': upgrade_below, 'frozen': FROZEN_MARGIN, 'grace': grace,
                  'now': int(time.time() if now is None else now)}
        if since_ts:
            sql += " AND end_ts >= :since"
            params['since'] = since_ts
        if listing_ids is not None:
            sql += " AND listing_id IN (SELECT value FROM json_each(:ids))"
            params['ids'] = json.dumps(list(listing_ids))
        sql += " ORDER BY fetched_at IS NOT NULL, end_ts DESC"
        if limit is not None:
            sql += " LIMIT :limit"
            params['limit'] = limit
        return self.conn.execute(sql, params).fetchall()

    def pending_history(self, max_attempts: int = 3) -> List[sqlite3.Row]:
        return self.conn.execute("""
            SELECT NULL AS listing_id, related_url AS url, MAX(related_end_ts) AS end_ts
            FROM listing_links
            WHERE related_listing_id IS NULL
            GROUP BY related_url
            HAVING MAX(follow_attempts) < ?
            ORDER BY MAX(related_end_ts) DESC
        """, (max_attempts,)).fetchall()

    def needs_fetch(self, url: str, max_attempts: int = 3) -> bool:
        """whether a linked url is worth a request: not fetched yet, and not given up on"""
        row = self.conn.execute("""
            SELECT
                EXISTS (SELECT 1 FROM auctions WHERE fetched_at IS NOT NULL AND (url = :url
                        OR listing_id IN (SELECT related_listing_id FROM listing_links WHERE related_url = :url))) AS fetched,
                EXISTS (SELECT 1 FROM auctions WHERE url = :url AND fetch_attempts >= :max) AS gave_up,
                COALESCE((SELECT MAX(follow_attempts) FROM listing_links WHERE related_url = :url), 0) >= :max AS gave_up_following
        """, {'url': url, 'max': max_attempts}).fetchone()
        return not (row['fetched'] or row['gave_up'] or row['gave_up_following'])

    def listing_ids_where(self, where: str) -> List[int]:
        """listing ids matching a sql condition on auctions, for scoped re-fetches and reparses"""
        return [r[0] for r in self.conn.execute(f"SELECT listing_id FROM auctions WHERE {where}")]

    def mark_stale(self, listing_ids: Iterable[int], fetched_before: Optional[int] = None) -> int:
        """queue fetched listings for a re-fetch without hiding them: parser_version goes to 0, fetched_at stays"""
        with self.conn:
            return self.conn.execute("""
                UPDATE auctions SET parser_version = 0
                WHERE fetched_at IS NOT NULL AND fetched_at < :before AND COALESCE(parser_version, -1) != 0
                  AND listing_id IN (SELECT value FROM json_each(:ids))
            """, {'ids': json.dumps(list(listing_ids)), 'before': fetched_before or 2 ** 62}).rowcount

    def rebuild_vehicles(self) -> dict:
        """group auctions into vehicles: same vin (or make + short chassis), or linked by bat history"""
        rows = self.conn.execute("""
            SELECT listing_id, make, chassis, vin, vehicle_id FROM auctions WHERE fetched_at IS NOT NULL
        """).fetchall()
        links = self.conn.execute("""
            SELECT l.listing_id, l.related_listing_id FROM listing_links l
            JOIN auctions a ON a.listing_id = l.related_listing_id AND a.fetched_at IS NOT NULL
        """).fetchall()

        parent = {r['listing_id']: r['listing_id'] for r in rows}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                # the smallest listing id in a group becomes its vehicle id
                parent[max(ra, rb)] = min(ra, rb)

        identified = set()
        first_by_key = {}
        for r in rows:
            key = vehicle_key(r['vin'], r['chassis'], r['make'])
            if not key:
                continue
            identified.add(r['listing_id'])
            if key in first_by_key:
                union(first_by_key[key], r['listing_id'])
            else:
                first_by_key[key] = r['listing_id']

        for link in links:
            if link['listing_id'] in parent and link['related_listing_id'] in parent:
                union(link['listing_id'], link['related_listing_id'])
                identified.update((link['listing_id'], link['related_listing_id']))

        updates = []
        groups = defaultdict(list)
        for r in rows:
            vehicle_id = find(r['listing_id']) if r['listing_id'] in identified else None
            if vehicle_id is not None:
                groups[vehicle_id].append(r)
            if vehicle_id != r['vehicle_id']:
                updates.append((vehicle_id, r['listing_id']))

        with self.conn:
            self.conn.executemany("UPDATE auctions SET vehicle_id = ? WHERE listing_id = ?", updates)

        return {
            'vehicles': len(groups),
            'repeat_vehicles': sum(1 for g in groups.values() if len(g) > 1),
            # history links can join listings whose chassis was typed differently
            'vin_conflicts': sum(1 for g in groups.values() if len({r['vin'] for r in g if r['vin']}) > 1),
            'updated': len(updates)
        }

    def find_vehicle_id(self, ref: str) -> Optional[int]:
        row = self.conn.execute("""
            SELECT vehicle_id FROM auctions
            WHERE vehicle_id IS NOT NULL
              AND (vin = UPPER(:ref) OR chassis = UPPER(:ref) OR url IN (:ref, :ref || '/')
                   OR CAST(listing_id AS TEXT) = :ref)
            LIMIT 1
        """, {'ref': ref.strip()}).fetchone()
        return row['vehicle_id'] if row else None

    def known_listing_ids(self, listing_ids: List[int]) -> set:
        if not listing_ids:
            return set()
        placeholders = ','.join('?' * len(listing_ids))
        rows = self.conn.execute(
            f"SELECT listing_id FROM auctions WHERE listing_id IN ({placeholders})", listing_ids
        ).fetchall()
        return {r['listing_id'] for r in rows}

    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row['value'] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("""
                INSERT INTO meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (key, value))

    def delete_meta(self, key: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM meta WHERE key = ?", (key,))

    def query(self, sql: str, params=()) -> List[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def _migrate(self) -> None:
        version = int(self.get_meta('schema_version') or 1)
        if version < 2 and self._url_is_unique():
            self._rebuild_auctions()
        if version < SCHEMA_VERSION:
            self.set_meta('schema_version', str(SCHEMA_VERSION))

    def _url_is_unique(self) -> bool:
        for index in self.conn.execute("PRAGMA index_list(auctions)").fetchall():
            columns = [r['name'] for r in self.conn.execute(f"PRAGMA index_info('{index['name']}')")]
            if index['unique'] and columns == ['url']:
                return True
        return False

    def _rebuild_auctions(self) -> None:
        """sqlite can't drop a constraint in place: copy auctions into a table without it"""
        old_columns = [r['name'] for r in self.conn.execute("PRAGMA table_info(auctions)")]
        rebuilt = AUCTIONS_TABLE.replace('CREATE TABLE IF NOT EXISTS auctions', 'CREATE TABLE auctions_rebuilt')
        self.conn.execute(rebuilt)
        new_columns = {r['name'] for r in self.conn.execute("PRAGMA table_info(auctions_rebuilt)")}
        self.conn.execute("DROP TABLE auctions_rebuilt")
        columns = ', '.join(c for c in old_columns if c in new_columns)

        views = ''.join(f"DROP VIEW IF EXISTS {name};" for name in VIEWS)
        # foreign keys from bids and listing_links stay pointed at "auctions" across the swap
        self.conn.executescript(f"""
            PRAGMA foreign_keys=OFF;
            BEGIN;
            {views}
            {rebuilt};
            INSERT INTO auctions_rebuilt ({columns}) SELECT {columns} FROM auctions;
            DROP TABLE auctions;
            ALTER TABLE auctions_rebuilt RENAME TO auctions;
            COMMIT;
            PRAGMA foreign_keys=ON;
        """)

    def _add_missing_columns(self) -> None:
        for table, column, column_type in ADDED_COLUMNS:
            existing = {r['name'] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

    def _upsert_member(self, member: Member) -> None:
        self.conn.execute("""
            INSERT INTO members (slug, display_name, user_id) VALUES (?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                display_name = COALESCE(excluded.display_name, members.display_name),
                user_id = COALESCE(excluded.user_id, members.user_id)
        """, (member.slug, member.display_name, member.user_id))
