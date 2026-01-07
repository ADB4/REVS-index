from typing import List, Optional
from core.models.listing import Listing
from core.models.scrape_config import ScrapeConfig
from core.browser.base_browser import BaseBrowser
from sites.base_site import BaseSite
from storage.storage_interface import StorageInterface


class ScrapingPipeline:
    
    def __init__(
        self,
        browser: BaseBrowser,
        site: BaseSite,
        storage: StorageInterface,
        append_storage: Optional[StorageInterface] = None
    ):
        self.browser = browser
        self.site = site
        self.storage = storage
        self.append_storage = append_storage
    
    def run(self, config: ScrapeConfig) -> List[Listing]:
        all_listings = []
        
        for idx, slug in enumerate(config.slugs, 1):
            print(f"\nscraping slug {idx}/{len(config.slugs)}: {slug}\n")
            
            self.site.navigate_to_category(self.browser, slug)
            
            self.browser.wait_for_element('.listing-card', timeout=15)
            
            if config.sort_oldest:
                print("sorting by oldest...")
                if self.site.apply_sort(self.browser, "Oldest"):
                    print("sorted by oldest")
                else:
                    print("could not sort")
            
            initial_count = len(self.browser.find_elements('.listing-card'))
            print(f"initial page loaded: {initial_count} listings\n")
            
            print("loading all listings...")
            print("=" * 70)
            
            urls = self.site.get_listing_urls(self.browser, config)
            
            print(f"\nparsing listing cards...")
            
            listings = self._scrape_listings(urls, config)
            all_listings.extend(listings)
            
            if self.append_storage and listings:
                break
        
        unique_listings = self._deduplicate(all_listings)
        
        print(f"\ncombined {len(all_listings)} listings from {len(config.slugs)} slug(s)")
        if len(all_listings) != len(unique_listings):
            print(f"removed {len(all_listings) - len(unique_listings)} duplicates")
        print(f"final count: {len(unique_listings)} unique listings\n")
        
        return unique_listings
    
    def _scrape_listings(self, urls: List[str], config: ScrapeConfig) -> List[Listing]:
        listings = []
        skipped = 0
        
        missing_fields = {
            'vin': 0, 'lot_number': 0, 'seller': 0, 'seller_type': 0,
            'high_bidder': 0, 'engine': 0, 'transmission': 0,
            'exterior_color': 0, 'interior_color': 0, 'mileage': 0,
            'location': 0, 'number_of_bids': 0, 'listing_details': 0
        }
        
        for i, url in enumerate(urls, 1):
            if i % 10 == 0 or i == 1:
                print(f"  scraping details: {i}/{len(urls)}")
            
            try:
                listing = self.site.extract_listing(self.browser, url, config, sale_price=None)
                
                if self.append_storage and listing.lot_number and listing.lot_number != 'N/A':
                    if self.append_storage.exists(listing.lot_number):
                        print(f"\n  found duplicate lot #{listing.lot_number}, stopping scrape")
                        print(f"  scraped {len(listings)} new listings before duplicate")
                        self.browser.back()
                        return listings
                
                if listing.country and listing.country != 'USA':
                    skipped += 1
                    print(f"    skipped (non-USA): {listing.title[:50]}... ({listing.country})")
                    self.browser.back()
                    continue
                
                if 'modified' in listing.title.lower():
                    skipped += 1
                    self.browser.back()
                    continue
                
                if config.should_skip_year(listing.year):
                    skipped += 1
                    self.browser.back()
                    continue
                
                if not listing.vin or listing.vin == 'N/A':
                    skipped += 1
                    print(f"    skipped (no VIN): {listing.title[:50]}...")
                    self.browser.back()
                    continue
                
                for field in missing_fields.keys():
                    value = getattr(listing, field, None)
                    if value is None or value == 'N/A' or (isinstance(value, list) and len(value) == 0):
                        missing_fields[field] += 1
                
                if config.fields:
                    listing = self._filter_fields(listing, config.fields)
                
                listings.append(listing)
                self.browser.back()
                
            except Exception as e:
                print(f"    error scraping {url}: {e}")
                try:
                    self.browser.back()
                except:
                    pass
        
        print(f"  completed detail scraping for {len(urls)} listings")
        print(f"  skipped {skipped} listings (no VIN, non-USA, modified, or outside year range)")
        print(f"  kept {len(listings)} car listings")
        
        if len(listings) > 0:
            self._print_missing_fields_summary(listings, missing_fields)
        
        return listings
    
    def _filter_fields(self, listing: Listing, fields: List[str]) -> Listing:
        filtered_dict = {}
        listing_dict = listing.to_dict()
        for field in fields:
            if field in listing_dict:
                filtered_dict[field] = listing_dict[field]
        return Listing.from_dict(filtered_dict)
    
    def _deduplicate(self, listings: List[Listing]) -> List[Listing]:
        seen_urls = set()
        unique = []
        
        for listing in listings:
            if listing.url not in seen_urls:
                seen_urls.add(listing.url)
                unique.append(listing)
        
        return unique
    
    def _print_missing_fields_summary(self, listings: List[Listing], missing_fields: dict):
        print(f"\n{'=' * 70}")
        print("missing fields summary")
        print(f"{'=' * 70}")
        print(f"total listings scraped: {len(listings)}")
        print()
        
        sorted_missing = sorted(missing_fields.items(), key=lambda x: x[1], reverse=True)
        
        for field, count in sorted_missing:
            if count > 0:
                percentage = (count / len(listings)) * 100
                print(f"  {field:20} : {count:3} missing ({percentage:5.1f}%)")
        
        complete_fields = [field for field, count in sorted_missing if count == 0]
        if complete_fields:
            print(f"\n  complete fields (100%): {', '.join(complete_fields)}")
        
        print(f"{'=' * 70}\n")
