# BringATrailer Scraper - Refactored Architecture

modular, maintainable, and extensible scraper architecture for auction sites

## architecture overview

```
scraper_refactored/
├── core/                       # core abstractions
│   ├── browser/               # browser interface and implementations
│   └── models/                # data models
├── extractors/                # field extraction logic
│   └── field_extractors/     # specific field extractors
├── sites/                     # site-specific implementations
│   └── bringatrailer/        # bringatrailer adapter
├── strategies/                # behavioral strategies
│   ├── anti_detection/       # human-like behavior
│   └── pagination/           # pagination strategies
├── storage/                   # data persistence
├── pipelines/                 # workflow orchestration
├── cli/                       # command-line interface
│   └── commands/             # individual commands
├── config/                    # configuration files
│   └── sites/                # site configurations
└── tests/                     # test suite
```

## usage

### basic scraping

```bash
cd scraper_refactored
python3 cli/commands/scrape.py \
  --slug "e46-m3" \
  --make "BMW" \
  --model-full "E46 M3" \
  --model-short "M3" \
  --max-listings 100
```

### append mode (incremental scraping)

```bash
python3 cli/commands/scrape.py \
  --slug "e46-m3" \
  --make "BMW" \
  --model-full "E46 M3" \
  --model-short "M3" \
  --append data/json/output/raw/e46-m3_data.json \
  --sort-oldest
```

### batch scraping from json

```bash
python3 cli/commands/scrape.py --json cars.json
```

### data normalization

```bash
python3 cli/commands/normalize.py \
  --input data/json/output/raw/e46-m3_data.json \
  --output data/json/output/normalized/e46-m3_normalized.json \
  --interactive \
  --save-rules config/normalization/e46-m3-rules.json
```

### database ingestion

```bash
python3 cli/commands/ingest.py \
  --json-file data/json/output/normalized/e46-m3_normalized.json
```

## adding a new site

### step 1: create site configuration

create `config/sites/newsite.yaml`:

```yaml
site:
  name: NewSite
  base_url: https://example.com
  source_name: newsite

selectors:
  listing_page:
    cards: .listing
    title: h2
  detail_page:
    price: .price
    vin: .vin

extraction_rules:
  vin:
    - type: selector
      selector: .vin
      regex: "([A-HJ-NPR-Z0-9]{17})"
```

### step 2: implement site adapter

create `sites/newsite/site.py`:

```python
from sites.base_site import BaseSite
from core.models.listing import Listing

class NewSite(BaseSite):
    def __init__(self, config_path: str):
        # load config
        # initialize extractors
        pass
    
    def navigate_to_category(self, browser, slug: str):
        browser.navigate(f"{self.base_url}/{slug}")
    
    def get_listing_urls(self, browser, config):
        # implement pagination logic
        pass
    
    def extract_listing(self, browser, url, config, sale_price=None):
        # implement extraction logic
        return Listing(...)
```

### step 3: register in factory

update `sites/factory.py`:

```python
from sites.newsite.site import NewSite

class SiteFactory:
    @staticmethod
    def create(site_name: str) -> BaseSite:
        if site_name.lower() == 'newsite':
            return NewSite('config/sites/newsite.yaml')
        # ...
```

### step 4: use the new site

```bash
python3 cli/commands/scrape.py --site newsite --slug "model" ...
```

## modifying bringatrailer selectors

when bringatrailer changes their html structure:

### step 1: identify changed selectors

open the site in browser, inspect element, find new selectors

### step 2: update configuration

edit `config/sites/bringatrailer.yaml`:

```yaml
selectors:
  detail_page:
    price: .new-price-class  # changed from .item-results
```

### step 3: test

```bash
python3 tests/integration/test_bat_site.py
```

no code changes required

## extending extractors

### adding a new field extractor

create `extractors/field_extractors/horsepower_extractor.py`:

```python
import re
from extractors.base_extractor import BaseExtractor

class HorsepowerExtractor(BaseExtractor):
    def extract(self, soup, driver=None, context=None):
        listing_details = context.get('listing_details', [])
        
        for detail in listing_details:
            match = re.search(r'(\d+)\s*hp', detail, re.I)
            if match:
                return int(match.group(1))
        
        return None
```

### using the new extractor

update `sites/bringatrailer/site.py`:

```python
from extractors.field_extractors.horsepower_extractor import HorsepowerExtractor

class BringATrailerSite(BaseSite):
    def __init__(self, config_path: str):
        # ...
        self.horsepower_extractor = HorsepowerExtractor(rules)
    
    def extract_listing(self, browser, url, config, sale_price=None):
        # ...
        horsepower = self.horsepower_extractor.extract(soup, browser.driver, context)
```

## testing

### unit tests (no browser required)

```bash
cd scraper_refactored
python3 -m pytest tests/unit/ -v
```

or:

```bash
python3 tests/unit/test_vin_extractor.py
```

### integration tests (mock html)

```bash
python3 -m pytest tests/integration/ -v
```

### end-to-end tests (real browser)

```bash
python3 tests/e2e/test_full_scrape.py
```

## anti-detection strategies

