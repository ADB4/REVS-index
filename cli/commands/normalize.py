import os
import sys
import argparse
import json
from collections import defaultdict
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from core.models.listing import Listing


class DataNormalizer:
    
    def __init__(self, custom_rules=None):
        self.use_normalization = custom_rules is not None
        self.engine_mappings = {}
        self.transmission_mappings = {}
        self.variant_mappings = {}
        
        if custom_rules:
            self._load_custom_rules(custom_rules)
    
    def _load_custom_rules(self, rules_file):
        with open(rules_file, 'r') as f:
            custom = json.load(f)
            
        if 'engine' in custom:
            self.engine_mappings = custom['engine']
        if 'transmission' in custom:
            self.transmission_mappings = custom['transmission']
        if 'variant' in custom:
            self.variant_mappings = {k.upper(): v for k, v in custom['variant'].items()}
    
    def normalize_engine(self, engine):
        if not engine or engine == 'N/A' or not self.use_normalization:
            return engine
        return self.engine_mappings.get(engine, engine)
    
    def normalize_transmission(self, transmission):
        if not transmission or transmission == 'N/A' or not self.use_normalization:
            return transmission
        return self.transmission_mappings.get(transmission, transmission)
    
    def normalize_variant(self, variant):
        if not variant or not self.use_normalization:
            return variant
        return self.variant_mappings.get(variant.upper(), variant)
    
    def normalize_listing(self, listing_dict):
        normalized = listing_dict.copy()
        
        if 'engine' in normalized:
            normalized['engine'] = self.normalize_engine(normalized['engine'])
        
        if 'transmission' in normalized:
            normalized['transmission'] = self.normalize_transmission(normalized['transmission'])
        
        if 'variant' in normalized:
            normalized['variant'] = self.normalize_variant(normalized['variant'])
        
        return normalized
    
    def normalize_all(self, listings):
        return [self.normalize_listing(listing) for listing in listings]
    
    def analyze_fields(self, listings):
        stats = {
            'engine': defaultdict(int),
            'transmission': defaultdict(int),
            'variant': defaultdict(int)
        }
        
        for listing in listings:
            for field in ['engine', 'transmission', 'variant']:
                value = listing.get(field)
                if value:
                    stats[field][value] += 1
        
        return stats
    
    def print_analysis(self, before_stats, after_stats):
        print("\n" + "=" * 70)
        print("normalization analysis")
        print("=" * 70)
        
        for field in ['engine', 'transmission', 'variant']:
            print(f"\n{field}:")
            print(f"  before: {len(before_stats[field])} unique values")
            print(f"  after:  {len(after_stats[field])} unique values")
            print(f"  reduction: {len(before_stats[field]) - len(after_stats[field])} values consolidated")
            
            if len(before_stats[field]) <= 20:
                print(f"\n  before normalization:")
                for value, count in sorted(before_stats[field].items(), key=lambda x: -x[1]):
                    print(f"    - {value}: {count}")
                
                print(f"\n  after normalization:")
                for value, count in sorted(after_stats[field].items(), key=lambda x: -x[1]):
                    print(f"    - {value}: {count}")


def interactive_mode(listings):
    print("\n" + "=" * 70)
    print("interactive normalization mode")
    print("=" * 70)
    print("\nfor each unique value, you can:")
    print("  - press enter to keep unchanged")
    print("  - type a new value to normalize to")
    print("  - type 'skip' to skip this field entirely")
    print()
    
    rules = {
        'engine': {},
        'transmission': {},
        'variant': {}
    }
    
    stats = {
        'engine': defaultdict(int),
        'transmission': defaultdict(int),
        'variant': defaultdict(int)
    }
    
    for listing in listings:
        for field in ['engine', 'transmission', 'variant']:
            value = listing.get(field)
            if value:
                stats[field][value] += 1
    
    for field in ['engine', 'transmission', 'variant']:
        print("\n" + "=" * 70)
        print(f"{field} normalization")
        print("=" * 70)
        
        unique_values = sorted(stats[field].items(), key=lambda x: -x[1])
        
        if not unique_values:
            print(f"no {field} values found, skipping...")
            continue
        
        print(f"\nfound {len(unique_values)} unique {field} values:\n")
        
        for value, count in unique_values:
            print(f"  - \"{value}\" ({count} listings)")
        
        print("\n" + "-" * 70)
        print("now let's normalize these values:")
        print("-" * 70)
        
        skip_field = False
        
        for value, count in unique_values:
            if skip_field:
                break
            
            print(f"\ncurrent value: \"{value}\" ({count} listings)")
            response = input(f"  normalize to (enter=keep, 'skip'=skip field): ").strip()
            
            if response.lower() == 'skip':
                print(f"  skipping remaining {field} values...")
                skip_field = True
                continue
            elif response:
                rules[field][value] = response
                print(f"  will normalize to: \"{response}\"")
            else:
                print(f"  keeping unchanged")
    
    return rules


def save_rules(rules, output_path):
    output_dir = os.path.dirname(output_path)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir)
        print(f"  created directory: {output_dir}")
    
    with open(output_path, 'w') as f:
        json.dump(rules, f, indent=2)
    print(f"\nrules saved to: {output_path}")


def main():
    parser = argparse.ArgumentParser(description='normalize bringatrailer json data')
    parser.add_argument('--input', required=True, help='input json file path')
    parser.add_argument('--output', required=True, help='output json file path')
    parser.add_argument('--rules', help='optional custom rules json file')
    parser.add_argument('--analyze', action='store_true', help='print analysis of normalization')
    parser.add_argument('--interactive', action='store_true', help='interactive mode to build normalization rules')
    parser.add_argument('--save-rules', help='save interactive rules to json file')
    
    args = parser.parse_args()
    
    print("=" * 70)
    print("bringatrailer data normalizer")
    print("=" * 70)
    print(f"input:  {args.input}")
    print(f"output: {args.output}")
    if args.rules:
        print(f"rules:  {args.rules}")
    if args.interactive:
        print(f"mode:   interactive")
    print()
    
    print("loading data...")
    with open(args.input, 'r') as f:
        listings = json.load(f)
    print(f"loaded {len(listings)} listings")
    
    if args.interactive:
        rules_dict = interactive_mode(listings)
        
        if args.save_rules:
            save_rules(rules_dict, args.save_rules)
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tmp:
            json.dump(rules_dict, tmp, indent=2)
            temp_rules_path = tmp.name
        
        normalizer = DataNormalizer(custom_rules=temp_rules_path)
        os.unlink(temp_rules_path)
    else:
        normalizer = DataNormalizer(custom_rules=args.rules)
    
    if args.analyze:
        before_stats = normalizer.analyze_fields(listings)
    
    print("\nnormalizing data...")
    normalized_listings = normalizer.normalize_all(listings)
    
    if args.analyze:
        after_stats = normalizer.analyze_fields(normalized_listings)
        normalizer.print_analysis(before_stats, after_stats)
    
    print(f"\nsaving to {args.output}...")
    with open(args.output, 'w') as f:
        json.dump(normalized_listings, f, indent=4)
    
    print("\n" + "=" * 70)
    print("normalization complete")
    print("=" * 70)
    print(f"normalized {len(normalized_listings)} listings")
    print()
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
