from abc import ABC, abstractmethod
from typing import Optional, Any
from bs4 import BeautifulSoup


class BaseExtractor(ABC):
    
    def __init__(self, rules: list):
        self.rules = rules
    
    @abstractmethod
    def extract(self, soup: BeautifulSoup, driver=None, context: dict = None) -> Optional[Any]:
        pass
    
    def _apply_transform(self, value: str, transform: str) -> Any:
        if transform == 'multiply_1000':
            return int(float(value) * 1000)
        elif transform == 'handle_k_miles':
            if 'k' in value.lower():
                return int(float(value.replace(',', '')) * 1000)
            return int(value.replace(',', ''))
        elif transform == 'remove_commas':
            return int(value.replace(',', ''))
        return value
