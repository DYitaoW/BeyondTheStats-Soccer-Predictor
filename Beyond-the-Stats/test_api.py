#!/usr/bin/env python3
"""Test API endpoints to verify they return data correctly."""
import sys
import os

# Add the parent directory to the path so we can import the Flask app
sys.path.insert(0, r'c:\CS-Projects\BeyondTheStats\Soccer-Result-Predictor\Beyond-the-Stats\Website')

from app import app

print("="*80)
print("TESTING FLASK API ENDPOINTS")
print("="*80)

with app.test_client() as client:
    endpoints = [
        ("/api/upcoming/global", "Global Upcoming Fixtures"),
        ("/api/upcoming/mls", "MLS Upcoming Fixtures"),
        ("/api/upcoming/extra", "Extra League Upcoming Fixtures"),
        ("/api/upcoming/cups", "Cup Upcoming Fixtures"),
    ]
    
    for url, label in endpoints:
        print(f"\n🧪 Testing: {url}")
        print(f"📝 {label}")
        print("-" * 80)
        
        try:
            response = client.get(url)
            data = response.get_json()
            
            if response.status_code == 200:
                print(f"✓ Status: {response.status_code}")
                if data.get("ok"):
                    rows = data.get("rows", [])
                    stats = data.get("stats", {})
                    print(f"✓ Response OK: {len(rows)} rows")
                    print(f"✓ Stats: {stats}")
                    if rows:
                        print(f"✓ First row: {rows[0].get('home_team')} vs {rows[0].get('away_team')} ({rows[0].get('match_date')})")
                else:
                    print(f"✗ Response not OK: {data}")
            else:
                print(f"✗ Status: {response.status_code}")
                print(f"✗ Error: {data}")
                
        except Exception as e:
            print(f"✗ Exception: {e}")
            import traceback
            traceback.print_exc()

print(f"\n{'='*80}")
print("API ENDPOINT TESTING COMPLETE")
print(f"{'='*80}\n")
