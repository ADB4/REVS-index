import re
from typing import Optional, Tuple
from bs4 import BeautifulSoup
from extractors.base_extractor import BaseExtractor


class PriceExtractor(BaseExtractor):
    
    def extract(self, soup: BeautifulSoup, driver=None, context: dict = None) -> Tuple[Optional[int], Optional[str]]:
        results_elem = soup.select_one('.item-results')
        
        if not results_elem:
            return None, None
        
        text = results_elem.get_text(strip=True)
        
        price_match = re.search(r'\$\s?([\d,]+)', text)
        price = int(price_match.group(1).replace(',', '')) if price_match else None
        
        date_match = re.search(r'on\s+(\d{1,2}/\d{1,2}/\d{2,4})', text)
        sale_date = None
        if date_match:
            from datetime import datetime
            try:
                sale_date = datetime.strptime(date_match.group(1), '%m/%d/%y').strftime('%Y-%m-%d')
            except:
                pass
        
        return price, sale_date
