import json
import argparse
import os
from typing import List, Dict, Any
from operator import itemgetter


def load_json_file(filepath: str) -> List[Dict[str, Any]]:
    try:
        with open(filepath, 'r') as f:
            data = json.load(f)
            if not isinstance(data, list):
                print(f"warning: {filepath} does not contain a list, skipping")
                return []
            return data
    except Exception as e:
        print(f"error loading {filepath}: {e}")
        return []


def deduplicate_listings(listings: List[Dict[str, Any]], key_field: str = 'url') -> List[Dict[str, Any]]:
    seen = set()
    unique = []
    duplicates = 0
    
    for listing in listings:
        key_value = listing.get(key_field)
        if key_value and key_value not in seen:
            seen.add(key_value)
            unique.append(listing)
        else:
            duplicates += 1
    
    if duplicates > 0:
        print(f"removed {duplicates} duplicate listings")
    
    return unique


def sort_listings(listings: List[Dict[str, Any]], sort_fields: List[str], reverse: bool = False) -> List[Dict[str, Any]]:
    if not sort_fields:
        return listings
    
    def sort_key(listing):
        return tuple(listing.get(field) for field in sort_fields)
    
    try:
        return sorted(listings, key=sort_key, reverse=reverse)
    except Exception as e:
        print(f"error sorting by {sort_fields}: {e}")
        print("returning unsorted listings")
        return listings


def main():
    import sys
    
    parser = argparse.ArgumentParser(
        description='union multiple json files containing listing objects'
    )
    
    parser.add_argument(
        'files',
        nargs='+',
        help='json files to union (minimum 2)'
    )
    
    parser.add_argument(
        '--output',
        required=True,
        help='output file path'
    )
    
    parser.add_argument(
        '--sort-by',
        nargs='+',
        help='field(s) to sort by (e.g., --sort-by year price)'
    )
    
    parser.add_argument(
        '--reverse',
        action='store_true',
        help='sort in descending order'
    )
    
    parser.add_argument(
        '--key',
        default='url',
        help='field to use for deduplication (default: url)'
    )
    
    parser.add_argument(
        '--no-dedupe',
        action='store_true',
        help='skip deduplication step'
    )
    
    parser.add_argument(
        '--verbose',
        '-v',
        action='store_true',
        help='verbose output for debugging'
    )
    
    args = parser.parse_args()
    
    if args.verbose:
        print(f"python version: {sys.version}")
        print(f"current directory: {os.getcwd()}")
        print()
    
    if len(args.files) < 2:
        print("error: at least 2 json files required")
        return 1
    
    print("=" * 70)
    print("json union utility")
    print("=" * 70)
    print(f"input files: {len(args.files)}")
    for f in args.files:
        print(f"  - {f}")
    print()
    sys.stdout.flush()
    
    all_listings = []
    for filepath in args.files:
        if not os.path.exists(filepath):
            print(f"warning: file not found: {filepath}, skipping")
            continue
        
        listings = load_json_file(filepath)
        print(f"loaded {len(listings)} listings from {os.path.basename(filepath)}")
        all_listings.extend(listings)
    
    if not all_listings:
        print("\nerror: no listings loaded")
        return 1
    
    print(f"\ntotal listings before deduplication: {len(all_listings)}")
    
    if not args.no_dedupe:
        all_listings = deduplicate_listings(all_listings, args.key)
        print(f"total unique listings: {len(all_listings)}")
    
    if args.sort_by:
        print(f"\nsorting by: {', '.join(args.sort_by)}")
        if args.reverse:
            print("sort order: descending")
        all_listings = sort_listings(all_listings, args.sort_by, args.reverse)
    
    output_dir = os.path.dirname(args.output)
    if output_dir:
        try:
            os.makedirs(output_dir, exist_ok=True)
            if args.verbose:
                print(f"\noutput directory: {output_dir}")
        except Exception as e:
            print(f"\nerror creating output directory {output_dir}: {e}")
            return 1
    
    try:
        with open(args.output, 'w') as f:
            json.dump(all_listings, f, indent=4)
    except Exception as e:
        print(f"\nerror writing output file {args.output}: {e}")
        return 1
    
    print(f"\nwrote {len(all_listings)} listings to {args.output}")
    print("=" * 70)
    print("union complete")
    print()
    
    return 0


if __name__ == '__main__':
    exit(main())