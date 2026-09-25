# REVS-index

bring a trailer data, collected two ways:

- **activity crawler** (plain http, no browser): a model's auctions (price, specs, and who sold, bid on and bought each car), or every completed auction → sqlite, with reports on prices, members and individual cars, and an export in the scraper's json shape
- **listing scraper** (selenium, the original): one model's listings in detail → json. `activity.py model … && activity.py export …` now does its job; it stays in the repo, as do the tools after it: json → normalized json → postgres

## setup

```bash
python3 -m venv env
source env/bin/activate
pip install -r requirements.txt
```

run everything from the repo root: the scraper writes to relative paths. the scraper also needs chrome (webdriver-manager fetches the driver), and `ingest.py` needs postgres.

## usage

### listing scraper

the activity crawler's `model` and `export` replace this (see below); normalize, ingest and the llm step read either's json.

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

each listing page embeds its whole comment thread as json (`var BAT_VMS`), so one GET per auction returns every bid with bidder, amount and time, alongside the listing details the scraper read (engine, transmission, mileage, colors, excerpt). before a big crawl, read bat's terms of use and live robots.txt, and consider asking bat.

#### one model

```bash
# its auctions, their bid histories, then prices and people; rerun it to pick up new sales
python3 cli/commands/activity.py model chevrolet/c8 --make Chevrolet --model-full "C8 Corvette" --model-short "Corvette " --since 2026-03-24
python3 cli/commands/activity.py model --json cli/input/cars_testCorvetteC6.json     # the scraper's model lists work too

python3 cli/commands/activity.py report --model chevrolet/c8     # or its short spelling, c8; --since YYYY-MM-DD scopes
python3 cli/commands/activity.py export --model chevrolet/c8     # → data/json/output/raw/chevrolet-c8_data.json
```

