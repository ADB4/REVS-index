import re
from typing import Optional
from bs4 import BeautifulSoup
from extractors.base_extractor import BaseExtractor


class TransmissionExtractor(BaseExtractor):
    
    def extract(self, soup: BeautifulSoup, driver=None, context: dict = None) -> Optional[str]:
        listing_details = context.get('listing_details', []) if context else []
        
        for rule in self.rules:
            if rule['type'] == 'listing_detail':
                transmission = self._extract_from_details(listing_details, rule)
                if transmission:
                    return transmission
        
        return None
    
    def _extract_from_details(self, details: list, rule: dict) -> Optional[str]:
        for detail in details:
            normalized_text = detail.replace('‑', '-').replace('–', '-').replace('—', '-')
            for pattern in rule['patterns']:
                match = re.search(pattern, normalized_text, re.I)
                if match:
                    return match.group(0).strip()
        return None
