import os
import sys
import argparse
import json
import psycopg2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))


def get_db_connection():
    db_url = os.getenv('DATABASE_URL', 'postgresql://postgres:postgres@localhost:5432/nfs_index')
    
    if db_url.startswith('postgres://'):
        db_url = db_url.replace('postgres://', 'postgresql://', 1)
    
    return psycopg2.connect(db_url)


def get_or_create_make(conn, make_name):
    make_name = make_name.upper()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM makes WHERE name = %s", (make_name,))
        result = cur.fetchone()
        
        if result:
            return result[0]
        
        cur.execute("INSERT INTO makes (name) VALUES (%s) RETURNING id", (make_name,))
        conn.commit()
        return cur.fetchone()[0]


def get_or_create_model(conn, make_id, model_name):
    model_name = model_name.upper()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM models WHERE make_id = %s AND name = %s",
            (make_id, model_name)
        )
        result = cur.fetchone()
        
        if result:
            return result[0]
        
        cur.execute(
            "INSERT INTO models (make_id, name) VALUES (%s, %s) RETURNING id",
            (make_id, model_name)
        )
        conn.commit()
        return cur.fetchone()[0]


def get_or_create_variant(conn, model_id, variant_name):
    if not variant_name:
        variant_name = "STANDARD"
    else:
        variant_name = variant_name.upper()
    
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM variants WHERE model_id = %s AND name = %s",
            (model_id, variant_name)
        )
        result = cur.fetchone()
        
        if result:
            return result[0]
        
        cur.execute(
            "INSERT INTO variants (model_id, name) VALUES (%s, %s) RETURNING id",
            (model_id, variant_name)
        )
        conn.commit()
        return cur.fetchone()[0]


def ingest_listing(conn, listing, make_id, model_id):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM listings WHERE url = %s", (listing['url'],))
        existing = cur.fetchone()
        
        variant_name = listing.get('variant', 'Standard')
        variant_id = get_or_create_variant(conn, model_id, variant_name)
        
        sale_price_cents = listing.get('price') * 100 if 'price' in listing else None
        
        listing_details_json = None
        if 'listing_details' in listing and listing['listing_details']:
            listing_details_json = json.dumps(listing['listing_details'])
        
        values = {
            'url': listing['url'],
            'source': listing.get('source', 'bringatrailer'),
            'lot_number': listing.get('lot_number'),
            'seller': listing.get('seller'),
            'seller_type': listing.get('seller_type'),
            'result': listing.get('result'),
            'high_bidder': listing.get('high_bidder'),
            'title': listing.get('title'),
            'vin': listing.get('vin'),
            'year': listing.get('year'),
            'make_id': make_id,
            'model_id': model_id,
            'variant_id': variant_id,
            'listing_details': listing_details_json,
            'engine': listing.get('engine'),
            'transmission': listing.get('transmission'),
            'exterior_color': listing.get('exterior_color'),
            'interior_color': listing.get('interior_color'),
            'mileage': listing.get('mileage'),
            'sale_price': sale_price_cents,
            'sale_date': listing.get('sale_date'),
            'reserve_met': True if sale_price_cents else None,
            'number_of_bids': listing.get('number_of_bids'),
            'location': listing.get('location'),
        }
        
        if existing:
            cur.execute("""
                UPDATE listings SET
                    source = %(source)s,
                    lot_number = %(lot_number)s,
                    seller = %(seller)s,
                    seller_type = %(seller_type)s,
                    result = %(result)s,
                    high_bidder = %(high_bidder)s,
                    title = %(title)s,
                    vin = %(vin)s,
                    year = %(year)s,
                    make_id = %(make_id)s,
                    model_id = %(model_id)s,
                    variant_id = %(variant_id)s,
                    listing_details = %(listing_details)s::jsonb,
                    engine = %(engine)s,
                    transmission = %(transmission)s,
                    exterior_color = %(exterior_color)s,
                    interior_color = %(interior_color)s,
                    mileage = %(mileage)s,
                    sale_price = %(sale_price)s,
                    sale_date = %(sale_date)s,
                    reserve_met = %(reserve_met)s,
                    number_of_bids = %(number_of_bids)s,
                    location = %(location)s
                WHERE url = %(url)s
            """, values)
            return 'updated'
        else:
            cur.execute("""
                INSERT INTO listings (
                    url, source, lot_number, seller, seller_type, result, high_bidder,
                    title, vin, year, make_id, model_id, variant_id,
                    listing_details, engine, transmission, exterior_color, interior_color,
                    mileage, sale_price, sale_date, reserve_met,
                    number_of_bids, location
                ) VALUES (
                    %(url)s, %(source)s, %(lot_number)s, %(seller)s, %(seller_type)s, %(result)s, %(high_bidder)s,
                    %(title)s, %(vin)s, %(year)s, %(make_id)s, %(model_id)s, %(variant_id)s,
                    %(listing_details)s::jsonb, %(engine)s, %(transmission)s,
                    %(exterior_color)s, %(interior_color)s,
                    %(mileage)s, %(sale_price)s, %(sale_date)s, %(reserve_met)s,
                    %(number_of_bids)s, %(location)s
                )
            """, values)
            return 'inserted'


