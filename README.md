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

## seller, bidder and buyer activity

`cli/commands/activity.py` tracks who sells, who bids and who wins across every completed bat auction. it doesn't use selenium: each listing page embeds its full comment thread as json (`var BAT_VMS`), so one GET per auction returns every bid with the bidder's id, amount and timestamp.

two steps:

1. **discover**: pages through the site-wide results api (`/wp-json/bringatrailer/1.0/data/listings-filter`, 60 per page) and records each auction's id, url, result, price and end time
2. **fetch**: downloads each discovered listing once and stores the seller, make/model, chassis/vin, every bid and the winner. it also follows the listing's "bat history" links, so earlier auctions of the same car are collected even when they fall outside the discovered window

```bash
# daily: new results, then the bid histories of what that run discovered
python3 cli/commands/activity.py sync

# backfill history, a chunk at a time (remembers where it stopped)
python3 cli/commands/activity.py discover --backfill --max-pages 200
python3 cli/commands/activity.py fetch --limit 2000

# or bound the backfill by date
python3 cli/commands/activity.py sync --backfill --since 2025-01-01
```

`sync` fetches only what its own discovery saw, plus already-fetched listings whose re-fetch is due (see "what gets saved"); `sync --all` works through the whole queue, like `fetch`. `--limit N` is a hard cap on listing pages, bat history links included: links left over from earlier runs go first, and new ones straight after the page that listed them. `--since` limits the queue to auctions ending on or after a date; bat history links found on those are still followed, whatever their date.

after each fetch, auctions are grouped into vehicles (see below).

### stopping and resuming

ctrl-c is safe:

- each listing is saved in one transaction, so a fetch stops between listings
- `discover --backfill` keeps a cursor and resumes from it. `--start-page` never moves the cursor past pages it hasn't read, and `--reset-backfill-cursor` starts the backfill over
- incremental discovery remembers the newest auction the last complete run saw (a watermark, seeded by the backfill). pages newer than that don't count toward the "nothing new" stop, so a run cut short by ctrl-c, a block or `--max-pages` leaves no hole: the next run walks through whatever the interrupted one missed

one run per database: `discover`, `fetch`, `sync`, `link`, `reparse` and `reset-errors` take a lock on `<db>.lock` and exit at once if another run holds it. reports don't lock and are safe to run while a crawl writes.

| exit status | meaning |
| --- | --- |
| 0 | done |
| 1 | nothing was fetched and something failed |
| 2 | stopped: the site pushed back, robots.txt ruled the crawl out, or too many failures in a row (the last lines say which) |
| 75 | another run holds the database |
| 130 | ctrl-c |

### pacing and politeness

before the backfill, read bat's terms of use and its live robots.txt, and consider asking bat.

- requests are spaced `--delay` seconds apart (default 3s) plus up to 1.5s of jitter, never less than robots.txt's crawl-delay. every 250–500 requests the crawler takes a 1–3 minute break (`activity.http` in the yaml). that averages about 4.1s a request: the full archive (~265k auctions, ~270k requests) takes about 12–13 days non-stop
- robots.txt is read from the site at the start of each run and every 24h, and applied per RFC 9309: the group for `robots_token` (or `*`), longest match wins, `*` and `$` patterns, percent-escapes normalized, and the url is checked as it will be sent, query string and params included. a missing robots.txt (4xx) means no rules; a 5xx, 429 or network error stops the run, and so does a file that rules out the results feed or listing pages. a disallowed url is refused without being requested. until the site's file has been read, the copy in the yaml applies, and differences between the two are printed
- redirects are followed by hand, at most 3 hops on the same host and scheme, each one paced, counted and checked against robots.txt like the first url
- 408, 429, 5xx and cdn 520–524 responses are retried after 15s, 30s, 60s and 120s. a `Retry-After` (in seconds or as a date) is a floor, and it holds every later request, not just the retry; a pause over 15 minutes stops the run with "resume after <utc time>"
- a response body over 10 MB, or one still arriving 2 minutes after the request, is cut off
- each run logs the first response's status and cdn/waf headers, which helps tell what a block looks like
- the user agent is `activity.http.user_agent` in `config/sites/bringatrailer.yaml` (`REVS-index-activity/0.2`). set `contact` there to a url or email and it's sent as `REVS-index-activity/0.2 (+contact)`. the crawler doesn't pass itself off as a browser: no browser user agent, no browser headers, no tls imitation

**stopping instead of hammering.** a site-level failure (401/403, retries used up, a challenge page, an unexpected status) isn't held against the listing, and 5 in a row stop the run. 20 failures of any kind in a row stop it too, e.g. after a markup change: layout errors don't use up a listing's attempts, errors that belong to the url (404, a redirect off the site) do. if auctions that discovery says have ended keep showing up without the page's "ended" marker, the run stops after 5 of them rather than skip every page.

