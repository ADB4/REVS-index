import re
from typing import Optional


COMMON_WORDS = ['for', 'with', 'in', 'at', 'by', 'from', 'on', 'and', 'the']


def extract_variant(title: str, make: Optional[str], model_short: Optional[str]) -> str:
    """what a title says after the make and model: '2003 BMW M3 Coupe 6-Speed' with make 'BMW' and model_short 'M3'
    gives 'Coupe'. 'Standard' when nothing follows the model, or the title doesn't name both. shared by the
    selenium scraper (site.py) and the activity crawler's export"""
    try:
        title_upper = title.upper()
        make_upper = make.upper()
        model_short_upper = model_short.upper()

        make_index = title_upper.find(make_upper)
        if make_index == -1:
            return "Standard"

        after_make = title[make_index + len(make):].strip()
        model_index = after_make.upper().find(model_short_upper)

        if model_index == -1:
            return "Standard"

        after_model = after_make[model_index + len(model_short):].strip()

        if not after_model:
            return "Standard"

        transmission_match = re.search(r'\d+-Speed', after_model, re.I)
        if transmission_match:
            variant_end = transmission_match.start()
            variant = after_model[:variant_end].strip()
        else:
            variant = after_model.strip()

        if not variant:
            return "Standard"

        variant_parts = variant.split()
        if variant_parts:
            first_word = variant_parts[0]
            if first_word.lower() in COMMON_WORDS:
                return "Standard"

        return variant

    except Exception:
        return "Standard"
