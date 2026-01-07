import yaml
import re
import time
import random
from typing import List, Dict
from bs4 import BeautifulSoup

from sites.base_site import BaseSite
from core.models.listing import Listing
from core.models.scrape_config import ScrapeConfig
from extractors.field_extractors.vin_extractor import VINExtractor
from extractors.field_extractors.engine_extractor import EngineExtractor
from extractors.field_extractors.transmission_extractor import TransmissionExtractor
from extractors.field_extractors.mileage_extractor import MileageExtractor
from extractors.field_extractors.color_extractor import ColorExtractor
from extractors.field_extractors.price_extractor import PriceExtractor


class BringATrailerSite(BaseSite):
    
    def __init__(self, config_path: str):
        with open(config_path) as f:
            self.config = yaml.safe_load(f)
        
        self.base_url = self.config['site']['base_url']
        self.selectors = self.config['selectors']
        self.extraction_rules = self.config['extraction_rules']
        
        self.vin_extractor = VINExtractor(self.extraction_rules['vin'])
        self.engine_extractor = EngineExtractor(self.extraction_rules['engine'])
        self.transmission_extractor = TransmissionExtractor(self.extraction_rules['transmission'])
        self.mileage_extractor = MileageExtractor(self.extraction_rules['mileage'])
        self.color_extractor = ColorExtractor([self.extraction_rules['colors']])
        self.price_extractor = PriceExtractor([])
    
    def navigate_to_category(self, browser, slug: str) -> None:
        url = f"{self.base_url}/{slug}/"
        browser.navigate(url)
    
    def apply_sort(self, browser, sort_type: str) -> bool:
        try:
            dropdown = browser.find_element(self.selectors['listing_page']['sort_dropdown'])
            if not dropdown:
                return False
            
            title = dropdown.find_element_by_class_name('dropdown-title')
            browser.click(self.selectors['listing_page']['sort_dropdown_title'], human_like=True)
            time.sleep(random.uniform(0.5, 1.0))
            
            options = dropdown.find_elements_by_class_name('dropdown-option')
            for option in options:
                if sort_type in option.text:
                    from selenium.webdriver.common.action_chains import ActionChains
                    actions = ActionChains(browser.driver)
                    actions.move_to_element(option).click().perform()
                    time.sleep(random.uniform(1.0, 2.0))
                    return True
        except Exception as e:
            print(f"could not sort by {sort_type}: {e}")
            return False
        
        return False
    
    def get_listing_urls(self, browser, config: ScrapeConfig) -> List[str]:
        self._load_all_listings(browser, config.max_clicks)
        
        html = browser.get_html()
        soup = BeautifulSoup(html, 'html.parser')
        
        cards = soup.select(self.selectors['listing_page']['cards'])
        
        listings = []
        for card in cards:
            url = card.get('href')
            if url and not url.startswith('http'):
                url = f"{self.base_url}{url}"
            
            if url:
                listings.append(url)
        
        return listings[:config.max_listings]
    
    def _load_all_listings(self, browser, max_clicks: int):
        clicks = 0
        consecutive_failures = 0
        
        while clicks < max_clicks:
            try:
                total_height = browser.execute_script("return document.body.scrollHeight")
                offset = random.randint(-50, 0)
                browser.execute_script(f"window.scrollTo(0, {total_height + offset});")
                
                time.sleep(random.uniform(1.5, 3.0))
                
                listings_before = len(browser.find_elements(self.selectors['listing_page']['cards']))
                
                result = browser.execute_script(self.config['javascript']['load_more_listings'])
                
                if 'done' in result:
                    print(f"all listings loaded: {result['reason']}")
                    break
                
                if 'error' in result:
                    consecutive_failures += 1
                    if consecutive_failures >= 2:
                        break
                    continue
                
                if 'success' in result:
                    clicks += 1
                    wait_time = random.uniform(3, 5)
                    time.sleep(wait_time)
                    
                    for wait_attempt in range(15):
                        listings_after = len(browser.find_elements(self.selectors['listing_page']['cards']))
                        
                        if listings_after > listings_before:
                            new_count = listings_after - listings_before
                            print(f"  click {clicks}: +{new_count} listings (total: {listings_after})")
                            consecutive_failures = 0
                            break
                        
                        time.sleep(random.uniform(1, 2))
                    else:
                        consecutive_failures += 1
                        if consecutive_failures >= 2:
                            break
                
            except Exception as e:
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    break
        
        final_count = len(browser.find_elements(self.selectors['listing_page']['cards']))
        print(f"\nloading complete: {final_count} listings found\n")
    
    def extract_listing(self, browser, url: str, config: ScrapeConfig, sale_price: int = None) -> Listing:
        browser.navigate(url)
        
        if random.random() < 0.7:
            viewport_height = browser.execute_script("return window.innerHeight;")
            scroll_distance = random.randint(int(viewport_height * 0.3), int(viewport_height * 0.7))
            from strategies.anti_detection.scrolling import ScrollStrategy
            ScrollStrategy.human_scroll(browser.driver, scroll_distance)
            time.sleep(random.uniform(0.5, 1.2))
        
        html = browser.get_html()
        soup = BeautifulSoup(html, 'html.parser')
        
        title = self._extract_title(soup)
        year = self._extract_year(title)
        
        country = self._extract_country(soup)
        convertible = self._is_convertible(soup)
        location, seller, seller_type, lot_number = self._extract_essentials(soup)
        
        listing_details = self._extract_listing_details(soup)
        context = {
            'listing_details': listing_details,
            'title': title
        }
        
        vin = self.vin_extractor.extract(soup, browser.driver, context)
        engine = self.engine_extractor.extract(soup, browser.driver, context)
        transmission = self.transmission_extractor.extract(soup, browser.driver, context)
        mileage = self.mileage_extractor.extract(soup, browser.driver, context)
        exterior_color, interior_color = self.color_extractor.extract(soup, browser.driver, context)
        
        number_of_bids = self._extract_bids(soup)
        
        high_bidder = self._extract_high_bidder(browser, sale_price)
        
        excerpt = self._extract_excerpt(soup)
        
        variant = self._extract_variant(title, config)
        
        listing = Listing(
            url=url,
            source=self.config['site']['source_name'],
            title=title,
            lot_number=lot_number,
            seller=seller,
            seller_type=seller_type,
            result='Sold' if sale_price else 'Reserve Not Met',
            high_bidder=high_bidder,
            price=sale_price,
            sale_date=None,
            number_of_bids=number_of_bids,
            vin=vin,
            year=year,
            make=config.make,
            model=config.model_full,
            variant=variant,
            convertible=convertible,
            engine=engine,
            transmission=transmission,
            exterior_color=exterior_color,
            interior_color=interior_color,
            mileage=mileage,
            location=location,
            country=country,
            listing_details=listing_details,
            excerpt=excerpt
        )
        
        return listing
    
    def _extract_title(self, soup: BeautifulSoup) -> str:
        title_elem = soup.select_one('h1.post-title')
        return title_elem.get_text(strip=True) if title_elem else ''
    
    def _extract_year(self, title: str) -> int:
        year_match = re.search(r'\b(19|20)\d{2}\b', title)
        return int(year_match.group()) if year_match else None
    
    def _extract_country(self, soup: BeautifulSoup) -> str:
        country_elem = soup.select_one(self.selectors['detail_page']['country'])
        return country_elem.get_text(strip=True) if country_elem else None
    
    def _is_convertible(self, soup: BeautifulSoup) -> bool:
        buttons = soup.select(self.selectors['detail_page']['category_buttons'])
        for button in buttons:
            if 'Convertible' in button.get_text():
                return True
        return False
    
    def _extract_essentials(self, soup: BeautifulSoup):
        location = None
        seller = None
        seller_type = None
        lot_number = None
        
        essentials = soup.select_one(self.selectors['detail_page']['essentials'])
        if not essentials:
            return location, seller, seller_type, lot_number
        
        strongs = essentials.select('strong')
        for strong in strongs:
            label = strong.get_text(strip=True)
            parent = strong.parent
            
            if label == 'Location':
                link = parent.select_one('a[href*="google.com/maps"]')
                if link:
                    location = link.get_text(strip=True)
            
            elif label == 'Seller':
                link = parent.select_one('a[href*="/member/"]')
                if link:
                    seller = link.get_text(strip=True)
            
            elif label == 'Private Party or Dealer':
                parent_text = parent.get_text(strip=True)
                if ':' in parent_text:
                    value = parent_text.split(':', 1)[1].strip()
                    if value in ['Private Party', 'Dealer']:
                        seller_type = value
            
            elif label == 'Lot':
                parent_text = parent.get_text(strip=True)
                lot_match = re.search(r'#?(\d+)', parent_text)
                if lot_match:
                    lot_number = lot_match.group(1)
        
        return location, seller, seller_type, lot_number
    
    def _extract_listing_details(self, soup: BeautifulSoup) -> List[str]:
        essentials = soup.select_one(self.selectors['detail_page']['essentials'])
        if not essentials:
            return []
        
        ul = essentials.select_one('ul')
        if not ul:
            return []
        
        li_elements = ul.select('li')
        details = []
        for li in li_elements:
            text = li.get_text(strip=True)
            if text:
                details.append(text)
        
        return details
    
    def _extract_bids(self, soup: BeautifulSoup) -> int:
        listing_stats = soup.select_one(self.selectors['detail_page']['listing_stats'])
        if not listing_stats:
            return None
        
        rows = listing_stats.select(self.selectors['detail_page']['stat_row'])
        for row in rows:
            label = row.select_one(self.selectors['detail_page']['stat_label'])
            value = row.select_one(self.selectors['detail_page']['stat_value'])
            
            if label and 'bids' in label.get_text().lower():
                bids_text = value.get_text(strip=True)
                bids_match = re.search(r'(\d+)', bids_text)
                if bids_match:
                    return int(bids_match.group(1))
        
        return None
    
    def _extract_high_bidder(self, browser, sale_price: int) -> str:
        max_clicks = 10
        clicks = 0
        
        while clicks < max_clicks:
            bid_links = browser.find_elements('.bid-notification-link')
            
            if bid_links and sale_price:
                for bid_link in reversed(bid_links):
                    try:
                        parent = bid_link.find_element_by_xpath("../..")
                        comment_text = parent.text
                        
                        bid_match = re.search(r'USD\s+\$([0-9,]+)', comment_text, re.I)
                        if bid_match:
                            bid_amount = int(bid_match.group(1).replace(',', ''))
                            if bid_amount == sale_price:
                                return bid_link.text.strip()
                    except:
                        continue
            elif bid_links and not sale_price:
                return bid_links[-1].text.strip()
            
            show_more = browser.find_element('#comments-load-button')
            if show_more and show_more.is_displayed():
                browser.scroll_to_element('#comments-load-button', natural=True)
                time.sleep(random.uniform(0.3, 0.7))
                browser.click('#comments-load-button', human_like=True)
                clicks += 1
                time.sleep(random.uniform(2, 3.5))
            else:
                break
        
        return None
    
    def _extract_excerpt(self, soup: BeautifulSoup) -> List[str]:
        excerpt_elem = soup.select_one(self.selectors['detail_page']['post_excerpt'])
        if not excerpt_elem:
            return []
        
        paragraphs = excerpt_elem.select(self.selectors['detail_page']['excerpt_paragraphs'])
        excerpt_paragraphs = []
        for p in paragraphs:
            text = p.get_text(strip=True)
            if text:
                excerpt_paragraphs.append(text)
        
        return excerpt_paragraphs
    
    def _extract_variant(self, title: str, config: ScrapeConfig) -> str:
        try:
            title_upper = title.upper()
            make_upper = config.make.upper()
            model_short_upper = config.model_short.upper()
            
            make_index = title_upper.find(make_upper)
            if make_index == -1:
                return "Standard"
            
            after_make = title[make_index + len(config.make):].strip()
            model_index = after_make.upper().find(model_short_upper)
            
            if model_index == -1:
                return "Standard"
            
            after_model = after_make[model_index + len(config.model_short):].strip()
            
            if not after_model:
                return "Standard"
            
            transmission_match = re.search(r'\d+-Speed', after_model, re.I)
            if transmission_match:
                variant_end = transmission_match.start()
                variant = after_model[:variant_end].strip()
            else:
                variant = after_model.strip()
            
            if not variant:
                return "Standard"
            
            variant_parts = variant.split()
            if variant_parts:
                first_word = variant_parts[0]
                common_words = ['for', 'with', 'in', 'at', 'by', 'from', 'on', 'and', 'the']
                if first_word.lower() in common_words:
                    return "Standard"
            
            return variant
            
        except Exception as e:
            return "Standard"
