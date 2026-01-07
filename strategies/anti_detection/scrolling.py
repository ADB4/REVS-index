import time
import random


class ScrollStrategy:
    
    @staticmethod
    def human_scroll(driver, target_position=None):
        current_position = driver.execute_script("return window.pageYOffset;")
        
        if target_position is None:
            viewport_height = driver.execute_script("return window.innerHeight;")
            scroll_amount = random.randint(int(viewport_height * 0.4), int(viewport_height * 0.9))
            target_position = current_position + scroll_amount
        
        distance = target_position - current_position
        steps = random.randint(8, 15)
        
        for i in range(steps):
            progress = (i + 1) / steps
            ease = progress * progress * (3.0 - 2.0 * progress)
            scroll_to = int(current_position + (distance * ease))
            driver.execute_script(f"window.scrollTo(0, {scroll_to});")
            time.sleep(random.uniform(0.02, 0.05))
        
        if random.random() < 0.15:
            back_scroll = random.randint(50, 150)
            current = driver.execute_script("return window.pageYOffset;")
            driver.execute_script(f"window.scrollTo(0, {current - back_scroll});")
            time.sleep(random.uniform(0.3, 0.7))
            driver.execute_script(f"window.scrollTo(0, {current});")
            time.sleep(random.uniform(0.2, 0.4))
    
    @staticmethod
    def scroll_to_bottom_naturally(driver):
        total_height = driver.execute_script("return document.body.scrollHeight")
        viewport_height = driver.execute_script("return window.innerHeight;")
        current_position = driver.execute_script("return window.pageYOffset;")
        
        while current_position < total_height - viewport_height:
            scroll_amount = random.randint(300, 800)
            target = min(current_position + scroll_amount, total_height)
            
            ScrollStrategy.human_scroll(driver, target)
            
            if random.random() < 0.2:
                time.sleep(random.uniform(1.5, 3.0))
            else:
                time.sleep(random.uniform(0.3, 0.8))
            
            current_position = driver.execute_script("return window.pageYOffset;")
            total_height = driver.execute_script("return document.body.scrollHeight")