def main():
    parser = argparse.ArgumentParser(description='populate nfs index database from json')
    parser.add_argument('--json-file', required=True, help='path to json file')
    
    args = parser.parse_args()
    
    if not os.path.exists(args.json_file):
        print(f"error: file not found: {args.json_file}")
        return 1
    
    print("=" * 70)
    print("nfs index - database population from json")
    print("=" * 70)
    print(f"file: {args.json_file}")
    print()
    
    print("step 1: loading json data...")
    print("-" * 70)
    
    with open(args.json_file) as f:
        listings = json.load(f)
    
    if not listings:
        print(f"no listings found in json file.")
        return 1
    
    print(f"loaded {len(listings)} listings from json")
    
    first_listing = listings[0]
    make_name = first_listing.get('make')
    model_name = first_listing.get('model')
    
    if not make_name or not model_name:
        print("error: json listings must contain 'make' and 'model' fields")
        return 1
    
    print(f"make: {make_name}")
    print(f"model: {model_name}")
    
    print("\n" + "=" * 70)
    print("step 2: connecting to database...")
    print("-" * 70)
    
    try:
        conn = get_db_connection()
        print("connected to database")
    except Exception as e:
        print(f"could not connect to database: {e}")
        return 1
    
    print("\n" + "=" * 70)
    print("step 3: setting up make and model...")
    print("-" * 70)
    
    make_id = get_or_create_make(conn, make_name)
    print(f"{make_name} -> {make_name.upper()} (id: {make_id})")
    
    model_id = get_or_create_model(conn, make_id, model_name)
    print(f"{model_name} -> {model_name.upper()} (id: {model_id})")
    
    print("\n" + "=" * 70)
    print("step 4: ingesting listings...")
    print("-" * 70)
    
    inserted = 0
    updated = 0
    errors = 0
    
    for i, listing in enumerate(listings, 1):
        try:
            result = ingest_listing(conn, listing, make_id, model_id)
            if result == 'inserted':
                inserted += 1
            elif result == 'updated':
                updated += 1
            
            if i % 10 == 0:
                print(f"  processed {i}/{len(listings)} listings...")
                conn.commit()
        except Exception as e:
            errors += 1
            print(f"  error on listing {i}: {e}")
            conn.rollback()
    
    conn.commit()
    conn.close()
    
    print("\n" + "=" * 70)
    print("population complete")
    print("=" * 70)
    print(f"  inserted: {inserted} new listings")
    print(f"  updated: {updated} existing listings")
    print(f"  errors: {errors}")
    print(f"  total processed: {inserted + updated}")
    print()
    print("data is now available in the nfs index")
    print()
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
