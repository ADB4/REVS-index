import time
import random
from selenium.webdriver.common.action_chains import ActionChains


class ClickStrategy:
    
    @staticmethod
    def human_click(driver, element):
        try:
            actions = ActionChains(driver)
            size = element.size
            offset_x = random.randint(-size['width']//4, size['width']//4)
            offset_y = random.randint(-size['height']//4, size['height']//4)
            actions.move_to_element_with_offset(element, offset_x, offset_y)
            actions.pause(random.uniform(0.1, 0.3))
            actions.click()
            actions.perform()
        except:
            element.click()
