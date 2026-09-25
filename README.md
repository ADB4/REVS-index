# REVS-index

bring a trailer data, collected two ways:

- **listing scraper** (selenium): one model's listings in detail (vin, mileage, engine, colors, price) → json → normalized json → postgres
- **activity crawler** (plain http, no browser): who sold, bid on and won every completed auction → sqlite, with reports on members and individual cars

## setup

```bash
python3 -m venv env
source env/bin/activate
pip install -r requirements.txt
```

run everything from the repo root: the scraper writes to relative paths. the scraper also needs chrome (webdriver-manager fetches the driver), and `ingest.py` needs postgres.

## usage

### listing scraper

```bash
# one model → data/json/output/raw/e46-m3_data.json
python3 cli/commands/scrape.py --slug e46-m3 --make BMW --model-full "E46 M3" --model-short M3 --max-listings 100

# several models from a json list (see cli/input/)
python3 cli/commands/scrape.py --json cli/input/cars_e46.json

# append: stop at the first lot already in the file and save only the new ones → e46-m3_data_n<count>.json
# (newest first picks up new sales; --sort-oldest fills in the old end of a capped run)
python3 cli/commands/scrape.py --slug e46-m3 --make BMW --model-full "E46 M3" --model-short M3 \
  --append data/json/output/raw/e46-m3_data.json --sort-oldest
```

also `--min-year`/`--max-year`, `--headless` and `--fields url title price ...`. listings outside the usa, titled "modified", outside the year range or without a vin are skipped.

```bash
# normalize: build rules interactively once (config/normalization/ is gitignored)
python3 cli/commands/normalize.py --input data/json/output/raw/e46-m3_data.json \
  --output data/json/output/normalized/e46-m3_normalized.json \
  --interactive --save-rules config/normalization/e46-m3-rules.json

# then reuse them; --analyze prints field stats before and after
python3 cli/commands/normalize.py --input data/json/output/raw/e46-m3_data.json \
  --output data/json/output/normalized/e46-m3_normalized.json \
  --rules config/normalization/e46-m3-rules.json --analyze

# ingest: upserts by url into existing makes/models/variants/listings tables (the schema isn't in this repo)
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/nfs_index   # the default
python3 cli/commands/ingest.py --json-file data/json/output/normalized/e46-m3_normalized.json

# json helpers: merge runs (dedupes by url), count N/A per field
python3 utils/union_json.py a.json b.json --output data/json/output/union/merged.json
python3 utils/filter_na.py data/json/output/raw/e46-m3_data.json --summary

# fill N/A fields with a local llm: ollama by default (--model, default neural-chat:13b),
# or --backend llama-cpp --model <file>.gguf. llm/setup.py installs a backend
python3 llm/json_processor.py data/json/output/raw/e46-m3_data.json -o data/json/output/<model>/results.json
```

### activity crawler

each listing page embeds its whole comment thread as json (`var BAT_VMS`), so one GET per auction returns every bid with bidder, amount and time. `discover` pages through the site-wide results api and queues finished auctions; `fetch` downloads the queue and follows each listing's "bat history" links to earlier auctions of the same car. before a backfill, read bat's terms of use and live robots.txt, and consider asking bat.

```bash
# daily: new results, then what that run discovered (plus re-fetches that are due)
python3 cli/commands/activity.py sync

# backfill a chunk at a time; each picks up where it stopped
python3 cli/commands/activity.py discover --backfill --max-pages 200
python3 cli/commands/activity.py fetch --limit 2000
python3 cli/commands/activity.py sync --backfill --since 2025-01-01    # or bounded by date

# gentler: at most 10k requests per local day, 08:00-22:00 only (remembered in the db; `off` clears either)
python3 cli/commands/activity.py fetch --daily-budget 10000 --active-hours 08:00-22:00
```

`sync --all` works through the whole queue, like `fetch`. `--limit` counts bat history links too, and `--since` still follows them whatever their date. at ~4s a request the full archive (~270k requests) takes ~12–13 days non-stop, or ~27 days at 10k a day.

