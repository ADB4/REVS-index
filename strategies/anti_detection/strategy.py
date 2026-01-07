from .delays import DelayStrategy
from .user_agent import UserAgentStrategy
from .scrolling import ScrollStrategy
from .clicks import ClickStrategy


class AntiDetectionStrategy:
    
    def __init__(self):
        self.delay_strategy = DelayStrategy()
        self.user_agent_strategy = UserAgentStrategy()
        self.scroll_strategy = ScrollStrategy()
        self.click_strategy = ClickStrategy()
        
        self.user_agent = self.user_agent_strategy.get_random_user_agent()
        self.viewport = self.user_agent_strategy.get_random_viewport()
    
    def apply_to_driver(self, driver):
        driver.execute_cdp_cmd('Network.setUserAgentOverride', {
            "userAgent": self.user_agent
        })
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    
    def get_page_load_delay(self) -> float:
        return self.delay_strategy.get_page_load_delay()
    
    def get_navigation_delay(self) -> float:
        return self.delay_strategy.get_navigation_delay()
    
    def get_delay(self) -> float:
        return self.delay_strategy.get_delay()
    
    def human_click(self, driver, element):
        self.click_strategy.human_click(driver, element)
    
    def human_scroll(self, driver, target_position=None):
        self.scroll_strategy.human_scroll(driver, target_position)
    
    def scroll_to_bottom_naturally(self, driver):
        self.scroll_strategy.scroll_to_bottom_naturally(driver)
