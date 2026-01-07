from abc import ABC, abstractmethod
from typing import List, Dict
from core.models.listing import Listing
from core.models.scrape_config import ScrapeConfig


class BaseSite(ABC):
    
    @abstractmethod
    def navigate_to_category(self, browser, slug: str) -> None:
        pass
    
    @abstractmethod
    def get_listing_urls(self, browser, config: ScrapeConfig) -> List[str]:
        pass
    
    @abstractmethod
    def extract_listing(self, browser, url: str, config: ScrapeConfig, sale_price: int = None) -> Listing:
        pass
    
    @abstractmethod
    def apply_sort(self, browser, sort_type: str) -> bool:
        pass
