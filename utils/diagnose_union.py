#!/usr/bin/env python3

import os
import sys
import json

print("union_json.py diagnostic tool")
print("=" * 70)

files_to_check = sys.argv[1:] if len(sys.argv) > 1 else []

if not files_to_check:
    print("\nusage: python3 diagnose_union.py file1.json file2.json ...")
    print("\nthis will check if the files exist and are valid json")
    sys.exit(1)

print(f"\nchecking {len(files_to_check)} file(s)...\n")

all_ok = True

for filepath in files_to_check:
    print(f"checking: {filepath}")
    
    if not os.path.exists(filepath):
        print(f"  [ERROR] file does not exist")
        all_ok = False
        continue
    
    print(f"  [OK] file exists")
    
    try:
        file_size = os.path.getsize(filepath)
        print(f"  [OK] file size: {file_size:,} bytes")
    except Exception as e:
        print(f"  [ERROR] cannot get file size: {e}")
        all_ok = False
        continue
    
    try:
        with open(filepath, 'r') as f:
            data = json.load(f)
        print(f"  [OK] valid json")
    except json.JSONDecodeError as e:
        print(f"  [ERROR] invalid json: {e}")
        all_ok = False
        continue
    except Exception as e:
        print(f"  [ERROR] cannot read file: {e}")
        all_ok = False
        continue
    
    if not isinstance(data, list):
        print(f"  [ERROR] json is not a list (it is {type(data).__name__})")
        all_ok = False
        continue
    
    print(f"  [OK] json is a list with {len(data)} items")
    
    if len(data) > 0:
        first = data[0]
        if isinstance(first, dict):
            print(f"  [OK] first item is a dict with fields: {', '.join(list(first.keys())[:5])}")
            if 'url' in first:
                print(f"  [OK] has 'url' field")
            else:
                print(f"  [WARNING] no 'url' field (deduplication will fail)")
        else:
            print(f"  [ERROR] first item is not a dict (it is {type(first).__name__})")
            all_ok = False
    else:
        print(f"  [WARNING] list is empty")
    
    print()

if all_ok:
    print("=" * 70)
    print("all files look good")
    print("\ntry running union_json.py now:")
    print(f"\npython3 utils/union_json.py {' '.join(files_to_check)} --output output.json")
else:
    print("=" * 70)
    print("found issues with one or more files")
    print("fix the errors above and try again")

sys.exit(0 if all_ok else 1)