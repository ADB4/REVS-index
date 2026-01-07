import re
from typing import Optional
from bs4 import BeautifulSoup
from extractors.base_extractor import BaseExtractor


class VINExtractor(BaseExtractor):
    
    VIN_PATTERN = r'[A-HJ-NPR-Z0-9]{17}'
    
    def extract(self, soup: BeautifulSoup, driver=None, context: dict = None) -> Optional[str]:
        for rule in self.rules:
            vin = None
            
            if rule['type'] == 'selector':
                vin = self._extract_from_selector(soup, rule)
            elif rule['type'] == 'comment_search' and driver:
                vin = self._extract_from_comments(driver, rule)
            
            if vin and self._validate_vin(vin):
                return vin
        
        return None
    
    def _extract_from_selector(self, soup: BeautifulSoup, rule: dict) -> Optional[str]:
        elements = soup.select(rule['selector'])
        for elem in elements:
            text = elem.get_text(strip=True)
            if 'regex' in rule:
                match = re.search(rule['regex'], text, re.I)
                if match:
                    return match.group(1)
        return None
    
    def _extract_from_comments(self, driver, rule: dict) -> Optional[str]:
        try:
            elements = driver.find_elements_by_css_selector(rule['selector'])
            for elem in elements:
                text = elem.text
                for pattern in rule['patterns']:
                    match = re.search(pattern, text, re.I)
                    if match:
                        return match.group(1)
        except:
            pass
        return None
    
    def _validate_vin(self, vin: str) -> bool:
        return bool(re.match(f'^{self.VIN_PATTERN}$', vin))
