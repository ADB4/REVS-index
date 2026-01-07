import time
import random


class DelayStrategy:
    
    def __init__(self, min_delay: float = 1.0):
        self.min_delay = min_delay
        self.action_count = 0
        self.last_action_time = time.time()
    
    def get_delay(self) -> float:
        self.action_count += 1
        base_delay = random.uniform(1.5, 3.0)
        
        if self.action_count % random.randint(10, 15) == 0:
            return random.uniform(8, 15)
        
        fatigue_factor = 1 + (self.action_count * 0.005)
        
        if random.random() < 0.15:
            delay = random.uniform(0.5, 1.0)
        else:
            delay = base_delay * fatigue_factor
        
        return max(delay, self.min_delay)
    
    def get_page_load_delay(self) -> float:
        return random.uniform(2, 3.5) + self.get_delay() * 0.3
    
    def get_navigation_delay(self) -> float:
        return self.get_delay()
    
    def wait(self) -> None:
        time.sleep(self.get_delay())
