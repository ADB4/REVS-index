from abc import ABC, abstractmethod
from typing import Optional, List, Any


class BaseBrowser(ABC):
    
    @abstractmethod
    def navigate(self, url: str) -> None:
        pass
    
    @abstractmethod
    def find_element(self, selector: str, by: str = 'css') -> Optional[Any]:
        pass
    
    @abstractmethod
    def find_elements(self, selector: str, by: str = 'css') -> List[Any]:
        pass
    
    @abstractmethod
    def click(self, selector: str, human_like: bool = True) -> None:
        pass
    
    @abstractmethod
    def scroll_to_bottom(self, natural: bool = True) -> None:
        pass
    
    @abstractmethod
    def scroll_to_element(self, selector: str, natural: bool = True) -> None:
        pass
    
    @abstractmethod
    def get_html(self) -> str:
        pass
    
    @abstractmethod
    def execute_script(self, script: str) -> Any:
        pass
    
    @abstractmethod
    def wait_for_element(self, selector: str, timeout: int = 10) -> bool:
        pass
    
    @abstractmethod
    def close(self) -> None:
        pass
    
    @abstractmethod
    def back(self) -> None:
        pass
