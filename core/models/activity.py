from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class Member:
    slug: str
    display_name: Optional[str] = None
    user_id: Optional[int] = None


@dataclass
class AuctionSummary:
    listing_id: int
    url: str
    title: str
    result: str
    high_bid: Optional[int] = None
    currency: Optional[str] = None
    end_ts: Optional[int] = None
    year: Optional[int] = None
    country_code: Optional[str] = None
    no_reserve: bool = False
    premium: bool = False


@dataclass
class Bid:
    bid_id: int
    bidder: Member
    amount: int
    ts: int


@dataclass
class HistoryLink:
    url: str
    end_ts: Optional[int] = None
    summary: Optional[str] = None


@dataclass
class AuctionDetail:
    listing_id: int
    url: str
    title: Optional[str] = None
    result: str = 'unknown'
    high_bid: Optional[int] = None
    currency: Optional[str] = None
    end_ts: Optional[int] = None

    seller: Optional[Member] = None
    seller_type: Optional[str] = None
    lot_number: Optional[str] = None
    location: Optional[str] = None
    country: Optional[str] = None

    make: Optional[str] = None
    model: Optional[str] = None
    model_slug: Optional[str] = None
    era: Optional[str] = None
    origin: Optional[str] = None
    category: Optional[str] = None

    chassis: Optional[str] = None
    # the text after "Chassis:" as the page shows it
    chassis_raw: Optional[str] = None
    vin: Optional[str] = None
    history: List[HistoryLink] = field(default_factory=list)

    high_bidder: Optional[Member] = None
    winner: Optional[Member] = None
    n_comments: int = 0
    bids_reported: Optional[int] = None
    bids: List[Bid] = field(default_factory=list)
    # oddities worth a line in the fetch log that don't stop the save
    notes: List[str] = field(default_factory=list)