```bash
# reports: read-only, safe while a crawl runs
python3 cli/commands/activity.py report                         # overview, top sellers, bidders, buyers
python3 cli/commands/activity.py report --model bmw/e46-m3      # or a make (--model bmw); --since YYYY-MM-DD also scopes
python3 cli/commands/activity.py report --member <slug>         # one member: sold, bid on, won, counterparties
python3 cli/commands/activity.py report --vehicle <vin>         # every auction of one car (vin, chassis, url or listing id)
python3 cli/commands/activity.py report --pairs                 # sellers whose auctions the same bidders keep showing up on
python3 cli/commands/activity.py report --resales               # members who resell cars they won: hold time, price change
```

`--member`, `--vehicle`, `--pairs` and `--resales` are one at a time. `report --member` profiles a real person from public but scattered data: keep the database private and don't publish per-member reports.

```bash
# maintenance, no network
python3 cli/commands/activity.py reset-errors   # listings that used up --max-attempts (default 3) are tried again
python3 cli/commands/activity.py reparse        # rerun the parser over stored pages; scope with --where or --ids-from
python3 cli/commands/activity.py link           # rebuild participants and regroup vehicles

# targeted re-fetches
python3 cli/commands/activity.py fetch --url 2002-bmw-m3-convertible-106   # one car and its bat history (url or slug)
python3 cli/commands/activity.py fetch --where "seller_type IS NULL"       # sql on auctions; or --ids-from ids.txt
python3 cli/commands/activity.py fetch --upgrade                           # everything saved by an older parser version
python3 cli/commands/activity.py fetch --recheck-mismatches                # bid count disagreed with the page's counter
```

`fetch --where`/`--ids-from` mark the matches stale (their data stays in reports) and re-fetch them; rerun the same command to resume.

**stopping and resuming.** ctrl-c is safe: each listing is saved in one transaction, `discover --backfill` resumes from a cursor (`--reset-backfill-cursor` starts over), and incremental discovery keeps a watermark, so an interrupted run leaves no gap. commands that write take `<db>.lock`: one run per database.

| exit | meaning |
| --- | --- |
| 0 | done |
| 1 | nothing was fetched and something failed |
| 2 | stopped: the site pushed back, robots.txt ruled the crawl out, or too many failures in a row (the last lines say which) |
| 75 | another run holds the database |
| 130 | ctrl-c |

### tests

```bash
python3 -m unittest discover -s tests/unit          # offline: fake clients and 127.0.0.1 servers, never bat
python3 -m unittest discover -s tests/integration   # the scraper's page parsing, on mock html
```

## architecture

```
cli/commands/     entry points: scrape, normalize, ingest (scraper); activity (crawler)
cli/input/        model lists for scrape --json
config/sites/     bringatrailer.yaml: selectors, extraction_rules, javascript (scraper);
                  activity, robots_txt fallback (crawler)
core/browser/     selenium wrapper
core/models/      dataclasses: Listing, ScrapeConfig (scraper); Member, AuctionSummary, AuctionDetail, Bid (crawler)
sites/            SiteFactory, BaseSite
  bringatrailer/  site.py (scraper); http_client.py, robots.py, activity_parser.py (crawler)
extractors/       field extractors: vin, engine, transmission, mileage, color, price (the crawler reuses vin)
strategies/       the scraper's anti-detection: delays, scrolling, clicks, random browser user agents
pipelines/        scraping_pipeline.py (scraper); activity_pipeline.py, crawl_budget.py (crawler)
storage/          json_storage.py (scraper); activity_db.py, raw_store.py (crawler)
llm/, utils/      json post-processing
tests/            unit/ (offline), integration/ (mock html)
data/json/        output/ (raw, normalized, merged, llm results); interface/ (field specs for llm --interface)
data/db/          crawler databases (gitignored)
```

