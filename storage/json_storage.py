import json
import os
from typing import List, Set
from core.models.listing import Listing
from storage.storage_interface import StorageInterface


class JSONStorage(StorageInterface):
    
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.existing_lot_numbers: Set[str] = set()
        self._load_existing_lot_numbers()
    
    def _load_existing_lot_numbers(self):
        if not os.path.exists(self.filepath):
            return
        
        try:
            with open(self.filepath, 'r') as f:
                data = json.load(f)
                for item in data:
                    if 'lot_number' in item and item['lot_number'] != 'N/A':
                        self.existing_lot_numbers.add(item['lot_number'])
        except:
            pass
    
    def save(self, listings: List[Listing]) -> None:
        os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
        
        data = [listing.to_dict() for listing in listings]
        
        with open(self.filepath, 'w') as f:
            json.dump(data, f, indent=4)
    
    def save_incremental(self, listing: Listing) -> None:
        pass
    
    def load(self) -> List[Listing]:
        if not os.path.exists(self.filepath):
            return []
        
        with open(self.filepath, 'r') as f:
            data = json.load(f)
        
        return [Listing.from_dict(item) for item in data]
    
    def exists(self, lot_number: str) -> bool:
        return lot_number in self.existing_lot_numbers
    
    def get_existing_lot_numbers(self) -> Set[str]:
        return self.existing_lot_numbers