**daily budget and active hours.** off unless asked for, and remembered in the database once set:

```bash
# at most 10,000 requests per local day, only between 08:00 and 22:00 local time
python3 cli/commands/activity.py fetch --daily-budget 10000 --active-hours 08:00-22:00

# later runs keep those settings; to drop them
python3 cli/commands/activity.py fetch --daily-budget off --active-hours off
```

outside the hours, or once the day's requests are used, the crawler pauses until it may go on. every run against the database shares the day's count. for the backfill this is the gentler way to go: at 10,000 requests a day, the archive takes about 27 days.

### what gets saved, and when it's fetched again

- only finished auctions: a page without bat's "ended" marker, or ending in the future (a live relist reached through bat history), isn't saved and isn't charged an attempt; it's fetched again on a later run
- a finished page without a result, seller, make or category, or end time is a layout error: nothing is saved, the error is recorded, and no attempt is used
- a queued url that serves a different listing is an error on that row, never a save under it
- once fetched, a page's own title, result, price and end time win over later discovery; discovery still keeps the url and flags up to date
- a fetched listing is fetched again when discovery reports it sold after the page said reserve not met, once 10 days after a reserve-not-met auction ended (post-auction deals), and when the page was saved before, or within 10 minutes of, the auction's end
- a bid count that disagrees with the page's own counter is saved and noted in `fetch_error`; `fetch --recheck-mismatches` fetches those listings again

### recovering from failures

a listing that fails 3 times (`--max-attempts`) drops out of the queue. after a block, an outage or a bug fix:

```bash
# listings and bat history links that ran out of attempts become eligible again
python3 cli/commands/activity.py reset-errors

# or allow more attempts for one run
python3 cli/commands/activity.py fetch --max-attempts 6
```

### stored pages and reparse

every fetched listing keeps what the parser read in `<db>_raw.db`, next to the database (`--raw-db` puts it elsewhere): the embedded comment data plus the page elements the parser uses, gzipped, about 18 KB a listing (roughly 5 GB for the archive). if re-parsing those fragments wouldn't give exactly the result the whole page gave, the whole page is kept instead. a parser fix can then be applied without downloading anything:

```bash
python3 cli/commands/activity.py reparse                                   # every stored page
python3 cli/commands/activity.py reparse --where "make = 'Porsche'"         # a sql condition on auctions
python3 cli/commands/activity.py reparse --ids-from listing_ids.txt         # one listing id per line
```

reparse keeps each listing's original fetch time. a fix that needs parts of the page the fragments don't hold needs a real re-fetch; scope it instead of re-fetching everything:

```bash
python3 cli/commands/activity.py fetch --where "seller_type IS NULL AND end_ts < 1600000000"
python3 cli/commands/activity.py fetch --ids-from listing_ids.txt
```

those listings are marked stale (`parser_version` goes to 0 while their data stays in the reports) and fetched again; rerunning the same command after an interruption picks up where it stopped. `fetch --upgrade` re-fetches everything saved by an older parser version.

### reports

```bash
python3 cli/commands/activity.py report                       # overview, top sellers, bidders, buyers
python3 cli/commands/activity.py report --model bmw/e46-m3    # scoped to a model (or a make: --model bmw)
python3 cli/commands/activity.py report --member lummy1088    # one member: sold, bid on, won, counterparties
python3 cli/commands/activity.py report --pairs               # sellers whose auctions the same bidders keep showing up on
python3 cli/commands/activity.py report --vehicle WBSBR934X2EX23144   # every bat auction of one car (vin, chassis, url or listing id)
python3 cli/commands/activity.py report --resales             # members who resell cars they won: hold time, price change
```

`--model` and `--since` apply to the leaderboards, `--pairs` and `--resales`; `--member`, `--vehicle`, `--pairs` and `--resales` are one at a time. a scoped report leaves out the whole-database overview.

`report --member` puts together a profile of one person from data that is public but scattered: every bid and its time, what they won and spent, what they resold and for how much, and a seller's location. keep the database private, and don't publish per-member reports.

### vehicle tracking

each auction gets a `vehicle_id` (the lowest listing id of that car). two auctions are the same car when either:

- they share a 17-character vin, or the same chassis number within the same make (pre-1981 chassis numbers are short and repeat across makes). parts and automobilia listings are never grouped by vin, since they can quote a donor car's
- one lists the other under "bat history", which also catches vins typed differently between listings

`vehicle_timeline` labels each auction by what changed since the car's previous one:

