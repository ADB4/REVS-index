from dataclasses import dataclass
from typing import Optional, List


@dataclass
class ScrapeConfig:
    slugs: List[str]
    make: str
    model_full: str
    model_short: str
    
    min_year: Optional[int] = None
    max_year: Optional[int] = None
    max_listings: int = 100
    max_clicks: int = 48
    
    headless: bool = False
    sort_oldest: bool = False
    
    append_file: Optional[str] = None
    fields: Optional[List[str]] = None
    
    def should_skip_year(self, year: int) -> bool:
        if self.min_year and year < self.min_year:
            return True
        if self.max_year and year > self.max_year:
            return True
        return False
