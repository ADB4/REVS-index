import re
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# the first year in a title: "No Reserve: 2022 Chevrolet Corvette Stingray" -> 2022
TITLE_YEAR_RE = re.compile(r'\b(19\d{2}|20\d{2})\b')


def normalize_slug(value: str) -> str:
    """'https://bringatrailer.com/bmw/e46-m3/', '/bmw/e46-m3/' and 'BMW/E46-M3' all give 'bmw/e46-m3'"""
    text = (value or '').strip().lower()
    text = re.sub(r'^[a-z]+://[^/]+', '', text)
    return text.split('?', 1)[0].split('#', 1)[0].strip('/')


def same_slug(a: str, b: str) -> bool:
    """one model page has two spellings: the old scraper's '/e46-m3/' and the listing tag's '/bmw/e46-m3/'.
    a short slug matches a long one by its last part; two long ones must be equal"""
    a, b = normalize_slug(a), normalize_slug(b)
    if not a or not b:
        return False
    if a == b:
        return True
    if ('/' in a) == ('/' in b):
        return False
    short, long = (a, b) if '/' not in a else (b, a)
    return long.rsplit('/', 1)[1] == short


def slugify(text: str) -> str:
    """'Mercedes-Benz' -> 'mercedes-benz', 'Aston Martin' -> 'aston-martin'"""
    return re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')


def title_year(title: str) -> Optional[int]:
    match = TITLE_YEAR_RE.search(title or '')
    return int(match.group(1)) if match else None


def word_in(needle: str, text: str, whole: bool = True) -> bool:
    """needle in text as words, ignoring case: 'M3' is in '2003 BMW M3 Coupe'. whole=False lets the last word run on,
    so 'CLK' is in '2006 Mercedes-Benz CLK500'"""
    needle = ' '.join((needle or '').split())
    if not needle:
        return False
    pattern = r'(?<![A-Za-z0-9])' + r'\s+'.join(re.escape(w) for w in needle.split())
    if whole:
        pattern += r'(?![A-Za-z0-9])'
    return re.search(pattern, text or '', re.I) is not None


@dataclass
class ModelDefinition:
    """one model, as the old scraper's cli/input/cars_*.json describe them: a key, the model page slugs, and the
    make and model names the old json output carries. filters holds each slug's listings-filter parameters, read
    from its model page; tag_slugs the listing tag spellings ('chevrolet/corvette-c8') seen for it"""
    key: str
    slugs: List[str]
    make: Optional[str] = None
    model_full: Optional[str] = None
    model_short: Optional[str] = None
    min_year: Optional[int] = None
    max_year: Optional[int] = None
    filters: Dict[str, dict] = field(default_factory=dict)
    tag_slugs: List[str] = field(default_factory=list)

    def all_slugs(self) -> List[str]:
        return list(dict.fromkeys([normalize_slug(s) for s in self.slugs + self.tag_slugs if normalize_slug(s)]))

    def names(self, ref: str) -> bool:
        """whether ref (a key or slug, in either spelling) refers to this model"""
        ref = normalize_slug(ref)
        return ref == normalize_slug(self.key) or any(same_slug(ref, s) for s in self.all_slugs())

    def matches_tag(self, model_slug: Optional[str], make: Optional[str] = None) -> bool:
        """whether a listing's model tag is this model's. a short slug's last part could name models of other
        makes, so when the make is known it has to agree"""
        tag = normalize_slug(model_slug or '')
        if not tag:
            return False
        for slug in self.all_slugs():
            if tag == slug:
                return True
            if '/' not in slug and same_slug(slug, tag):
                # the tag's first part is the make's slug; storage.activity_db.model_scope asks the same in sql
                if not self.make or tag.split('/', 1)[0] == slugify(self.make) or (make or '').lower() == self.make.lower():
                    return True
        return False

    def year_ok(self, year: Optional[int]) -> bool:
        """an unknown year is let through: the page will say"""
        if year is None:
            return True
        return not ((self.min_year and year < self.min_year) or (self.max_year and year > self.max_year))

    def title_matches(self, title: str) -> bool:
        """the fallback's pre-fetch guess: make and model_short in the title, and a model year in range. as in the
        old model lists, a model_short ending in a space ('Corvette ', 'GT ') is a whole word and one without ('CLK')
        may run on ('CLK500'); fetching the page settles it either way"""
        return (bool(self.make and self.model_short) and word_in(self.make, title)
                and word_in(self.model_short, title, whole=self.model_short != self.model_short.rstrip())
                and self.year_ok(title_year(title)))

    def output_name(self) -> str:
        """the file name part the old scraper used: its first slug, 'c6-corvette' -> c6-corvette_data.json"""
        return normalize_slug(self.slugs[0] if self.slugs else self.key).replace('/', '-')


def model_from_entry(entry: dict) -> ModelDefinition:
    """a cli/input/cars_*.json entry, with the spellings scrape.py's normalize_car_config accepts"""
    slugs = entry['slug'] if isinstance(entry['slug'], list) else [entry['slug']]
    slugs = [normalize_slug(s) for s in slugs if normalize_slug(s)]
    if not slugs:
        raise ValueError(f"model entry has no slug: {entry!r}")
    model_short = next((entry[k] for k in ('modelShort', 'modelSHort', 'modelshort') if k in entry), None)
    model_full = next((entry[k] for k in ('modelFull', 'modelfull') if k in entry), None)
    if not model_short:
        model_short = model_full or ''
    if not model_full:
        model_full = model_short.strip()
    return ModelDefinition(
        key=slugs[0], slugs=slugs, make=entry.get('make'), model_full=model_full or None,
        model_short=model_short or None, min_year=entry.get('minYear'), max_year=entry.get('maxYear')
    )


def load_model_file(path: str) -> List[ModelDefinition]:
    with open(path) as f:
        entries = json.load(f)
    if isinstance(entries, dict):
        entries = [entries]
    return [model_from_entry(e) for e in entries]
