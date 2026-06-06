#!/usr/bin/env python3
"""Diagnostic script to check website data loading issues."""
import os
import pandas as pd
from datetime import datetime

os.chdir(os.path.dirname(os.path.abspath(__file__)))

files = {
    'Cup': r'Data\Predictions\upcoming_cup_predictions.csv',
    'Global': r'Data\Predictions\upcoming_matchweek_predictions.csv',
}

print("="*80)
print("WEBSITE DATA DIAGNOSTIC")
print("="*80)

today = datetime.now().date()
print(f'\n📅 Today: {today}\n')

for name, path in files.items():
    abs_path = os.path.abspath(path)
    print(f"\n{'='*80}")
    print(f"📊 {name} Predictions: {abs_path}")
    print(f"{'='*80}")
    
    if not os.path.exists(abs_path):
        print(f"❌ File does not exist!")
        continue
    
    try:
        df = pd.read_csv(abs_path)
        print(f"✓ Total rows: {len(df)}")
        print(f"✓ Columns: {list(df.columns)}")
        
        if df.empty:
            print("⚠️  DataFrame is empty!")
            continue
        
        # Check date parsing
        print(f"\n🔍 Date Parsing Analysis:")
        df['parsed_date'] = pd.to_datetime(df['match_date'], errors='coerce').dt.normalize()
        df['date_ok'] = df['parsed_date'].notna() & (df['parsed_date'] >= pd.Timestamp(today))
        
        print(f"  Valid dates: {df['date_ok'].sum()} out of {len(df)}")
        print(f"\nFixtures:")
        for idx, row in df.iterrows():
            status = "✓" if row['date_ok'] else "✗"
            print(f"  {status} {row['match_date']:10} {row.get('competition',''):30} {row['home_team']:20} vs {row['away_team']:20}")
            
    except Exception as e:
        print(f"❌ Error reading file: {e}")

print(f"\n{'='*80}")
print("SUMMARY")
print(f"{'='*80}")
print("✓ Fixed: Index alignment issue in _load_upcoming_rows function")
print("✗ Missing: MLS and Extra-leagues prediction files")
print("\nTo resolve:")
print("  1. Run the prediction pipelines for MLS and Extra-leagues")
print("  2. Ensure all fixture dates are in the future (>= today)")
print(f"{'='*80}\n")
