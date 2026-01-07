import re
from typing import Optional, Tuple
from bs4 import BeautifulSoup
from extractors.base_extractor import BaseExtractor


class ColorExtractor(BaseExtractor):
    
    def extract(self, soup: BeautifulSoup, driver=None, context: dict = None) -> Tuple[Optional[str], Optional[str]]:
        listing_details = context.get('listing_details', []) if context else []
        
        exterior = None
        interior = None
        
        if not self.rules:
            return exterior, interior
        
        color_rules = self.rules[0] if self.rules else {}
        exterior_config = color_rules.get('exterior', {})
        interior_config = color_rules.get('interior', {})
        
        for idx, detail in enumerate(listing_details):
            if not exterior:
                for pattern in exterior_config.get('patterns', []):
                    match = re.search(pattern, detail, re.I)
                    if match:
                        exterior = match.group(1).strip()
                        if exterior_config.get('next_is_interior') and idx + 1 < len(listing_details):
                            interior = listing_details[idx + 1].strip()
                        break
        
        if not interior:
            interior_keywords = interior_config.get('keywords', [])
            skip_keywords = interior_config.get('skip_keywords', [])
            
            for detail in listing_details:
                detail_lower = detail.lower()
                
                if any(keyword in detail_lower for keyword in skip_keywords):
                    continue
                
                if not detail.strip() or len(detail.split()) > 8:
                    continue
                
                if any(keyword in detail_lower for keyword in interior_keywords):
                    interior = detail.strip()
                    break
        
        return exterior, interior
