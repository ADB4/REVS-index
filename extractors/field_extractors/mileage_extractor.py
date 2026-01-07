import re
from typing import Optional
from bs4 import BeautifulSoup
from extractors.base_extractor import BaseExtractor


class MileageExtractor(BaseExtractor):
    
    def extract(self, soup: BeautifulSoup, driver=None, context: dict = None) -> Optional[int]:
        listing_details = context.get('listing_details', []) if context else []
        title = context.get('title', '') if context else ''
        
        for rule in self.rules:
            mileage = None
            
            if rule['type'] == 'listing_detail':
                mileage = self._extract_from_details(listing_details, rule)
            elif rule['type'] == 'title':
                mileage = self._extract_from_title(title, rule)
            elif rule['type'] == 'comment_search' and driver:
                mileage = self._extract_from_comments(driver, rule)
            
            if mileage is not None:
                return mileage
        
        return None
    
    def _extract_from_details(self, details: list, rule: dict) -> Optional[int]:
        for detail in details:
            for pattern in rule['patterns']:
                match = re.search(pattern, detail, re.I)
                if match:
                    value = match.group(1)
                    if 'transform' in rule:
                        return self._apply_transform(value, rule['transform'])
                    return int(value.replace(',', ''))
        return None
    
    def _extract_from_title(self, title: str, rule: dict) -> Optional[int]:
        for pattern in rule['patterns']:
            match = re.search(pattern, title, re.I)
            if match:
                value = match.group(1)
                if 'transform' in rule:
                    return self._apply_transform(value, rule['transform'])
                return int(float(value) * 1000)
        return None
    
    def _extract_from_comments(self, driver, rule: dict) -> Optional[int]:
        try:
            comment_elem = driver.find_element_by_id('comments')
            text = comment_elem.text
            for pattern in rule['patterns']:
                match = re.search(pattern, text, re.I)
                if match:
                    value = match.group(1)
                    return int(value.replace(',', ''))
        except:
            pass
        return None
