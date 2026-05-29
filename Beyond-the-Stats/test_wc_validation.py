#!/usr/bin/env python
import json

# Load and validate the world cup projection
with open('Data/Predictions/world_cup_projection.json', 'r') as f:
    data = json.load(f)

ko = data.get('knockout', {})
print('=== World Cup Projection Validation ===\n')
print('Knockout rounds found:')
for stage_key, matches in ko.items():
    print(f'  {stage_key}: {len(matches)} matches')

# Check for draws in knockout
total_ko_matches = 0
total_draws = 0
for stage_key, matches in ko.items():
    for match in matches:
        total_ko_matches += 1
        if match.get('predicted_result') == 'D':
            total_draws += 1
            print(f'    ERROR: Draw found in {match.get("label")}: {match.get("home_team")} vs {match.get("away_team")}')

print(f'\nTotal knockout matches: {total_ko_matches}')
print(f'Total draws in knockout: {total_draws}')
print(f'Status: {"PASS - No draws in knockout" if total_draws == 0 else "FAIL - Found draws in knockout"}')

# Sample match
ro32 = ko.get('round_of_32', [])
if ro32:
    sample = ro32[0]
    print(f'\nSample Round of 32 match:')
    print(f'  Match: {sample.get("home_team")} vs {sample.get("away_team")}')
    print(f'  Result: {sample.get("predicted_result")}')
    print(f'  Score: {sample.get("pred_home_goals")}-{sample.get("pred_away_goals")}')
    print(f'  Winner: {sample.get("winner")}')
