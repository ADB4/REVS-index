from abc import ABC, abstractmethod
from typing import List
from core.models.listing import Listing


class StorageInterface(ABC):
    
    @abstractmethod
    def save(self, listings: List[Listing]) -> None:
        pass
    
    @abstractmethod
    def save_incremental(self, listing: Listing) -> None:
        pass
    
    @abstractmethod
    def load(self) -> List[Listing]:
        pass
    
    @abstractmethod
    def exists(self, lot_number: str) -> bool:
        pass