the slug is the model page's address: `chevrolet/c8` for bringatrailer.com/chevrolet/c8/, or the scraper's short form (`e46-m3` for /e46-m3/); either spelling names the same model afterwards. a model page's "show more" pages the results api with a filter the page embeds (its sub-models' keyword pages), so `model` reads the page, keeps the filter and the sub-models' tag spellings in the database, and pages through that model's feed back to `--since` or the first auction. it fetches what's new, plus each car's bat history links (the same car's other auctions), and ends with the report. a rerun only asks for what's new, `--max-pages` spreads a long first walk over several runs, and ctrl-c resumes where it stopped. a feed that lists far more auctions than its page said isn't being narrowed by the filter, and the run refuses it rather than record the whole site as the model.

the definition (`--make`, `--model-full`, `--model-short`, `--min-year`/`--max-year`) is kept too, so later runs, reports and exports need only the slug; without the names, variants come out "Standard" and make and model are the listing pages' own (both commands say so); changing the years re-sorts what was already found. the page is read again once a week, or with `--refresh-filter`; `--filter-url` takes the listings-filter request from the browser's network tab instead, and stands until `--refresh-filter`. with no readable model page, `model` falls back to the site-wide feed back to `--since` (required then), matches titles on make and `--model-short` (a trailing space means a whole word, as in the scraper's lists), and keeps the ones whose pages are tagged as the model; it lists the tags it ruled out. a challenge page instead of the model page stops the run, like any block. `--limit`, `--no-follow-history`, `--no-report`, `--delay`, `--daily-budget` and `--active-hours` work as for `fetch`.

`report --model` leads with prices: auctions by result, sell-through, and median, low and high usd sale price by quarter (month under a year), model year, transmission and mileage, then the latest sales with specs, seller and buyer. then the people: top sellers, bidders and buyers.

`export` writes the scraper's json shape (`Listing.to_dict`) for normalize, ingest and the llm step: make and model from the definition, variant from the title, price and buyer only for sales. like the scraper it skips listings outside the usa, titled "modified" or without a vin; `--all` keeps them and withdrawn auctions (or one at a time: `--include-non-usa`, `--include-modified`, `--include-no-vin`). the model's year range always applies, and parts listings are never exported. `--format csv` adds the crawler's ids and counts; `--since` and `--output` as usual. an unfollowed slug covering several models (a make, say) is refused: ingest files a whole file under one model.

#### the whole site (optional)

`discover` pages through the site-wide results api and queues finished auctions; `fetch` downloads the queue and follows each listing's bat history links. models followed with `model` report whatever is fetched this way too.

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

**parts last.** the feed doesn't say what an auction is, so `discover` and `sync` also walk the parts and wheels feeds (`activity.parts_categories` in the yaml: `category[]=379` and `380`), to the same `--since`, at a request per 60 parts auctions. fetch then leaves those auctions till after all the cars, so a `--limit` or daily budget reaches every car first; `fetch --skip-parts` (or `sync --skip-parts`) leaves them for a later run instead. they're still worth fetching: bids on parts and memorabilia are part of a member's picture. an empty `parts_categories` turns this off.

```bash
# reports: read-only, safe while a crawl runs
python3 cli/commands/activity.py report                         # overview, top sellers, bidders, buyers
python3 cli/commands/activity.py report --model bmw/e46-m3      # a model's prices and people; a make (--model bmw) works too
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

**stopping and resuming.** ctrl-c is safe: each listing is saved in one transaction, `discover --backfill` resumes from a cursor (`--reset-backfill-cursor` starts over), and incremental discovery keeps a watermark, so an interrupted run leaves no gap; `model` keeps a cursor and watermark per model page feed. commands that write take `<db>.lock`: one run per database.

| exit | meaning |
| --- | --- |
| 0 | done |
| 1 | nothing was fetched and something failed, or `model` couldn't find a model's auctions as asked |
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
cli/input/        model lists for scrape --json and activity model --json
config/sites/     bringatrailer.yaml: selectors, extraction_rules, javascript (scraper; the crawler reads the
                  detail_page selectors and extraction_rules too); activity, robots_txt fallback (crawler)
core/browser/     selenium wrapper
core/models/      dataclasses: Listing, ScrapeConfig (scraper); Member, AuctionSummary, AuctionDetail, Bid,
                  ModelDefinition (crawler)
sites/            SiteFactory, BaseSite
  bringatrailer/  site.py (scraper); http_client.py, robots.py, activity_parser.py, listing_specs.py,
                  model_page.py (crawler)
extractors/       field extractors: vin, engine, transmission, mileage, color, price, and variant.py (the crawler
                  runs engine, transmission, mileage and color without a browser and reuses vin's pattern; export
                  runs variant)
strategies/       the scraper's anti-detection: delays, scrolling, clicks, random browser user agents
pipelines/        scraping_pipeline.py (scraper); activity_pipeline.py, crawl_budget.py, model_pipeline.py,
                  model_prices.py, model_export.py (crawler)
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
model      model page → its feed filter (auctionsCompletedInitialData.base_filter) → models
           results api with that filter → auctions, model_listings; then fetch those, and report
discover   results api (/wp-json/bringatrailer/1.0/data/listings-filter, 60 a page) → auctions (the queue)
fetch      listing page → ActivityParser (BAT_VMS json + page elements; ListingSpecs runs the scraper's
           extractors on the listing details) → auctions, bids, members, listing_links
           parsed fragments → <db>_raw.db, for reparse (~18 KB a listing, ~5 GB for the archive)
           then auctions are regrouped into vehicles
export     a model's auctions → Listing.to_dict json, the scraper's shape
```

`discover_feed` runs `discover` twice for one feed: the backfill from a page before its cursor down to `--since` or the end, then what ended after the watermark a completed run left, stopping at the watermark. the site-wide feed keeps its cursor and watermark under plain meta keys, each model page's feed under its own `model:<key>:<slug>:<filter hash>:` prefix, so a changed filter starts afresh. a model's auctions are what its feed listed (`model_listings`), plus fetched auctions tagged as it, less the ones set aside: outside its years, or title matches tagged as another model.

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
| `auctions` | one row per listing: result, price, end time, make/model, vin/chassis, seller, winner, bid counts, specs (engine, transmission, mileage, colors, categories, convertible, listing details, excerpt), fetch state |
| `bids` | listing, bidder, amount, time |
| `members` | slug, display name, numeric user id |
| `participants` | one row per member per auction bid on: bid count, max bid, won |
| `auction_participants` | `participants` with each auction's seller, model, result and price |
| `member_activity` | per-member selling, bidding and winning totals (money in usd only) |
| `seller_bidder_pairs` | how often each bidder bids on and wins each seller's auctions |
| `listing_links` | bat history links between auctions of the same car, with bat's "sold by x to y" |
| `vehicle_timeline` | every auction of every car in order: transition, days since previous, price change |
| `member_resales` | cars a member won and later resold: paid, resold for, days held |
| `models` | models followed with `model`: slugs, make and names, year range, each model page's feed filter |
| `feed_categories` | auctions the parts feeds listed, so fetch can leave them till last |
| `model_listings` | the auctions each model's discovery turned up: `member`, `unchecked` (a title match to fetch), `other_model` or `out_of_years` |
| `meta` | backfill cursors, discovery watermarks (site-wide and per model feed), budget settings and counts, schema version |

`report` also checks the data: bid counts against each page's counter, prices above the top bid, missing fields, bat history's "sold by x to y" against stored sellers and buyers, and the feed's total against stored auctions. older databases migrate in place when opened; listings saved before parser 4 get their specs from `fetch --upgrade` (or `reparse`, for pages stored whole).
