from sites.base_site import BaseSite
from sites.bringatrailer.site import BringATrailerSite
import os


class SiteFactory:
    
    @staticmethod
    def create(site_name: str) -> BaseSite:
        if site_name.lower() == 'bringatrailer':
            config_path = os.path.join(
                os.path.dirname(__file__),
                '../config/sites/bringatrailer.yaml'
            )
            return BringATrailerSite(config_path)
        else:
            raise ValueError(f"unknown site: {site_name}")