| transition | meaning |
| --- | --- |
| `first_seen` | the car's first auction here |
| `resold_by_buyer` | the previous buyer is now the seller |
| `relisted_after_sale` | the same seller again after a "sold" result, usually a sale that fell through |
| `relisted_unsold` | the same seller again after reserve not met, or after a withdrawn listing |
| `new_seller` | someone else is selling: the car changed hands off bat, or went through a dealer |
| `unknown_seller` | this auction's seller couldn't be read |
| `unknown_prev` | the previous auction's result or seller is unknown, so there's nothing to compare with |

an auction without an end time takes one from another listing's bat history, or sorts last.

one real car, from `report --vehicle WBSBR934X2EX23144`:

```
ended       result  price    seller    buyer        what happened        days since prev  price change
2015-09-09  sold    $12,750  willousb  flsandman    first seen           -                -
2015-09-14  sold    $13,000  willousb  NIACC        relisted after sale  5                +$250
2019-07-29  sold    $13,500  NIACC     drc354       resold by buyer      1,414            +$500
2021-10-03  sold    $17,000  drc354    Szvc         resold by buyer      797              +$3,500
2023-02-12  sold    $18,500  Szvc      WSomerville  resold by buyer      497              +$1,500
```

prices are hammer prices: buyer's fees, shipping and any work done while the car was held are not included.

the header says how many of the car's known auctions are fetched. auctions bat history mentions that aren't fetched yet (discovered or not) are listed under the history, and a label is marked "(provisional)" when one of them ended just before it: it may change once that auction is fetched. a chassis number shared by several cars lists them; pick one by listing url or id.

```bash
# fetch one car and its whole bat history (a full url, one without https://, or just the slug)
python3 cli/commands/activity.py fetch --url https://bringatrailer.com/listing/2002-bmw-m3-convertible-106/

# rebuild the participants table and regroup vehicles, without fetching anything
python3 cli/commands/activity.py link
```

### schema

data lives in `data/db/bat_activity.db` (sqlite), stored pages in `data/db/bat_activity_raw.db`. members are keyed by their `/member/<slug>/` slug, which is the same whether they appear as a seller, bidder or buyer.

| table / view | contents |
| --- | --- |
| `auctions` | one row per listing: result, price, end time, make/model, chassis (as parsed and as written), seller, high bidder, winner, bid counts, fetch state |
| `bids` | one row per bid: listing, bidder, amount, timestamp |
| `members` | slug, display name, numeric user id |
| `participants` | one row per member per auction bid on: bid count, max bid, won. kept in step with `bids`; `link` rebuilds it |
| `auction_participants` | `participants` with each auction's seller, model, result and price |
| `member_activity` | per-member totals for selling, bidding and winning (money columns are USD only); `win_rate` is wins among the auctions bid on, and `won_without_bid` counts wins with no parsed bid from the winner |
| `seller_bidder_pairs` | how often each bidder bids on / wins each seller's auctions |
| `listing_links` | "bat history" links from a listing to other auctions of the same car, with bat's "sold by x to y" summary |
| `vehicle_timeline` | every auction of every tracked car in order, with transition, days since previous and price change |
| `member_resales` | cars a member won and later sold again on bat: price paid, resale price, days held |
| `meta` | the backfill cursor, discovery watermark, budget settings and counts, schema version |

`report` checks the data as well:

- `auctions.bids_reported` is the page's own bid counter; listings where it disagrees with the bids parsed from the page are flagged
- stored bid rows against each listing's `n_bids`, prices above the top bid (post-auction deals, or a parse problem), and fetched rows missing a seller, make or result
- bat history's "sold by x to y" text is compared with the stored seller and buyer of each linked sale
- the results feed's total against the auctions stored from it

older databases are upgraded in place when opened; the first open after this change rebuilds the `auctions` table (the url is no longer unique) and fills `participants` from the bids, about 15s at the full archive's size. listings saved by an older parser version are re-fetched by `fetch --upgrade`; `reparse` only covers listings fetched with the raw store.

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

the selenium scraper can pick a browser user agent at random:

```python
from strategies.anti_detection.user_agent import UserAgentStrategy

ua = UserAgentStrategy()
user_agent = ua.get_random_user_agent()
```

the activity crawler (`cli/commands/activity.py`) never does this: it sends one honest user agent of its own, set in the yaml, and none of the strategies in this section apply to it.

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

for the selenium scraper:

1. verify anti-detection strategies are applied
2. check user agent rotation
3. verify delays are respected
4. consider adding more human-like behaviors

the activity crawler doesn't disguise itself. if it's blocked, it stops (exit status 2); slow down, set a daily budget, or ask the site, rather than make it look like a browser.

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
