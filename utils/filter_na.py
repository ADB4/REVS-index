import json
import argparse
import os
import sys
from typing import List, Dict, Any, Optional


def has_value_in_fields(listing: Dict[str, Any], target_value: Any, fields: Optional[List[str]] = None) -> bool:
    """
    check if a listing contains the target value in specified fields
    
    if fields is none, check all fields
    """
    if fields:
        check_fields = fields
    else:
        check_fields = listing.keys()
    
    for field in check_fields:
        if field in listing:
            if listing[field] == target_value:
                return True
    
    return False


def filter_listings(listings: List[Dict[str, Any]], target_value: Any, fields: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    filter listings that contain the target value in specified fields
    """
    return [listing for listing in listings if has_value_in_fields(listing, target_value, fields)]


def get_na_summary(listings: List[Dict[str, Any]], target_value: Any, fields: Optional[List[str]] = None) -> Dict[str, int]:
    """
    get count of target value occurrences per field
    """
    summary = {}
    
    if fields:
        check_fields = fields
    else:
        if listings:
            check_fields = set()
            for listing in listings:
                check_fields.update(listing.keys())
        else:
            check_fields = []
    
    for field in check_fields:
        count = sum(1 for listing in listings if listing.get(field) == target_value)
        if count > 0:
            summary[field] = count
    
    return summary


def main():
    parser = argparse.ArgumentParser(
        description='filter listings containing N/A or specified values'
    )
    
    parser.add_argument(
        'input',
        help='input json file path'
    )
    
    parser.add_argument(
        '--output',
        help='output file path (prints to stdout if not specified)'
    )
    
    parser.add_argument(
        '--fields',
        nargs='+',
        help='specific fields to check (checks all fields if not specified)'
    )
    
    parser.add_argument(
        '--value',
        default='N/A',
        help='value to search for (default: N/A)'
    )
    
    parser.add_argument(
        '--summary',
        action='store_true',
        help='show summary of N/A occurrences by field'
    )
    
    parser.add_argument(
        '--count-only',
        action='store_true',
        help='only show count of matching listings'
    )
    
    parser.add_argument(
        '--invert',
        action='store_true',
        help='return listings that do NOT contain the value'
    )
    
    args = parser.parse_args()
    
    if not os.path.exists(args.input):
        print(f"error: file not found: {args.input}")
        return 1
    
    try:
        with open(args.input, 'r') as f:
            listings = json.load(f)
    except json.JSONDecodeError as e:
        print(f"error: invalid json in {args.input}: {e}")
        return 1
    except Exception as e:
        print(f"error reading {args.input}: {e}")
        return 1
    
    if not isinstance(listings, list):
        print(f"error: input file must contain a json array")
        return 1
    
    print("=" * 70)
    print("filter n/a utility")
    print("=" * 70)
    print(f"input file: {args.input}")
    print(f"total listings: {len(listings)}")
    print(f"searching for: {repr(args.value)}")
    if args.fields:
        print(f"checking fields: {', '.join(args.fields)}")
    else:
        print(f"checking fields: all")
    print()
    sys.stdout.flush()
    
    filtered = filter_listings(listings, args.value, args.fields)
    
    if args.invert:
        filtered = [listing for listing in listings if listing not in filtered]
        print(f"listings WITHOUT {repr(args.value)}: {len(filtered)}")
    else:
        print(f"listings WITH {repr(args.value)}: {len(filtered)}")
    
    if args.summary:
        summary = get_na_summary(listings, args.value, args.fields)
        if summary:
            print(f"\noccurrences by field:")
            for field, count in sorted(summary.items(), key=lambda x: -x[1]):
                percentage = (count / len(listings)) * 100
                print(f"  {field}: {count} ({percentage:.1f}%)")
        else:
            print(f"\nno occurrences of {repr(args.value)} found")
    
    print()
    
    if args.count_only:
        print(f"count: {len(filtered)}")
        print("=" * 70)
        return 0
    
    if args.output:
        output_dir = os.path.dirname(args.output)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        
        try:
            with open(args.output, 'w') as f:
                json.dump(filtered, f, indent=4)
            print(f"wrote {len(filtered)} listings to {args.output}")
        except Exception as e:
            print(f"error writing output file: {e}")
            return 1
    else:
        print(json.dumps(filtered, indent=4))
    
    print("=" * 70)
    print("done\n")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())