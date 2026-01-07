import os
import sys
import argparse
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from core.browser.selenium_browser import SeleniumBrowser
from core.models.scrape_config import ScrapeConfig
from sites.factory import SiteFactory
from strategies.anti_detection.strategy import AntiDetectionStrategy
from storage.json_storage import JSONStorage
from pipelines.scraping_pipeline import ScrapingPipeline


def normalize_car_config(car):
    model_short = None
    for key in ['modelShort', 'modelSHort', 'modelshort']:
        if key in car:
            model_short = car[key]
            break
    
    model_full = None
    for key in ['modelFull', 'modelfull']:
        if key in car:
            model_full = car[key]
            break
    
    if not model_short:
        model_short = model_full or ''
    
    if not model_full:
        model_full = model_short.strip()
    
    return {
        'slugs': car['slug'] if isinstance(car['slug'], list) else [car['slug']],
        'make': car['make'],
        'model_full': model_full,
        'model_short': model_short,
        'min_year': car.get('minYear'),
        'max_year': car.get('maxYear')
    }


def main():
    parser = argparse.ArgumentParser(description='scrape bringatrailer model pages and save to json')
    parser.add_argument('--slug', nargs='+', help='slug(s) for bat url')
    parser.add_argument('--make', help='make name')
    parser.add_argument('--model-full', help='full model name')
    parser.add_argument('--model-short', help='short model name for variant matching')
    parser.add_argument('--min-year', type=int, help='minimum model year')
    parser.add_argument('--max-year', type=int, help='maximum model year')
    parser.add_argument('--max-listings', type=int, default=100, help='maximum listings to scrape')
    parser.add_argument('--headless', action='store_true', help='run in headless mode')
    parser.add_argument('--json', help='path to json file with car objects')
    parser.add_argument('--fields', nargs='+', help='specific fields to include')
    parser.add_argument('--sort-oldest', action='store_true', help='sort by oldest first')
    parser.add_argument('--append', help='path to existing json - scrape until duplicate found')
    parser.add_argument('--site', default='bringatrailer', help='site to scrape')
    
    args = parser.parse_args()
    
    if args.append and not os.path.exists(args.append):
        print(f"error: append file not found: {args.append}")
        return 1
    
    cars_to_scrape = []
    
    if args.json:
        print(f"loading cars from {args.json}...")
        with open(args.json, 'r') as f:
            cars_data = json.load(f)
        
        for car in cars_data:
            cars_to_scrape.append(normalize_car_config(car))
    else:
        if not all([args.slug, args.make, args.model_full, args.model_short]):
            print("error: --slug, --make, --model-full, and --model-short are required")
            return 1
        
        cars_to_scrape.append({
            'slugs': args.slug,
            'make': args.make,
            'model_full': args.model_full,
            'model_short': args.model_short,
            'min_year': args.min_year,
            'max_year': args.max_year
        })
    
    print(f"\ntotal cars to scrape: {len(cars_to_scrape)}\n")
    
    for idx, car_config in enumerate(cars_to_scrape, 1):
        print("=" * 70)
        print(f"[{idx}/{len(cars_to_scrape)}] bringatrailer scraper - {car_config['make']} {car_config['model_full']}")
        print("=" * 70)
        print(f"slugs: {', '.join(car_config['slugs'])}")
        if car_config['min_year']:
            print(f"year range: {car_config['min_year']}-{car_config['max_year'] or 'present'}")
        if args.append:
            print(f"append mode: will stop at first duplicate from {args.append}")
        print()
        
        config = ScrapeConfig(
            slugs=car_config['slugs'],
            make=car_config['make'],
            model_full=car_config['model_full'],
            model_short=car_config['model_short'],
            min_year=car_config['min_year'],
            max_year=car_config['max_year'],
            max_listings=args.max_listings,
            headless=args.headless,
            sort_oldest=args.sort_oldest,
            append_file=args.append,
            fields=args.fields
        )
        
        strategy = AntiDetectionStrategy()
        browser = SeleniumBrowser(anti_detection_strategy=strategy, headless=args.headless)
        site = SiteFactory.create(args.site)
        
        model_slug = car_config['slugs'][0]
        
        if args.append:
            base_name = os.path.splitext(os.path.basename(args.append))[0]
            output_path = f"data/json/output/raw/{base_name}_n{{count}}.json"
            append_storage = JSONStorage(args.append)
        else:
            output_path = f"data/json/output/raw/{model_slug}_data.json"
            append_storage = None
        
        storage = JSONStorage(output_path)
        
        pipeline = ScrapingPipeline(
            browser=browser,
            site=site,
            storage=storage,
            append_storage=append_storage
        )
        
        try:
            listings = pipeline.run(config)
            
            if listings:
                print(f"\n{'=' * 70}")
                print(f"successfully scraped {len(listings)} {car_config['model_full']} listings")
                print(f"{'=' * 70}\n")
                
                sold = [l for l in listings if l.price]
                if sold:
                    prices = [l.price for l in sold]
                    
                    print("summary statistics:")
                    print(f"  total listings: {len(listings)}")
                    print(f"  listings with price data: {len(sold)}")
                    print(f"  average sale price: ${sum(prices)/len(prices):,.0f}")
                    print(f"  price range: ${min(prices):,} - ${max(prices):,}")
                    print(f"\n{'=' * 70}")
                
                if args.append:
                    final_output = output_path.format(count=len(listings))
                else:
                    final_output = output_path
                
                storage.filepath = final_output
                storage.save(listings)
                
                print(f"\nsaved {len(listings)} listings to {final_output}")
                print("done\n")
            else:
                print("\nno listings found\n")
            
            browser.close()
        
        except Exception as e:
            print(f"\nerror scraping {car_config['make']} {car_config['model_full']}: {e}\n")
            try:
                browser.close()
            except:
                pass
            return 1
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