### listing scraper

`scrape.py` builds a `ScrapingPipeline` from `SeleniumBrowser` (with the anti-detection strategy), the site from `SiteFactory`, and `JSONStorage`. for each slug it opens the model page, loads more results through the yaml's `javascript` snippet, then visits each listing and fills a `Listing` with the field extractors, which follow the yaml's `selectors` and `extraction_rules`, so a markup change is usually a yaml edit. `normalize.py` cleans the json with rule files and `ingest.py` upserts it into postgres.

### activity crawler

```
discover   results api (/wp-json/bringatrailer/1.0/data/listings-filter, 60 a page) → auctions (the queue)
fetch      listing page → ActivityParser (BAT_VMS json + page elements) → auctions, bids, members, listing_links
           parsed fragments → <db>_raw.db, for reparse (~18 KB a listing, ~5 GB for the archive)
           then auctions are regrouped into vehicles
```

every request goes through `BaTClient`:

- an honest user agent from `activity.http` in the yaml (`REVS-index-activity/0.2`; set `contact` there to add a url or email), with no browser headers or tls imitation. the scraper's anti-detection code never applies here
- `--delay` (3s) plus up to 1.5s jitter between requests, never under robots.txt's crawl-delay, and a 1–3 minute break every 250–500 requests
- robots.txt read at the start and every 24h (rfc 9309); disallowed urls are never requested
- 408, 429, 5xx and 520–524 retried after 15, 30, 60 and 120s; `Retry-After` is a floor for every later request
- redirects followed by hand, at most 3 hops on the same host and scheme, each paced and checked against robots.txt

the run stops with exit 2 instead of pushing on when a `Retry-After` exceeds 15 minutes, robots.txt errors (5xx, 429, network) or rules out the feed or listings, or failures come in a row: 5 site-level (401/403, retries used up, a challenge page), 20 of any kind, or 5 ended auctions missing the page's "ended" marker.

only finished auctions are saved; a page missing its result, seller, make or end time is a layout error and saves nothing. layout and site failures don't use up a listing's attempts; url errors (404, a redirect off the site) do. listings whose result may still change are re-fetched on their own: reserve-not-met auctions 10 days after the end (post-auction deals) and whenever discovery later reports them sold, and pages saved within 10 minutes of the end.

auctions sharing a 17-character vin, a chassis number within one make, or a bat history link are one car, and its `vehicle_id` is its lowest listing id (parts listings join only through bat history). `vehicle_timeline` labels each auction `first_seen`, `resold_by_buyer`, `relisted_after_sale`, `relisted_unsold`, `new_seller`, `unknown_seller` or `unknown_prev`. prices are hammer prices, without buyer's fees.

### schema

`data/db/bat_activity.db` (`--db` for another). members are keyed by their `/member/<slug>/` slug, the same whether seller, bidder or buyer.

| table / view | contents |
| --- | --- |
| `auctions` | one row per listing: result, price, end time, make/model, vin/chassis, seller, winner, bid counts, fetch state |
| `bids` | listing, bidder, amount, time |
| `members` | slug, display name, numeric user id |
| `participants` | one row per member per auction bid on: bid count, max bid, won |
| `auction_participants` | `participants` with each auction's seller, model, result and price |
| `member_activity` | per-member selling, bidding and winning totals (money in usd only) |
| `seller_bidder_pairs` | how often each bidder bids on and wins each seller's auctions |
| `listing_links` | bat history links between auctions of the same car, with bat's "sold by x to y" |
| `vehicle_timeline` | every auction of every car in order: transition, days since previous, price change |
| `member_resales` | cars a member won and later resold: paid, resold for, days held |
| `meta` | backfill cursor, discovery watermark, budget settings and counts, schema version |

`report` also checks the data: bid counts against each page's counter, prices above the top bid, missing fields, bat history's "sold by x to y" against stored sellers and buyers, and the feed's total against stored auctions. older databases migrate in place when opened.
