from typing import Optional, List, Any
import time

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

from .base_browser import BaseBrowser


class SeleniumBrowser(BaseBrowser):
    
    def __init__(self, anti_detection_strategy=None, headless: bool = False):
        self.anti_detection = anti_detection_strategy
        self.driver = self._setup_driver(headless)
        
        if self.anti_detection:
            self.anti_detection.apply_to_driver(self.driver)
    
    def _setup_driver(self, headless: bool):
        chrome_options = Options()
        
        prefs = {"profile.default_content_setting_values.notifications": 2}
        chrome_options.add_experimental_option("prefs", prefs)
        
        if headless:
            chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-notifications')
        
        service = Service(ChromeDriverManager().install())
        return webdriver.Chrome(service=service, options=chrome_options)
    
    def navigate(self, url: str) -> None:
        self.driver.get(url)
        if self.anti_detection:
            time.sleep(self.anti_detection.get_page_load_delay())
    
    def find_element(self, selector: str, by: str = 'css') -> Optional[Any]:
        try:
            by_type = By.CSS_SELECTOR if by == 'css' else By.CLASS_NAME
            return self.driver.find_element(by_type, selector)
        except:
            return None
    
    def find_elements(self, selector: str, by: str = 'css') -> List[Any]:
        try:
            by_type = By.CSS_SELECTOR if by == 'css' else By.CLASS_NAME
            return self.driver.find_elements(by_type, selector)
        except:
            return []
    
    def click(self, selector: str, human_like: bool = True) -> None:
        element = self.find_element(selector)
        if not element:
            return
        
        if human_like and self.anti_detection:
            self.anti_detection.human_click(self.driver, element)
        else:
            element.click()
    
    def scroll_to_bottom(self, natural: bool = True) -> None:
        if natural and self.anti_detection:
            self.anti_detection.scroll_to_bottom_naturally(self.driver)
        else:
            total_height = self.driver.execute_script("return document.body.scrollHeight")
            self.driver.execute_script(f"window.scrollTo(0, {total_height});")
    
    def scroll_to_element(self, selector: str, natural: bool = True) -> None:
        element = self.find_element(selector)
        if not element:
            return
        
        if natural and self.anti_detection:
            element_y = element.location['y']
            viewport_height = self.driver.execute_script("return window.innerHeight;")
            scroll_target = max(0, element_y - viewport_height // 2)
            self.anti_detection.human_scroll(self.driver, scroll_target)
        else:
            self.driver.execute_script("arguments[0].scrollIntoView(true);", element)
    
    def get_html(self) -> str:
        return self.driver.page_source
    
    def execute_script(self, script: str) -> Any:
        return self.driver.execute_script(script)
    
    def wait_for_element(self, selector: str, timeout: int = 10) -> bool:
        try:
            WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
            return True
        except:
            return False
    
    def close(self) -> None:
        self.driver.quit()
    
    def back(self) -> None:
        self.driver.back()
        if self.anti_detection:
            time.sleep(self.anti_detection.get_navigation_delay())
