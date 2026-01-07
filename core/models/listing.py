from dataclasses import dataclass, field
from typing import Optional, List
from datetime import date


@dataclass
class Listing:
    url: str
    source: str
    title: str
    
    lot_number: Optional[str] = None
    seller: Optional[str] = None
    seller_type: Optional[str] = None
    result: Optional[str] = None
    high_bidder: Optional[str] = None
    
    price: Optional[int] = None
    sale_date: Optional[str] = None
    number_of_bids: Optional[int] = None
    
    vin: Optional[str] = None
    year: Optional[int] = None
    make: Optional[str] = None
    model: Optional[str] = None
    variant: Optional[str] = None
    
    convertible: bool = False
    engine: Optional[str] = None
    transmission: Optional[str] = None
    exterior_color: Optional[str] = None
    interior_color: Optional[str] = None
    mileage: Optional[int] = None
    location: Optional[str] = None
    country: Optional[str] = None
    
    listing_details: List[str] = field(default_factory=list)
    excerpt: List[str] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return {
            'url': self.url,
            'source': self.source,
            'lot_number': self.lot_number or 'N/A',
            'seller': self.seller or 'N/A',
            'seller_type': self.seller_type or 'N/A',
            'result': self.result or 'N/A',
            'high_bidder': self.high_bidder or 'N/A',
            'price': self.price,
            'sale_date': self.sale_date or 'N/A',
            'number_of_bids': self.number_of_bids,
            'title': self.title or 'N/A',
            'vin': self.vin or 'N/A',
            'year': self.year,
            'make': self.make or 'N/A',
            'model': self.model or 'N/A',
            'variant': self.variant or 'N/A',
            'convertible': self.convertible,
            'engine': self.engine or 'N/A',
            'transmission': self.transmission or 'N/A',
            'exterior_color': self.exterior_color or 'N/A',
            'interior_color': self.interior_color or 'N/A',
            'mileage': self.mileage,
            'location': self.location or 'N/A',
            'listing_details': self.listing_details,
            'excerpt': self.excerpt
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Listing':
        return cls(
            url=data.get('url', ''),
            source=data.get('source', 'unknown'),
            title=data.get('title', ''),
            lot_number=data.get('lot_number'),
            seller=data.get('seller'),
            seller_type=data.get('seller_type'),
            result=data.get('result'),
            high_bidder=data.get('high_bidder'),
            price=data.get('price'),
            sale_date=data.get('sale_date'),
            number_of_bids=data.get('number_of_bids'),
            vin=data.get('vin'),
            year=data.get('year'),
            make=data.get('make'),
            model=data.get('model'),
            variant=data.get('variant'),
            convertible=data.get('convertible', False),
            engine=data.get('engine'),
            transmission=data.get('transmission'),
            exterior_color=data.get('exterior_color'),
            interior_color=data.get('interior_color'),
            mileage=data.get('mileage'),
            location=data.get('location'),
            country=data.get('country'),
            listing_details=data.get('listing_details', []),
            excerpt=data.get('excerpt', [])
        )