the architecture separates anti-detection into composable strategies:

### delays

```python
from strategies.anti_detection.delays import DelayStrategy

delay = DelayStrategy(min_delay=1.0)
time.sleep(delay.get_delay())
```

### user agents

```python
from strategies.anti_detection.user_agent import UserAgentStrategy

ua = UserAgentStrategy()
user_agent = ua.get_random_user_agent()
```

### scrolling

```python
from strategies.anti_detection.scrolling import ScrollStrategy

ScrollStrategy.human_scroll(driver, target_position=500)
ScrollStrategy.scroll_to_bottom_naturally(driver)
```

### clicks

```python
from strategies.anti_detection.clicks import ClickStrategy

ClickStrategy.human_click(driver, element)
```

### combined strategy

```python
from strategies.anti_detection.strategy import AntiDetectionStrategy

strategy = AntiDetectionStrategy()
strategy.apply_to_driver(driver)
strategy.human_click(driver, element)
```

## benefits of refactored architecture

### 1. resilience to site changes

**before**: search through 1000+ lines of code
**after**: update yaml configuration file

### 2. easy to test

**unit tests**: test extractors with mock html
**integration tests**: test site logic with mock data
**e2e tests**: test full pipeline with real browser

### 3. easy to extend

**add new site**: implement interface, register in factory
**add new field**: create extractor, integrate into site
**add new strategy**: implement strategy, inject into pipeline

### 4. maintainable

**single responsibility**: each class does one thing
**dependency injection**: easy to swap implementations
**configuration**: externalize site-specific details

### 5. reusable

**extractors**: use across multiple sites
**strategies**: mix and match behaviors
**storage**: swap json, database, api

## migration from old scraper

### phase 1: run both in parallel

keep old scraper for production, test new scraper:

```bash
# old scraper
python3 scrape.py --slug "e46-m3" ...

# new scraper
python3 scraper_refactored/cli/commands/scrape.py --slug "e46-m3" ...

# compare outputs
diff data/json/output/raw/e46-m3_data.json scraper_refactored/data/json/output/raw/e46-m3_data.json
```

### phase 2: validate output

ensure new scraper produces identical results:

```python
import json

with open('old_output.json') as f:
    old = json.load(f)

with open('new_output.json') as f:
    new = json.load(f)

assert len(old) == len(new)
assert old[0]['vin'] == new[0]['vin']
# etc
```

### phase 3: cutover

once validated, switch to new scraper:

```bash
# update cron jobs
# update documentation
# archive old scraper
```

## configuration reference

### site configuration (yaml)

```yaml
site:
  name: string              # site display name
  base_url: string          # base url
  source_name: string       # source identifier

robots_txt:
  crawl_delay: float        # minimum delay between requests
  allowed_paths: list       # allowed url patterns
  disallowed_paths: list    # disallowed url patterns

selectors:
  listing_page:
    cards: string           # css selector for listing cards
    # ...
  detail_page:
    price: string           # css selector for price
    # ...

extraction_rules:
  vin:
    - type: selector        # extraction method
      selector: string      # css selector
      regex: string         # regex pattern
      # ...
```

### extraction rule types

- `selector`: extract from css selector
- `listing_detail`: extract from listing details array
- `comment_search`: search in comment section
- `title`: extract from page title
- `xpath`: extract using xpath (future)

## troubleshooting

### selectors not working

1. inspect element in browser
2. verify selector in browser console: `document.querySelector('.selector')`
3. update yaml configuration
4. test with integration test

### extractor not finding data

1. check extraction rules in yaml
2. test regex pattern: `re.search(pattern, text)`
3. add logging to extractor
4. check context data being passed

### browser detection

1. verify anti-detection strategies are applied
2. check user agent rotation
3. verify delays are respected
4. consider adding more human-like behaviors

### tests failing

1. check if site html structure changed
2. update selectors in yaml
3. update expected values in tests
4. verify test data fixtures are current

## performance considerations

### memory usage

extractors are stateless and reusable:
- one extractor instance per scraper
- no memory accumulation
- minimal overhead

### speed

- parallel processing: multiple sites simultaneously
- incremental scraping: append mode stops at duplicates
- configurable delays: balance speed vs detection

### scalability

- horizontal: run multiple scraper instances
- vertical: increase max_listings per run
- distributed: split slugs across workers

## future enhancements

### planned features

- playwright browser adapter
- proxy rotation strategy
- captcha solver integration
- distributed scraping with celery
- graphql api for data access
- web ui for monitoring
- automatic selector healing

### plugin system

future: support for third-party extractors and strategies

```python
from plugins import CustomExtractor

extractor = CustomExtractor.from_plugin('community.horsepower')
```

## contributing

### adding tests

1. write test in appropriate directory
2. run test suite: `pytest`
3. ensure coverage: `pytest --cov`

### code style

- lowercase function and variable names
- no emojis or exclamation marks
- clear, descriptive names
- minimal comments (code should be self-documenting)

### pull request process

1. create feature branch
2. implement changes
3. add tests
4. update documentation
5. submit pr with description

## license

see license file

## support

for questions or issues, see documentation or open an issue
