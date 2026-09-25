from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Optional, Any
from bs4 import BeautifulSoup


class BaseExtractor(ABC):
    
    def __init__(self, rules: list):
        self.rules = rules
    
    @abstractmethod
    def extract(self, soup: BeautifulSoup, driver=None, context: dict = None) -> Optional[Any]:
        pass
    
    def _apply_transform(self, value: str, transform: str) -> Any:
        # decimal, not float: "32.3k" is exactly 32,300, and a long run of digits can't overflow into inf
        if transform == 'multiply_1000':
            return int(Decimal(value) * 1000)
        elif transform == 'handle_k_miles':
            # "121k" is thousands of miles; "97,800" and "1.5" read as written
            number = value.lower().replace(',', '').strip()
            if number.endswith('k'):
                return int(Decimal(number[:-1].strip()) * 1000)
            return int(Decimal(number))
        elif transform == 'remove_commas':
            return int(value.replace(',', ''))
        return value
