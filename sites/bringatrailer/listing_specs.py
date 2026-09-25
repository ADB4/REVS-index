from typing import List

from bs4 import BeautifulSoup

from core.models.activity import AuctionDetail
from extractors.field_extractors.color_extractor import ColorExtractor
from extractors.field_extractors.engine_extractor import EngineExtractor
from extractors.field_extractors.mileage_extractor import MileageExtractor
from extractors.field_extractors.transmission_extractor import TransmissionExtractor


# more than any car shows; a bigger number is a typo or not an odometer reading
MAX_MILEAGE = 5_000_000


def element_text(elem) -> str:
    """an element's text as it reads on the page: words inside links keep their spaces, runs of whitespace become one"""
    return ' '.join(elem.get_text().split())


class ListingSpecs:
    """engine, transmission, mileage, colors, listing details and excerpt: what the selenium scraper read off a
    listing page, read here by the same extractors (with no browser) from the page the activity crawler fetched"""

    def __init__(self, config: dict):
        """config is the whole of config/sites/bringatrailer.yaml: its detail_page selectors and extraction_rules"""
        page = config['selectors']['detail_page']
        rules = config['extraction_rules']
        self.essentials = page['essentials']
        self.essentials_ul = page['essentials_ul']
        self.essentials_li = page['essentials_li']
        self.post_excerpt = page['post_excerpt']
        self.excerpt_paragraphs = page['excerpt_paragraphs']

        self.engine = EngineExtractor(rules['engine'])
        self.transmission = TransmissionExtractor(rules['transmission'])
        self.mileage = MileageExtractor(rules['mileage'])
        self.colors = ColorExtractor([rules['colors']])

    def page_selectors(self) -> List[str]:
        """the elements read here, which the raw store's fragments have to keep"""
        return [self.essentials, self.post_excerpt]

    def listing_details(self, soup: BeautifulSoup) -> List[str]:
        """the items of the first list in the essentials, as site.py's _extract_listing_details takes them"""
        essentials = soup.select_one(self.essentials)
        ul = essentials.select_one(self.essentials_ul) if essentials else None
        if not ul:
            return []
        return [text for text in (element_text(li) for li in ul.select(self.essentials_li)) if text]

    def excerpt(self, soup: BeautifulSoup) -> List[str]:
        # the first excerpt on the page is the listing's; later ones belong to the comment form
        excerpt = soup.select_one(self.post_excerpt)
        if not excerpt:
            return []
        return [text for text in (element_text(p) for p in excerpt.select(self.excerpt_paragraphs)) if text]

    def apply(self, soup: BeautifulSoup, detail: AuctionDetail):
        detail.listing_details = self.listing_details(soup)
        detail.excerpt = self.excerpt(soup)

        # driver=None: nothing here searches the comments, which a browser was needed for
        context = {'title': detail.title or '', 'listing_details': detail.listing_details}
        detail.engine = self._extract(self.engine, soup, context, detail)
        detail.transmission = self._extract(self.transmission, soup, context, detail)
        mileage = self._extract(self.mileage, soup, context, detail)
        detail.mileage = mileage if mileage is not None and 0 <= mileage <= MAX_MILEAGE else None
        detail.exterior_color, detail.interior_color = self._extract(self.colors, soup, context, detail) or (None, None)

    @staticmethod
    def _extract(extractor, soup: BeautifulSoup, context: dict, detail: AuctionDetail):
        """a spec that won't read is left empty, with a note: it never costs the listing its bids"""
        try:
            return extractor.extract(soup, None, context)
        except Exception as e:
            detail.notes.append(f"{type(extractor).__name__} failed: {e}")
            return None
