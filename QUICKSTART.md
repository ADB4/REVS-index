# quick start guide

## installation

### prerequisites
```bash
pip install selenium beautifulsoup4 webdriver-manager pyyaml psycopg2-binary
```

### directory setup
```bash
cd scraper_refactored
mkdir -p data/json/output/raw
mkdir -p data/json/output/normalized
```

## basic usage

### scrape a single model

```bash
python3 cli/commands/scrape.py \
  --slug "e46-m3" \
  --make "BMW" \
  --model-full "E46 M3" \
  --model-short "M3" \
  --max-listings 50
```

output: `data/json/output/raw/e46-m3_data.json`

### scrape with year filter

```bash
python3 cli/commands/scrape.py \
  --slug "e46-m3" \
  --make "BMW" \
  --model-full "E46 M3" \
  --model-short "M3" \
  --min-year 2003 \
  --max-year 2006 \
  --max-listings 100
```

### scrape in headless mode

```bash
python3 cli/commands/scrape.py \
  --slug "e46-m3" \
  --make "BMW" \
  --model-full "E46 M3" \
  --model-short "M3" \
  --headless
```

### incremental scraping (append mode)

```bash
python3 cli/commands/scrape.py \
  --slug "e46-m3" \
  --make "BMW" \
  --model-full "E46 M3" \
  --model-short "M3" \
  --append data/json/output/raw/e46-m3_data.json \
  --sort-oldest
```

output: `data/json/output/raw/e46-m3_data_n{count}.json`

### batch scraping

create `cars.json`:
```json
[
  {
    "slug": ["e46-m3"],
    "make": "BMW",
    "modelFull": "E46 M3",
    "modelShort": "M3",
    "minYear": 2001,
    "maxYear": 2006
  },
  {
    "slug": ["997-gt3"],
    "make": "Porsche",
    "modelFull": "911 997 GT3",
    "modelShort": "911 GT3",
    "minYear": 2007,
    "maxYear": 2012
  }
]
```

```bash
python3 cli/commands/scrape.py --json cars.json
```

### field filtering

scrape only specific fields:

```bash
python3 cli/commands/scrape.py \
  --slug "e46-m3" \
  --make "BMW" \
  --model-full "E46 M3" \
  --model-short "M3" \
  --fields url title price year vin mileage
```

## normalization

### interactive normalization

```bash
python3 cli/commands/normalize.py \
  --input data/json/output/raw/e46-m3_data.json \
  --output data/json/output/normalized/e46-m3_normalized.json \
  --interactive \
  --save-rules config/normalization/e46-m3-rules.json
```

### apply saved rules

```bash
python3 cli/commands/normalize.py \
  --input data/json/output/raw/e46-m3_data.json \
  --output data/json/output/normalized/e46-m3_normalized.json \
  --rules config/normalization/e46-m3-rules.json
```

### analyze normalization

```bash
python3 cli/commands/normalize.py \
  --input data/json/output/raw/e46-m3_data.json \
  --output data/json/output/normalized/e46-m3_normalized.json \
  --rules config/normalization/e46-m3-rules.json \
  --analyze
```

## database ingestion

### set database url

```bash
export DATABASE_URL="postgresql://user:pass@localhost:5432/nfs_index"
```

### ingest data

```bash
python3 cli/commands/ingest.py \
  --json-file data/json/output/normalized/e46-m3_normalized.json
```

## testing

### run unit tests

```bash
python3 tests/unit/test_vin_extractor.py
```

### run integration tests

```bash
python3 tests/integration/test_bat_site.py
```

### run all tests

```bash
python3 -m pytest tests/ -v
```

## troubleshooting

### common issues

**import errors**:
```bash
export PYTHONPATH="${PYTHONPATH}:/path/to/scraper_refactored"
```

**selenium errors**:
```bash
pip install --upgrade selenium webdriver-manager
```

**yaml errors**:
```bash
pip install pyyaml
```

**database errors**:
```bash
pip install psycopg2-binary
export DATABASE_URL="postgresql://user:pass@host:port/db"
```

### debug mode

add logging to any module:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

### verify configuration

```bash
python3 -c "
import yaml
with open('config/sites/bringatrailer.yaml') as f:
    config = yaml.safe_load(f)
    print(config['site']['name'])
"
```

## next steps

- read full documentation in readme.md
- review migration guide in migration.md
- explore architecture in detail
- customize for your use case
- add new sites or extractors
- contribute improvements
