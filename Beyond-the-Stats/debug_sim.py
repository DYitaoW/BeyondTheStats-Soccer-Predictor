#!/usr/bin/env python
import sys
sys.path.insert(0, '.')
import files.Project_World_Cup as pwc

try:
    # Get basic setup
    bundle = pwc.ensure_model_bundle(False, "")
    fixtures = pwc.fetch_world_cup_fixtures("2026-06-11", "2026-07-19")
    print(f"Fixtures loaded: {len(fixtures)}")
    
    group_fixtures = [row for row in fixtures if row.get("stage") == "group-stage"]
    knockout_fixtures = [row for row in fixtures if row.get("stage") in pwc.STAGE_ORDER]
    print(f"Group fixtures: {len(group_fixtures)}")
    print(f"Knockout fixtures: {len(knockout_fixtures)}")
    
    groups, team_to_group = pwc.infer_groups(group_fixtures)
    print(f"Groups: {len(groups)}")
    print(f"Teams by group: {team_to_group}")
    
    # Try one simulation
    print("\nTrying one simulation...")
    result = pwc.run_tournament_simulation(bundle, group_fixtures, knockout_fixtures, groups, team_to_group)
    if result:
        print(f"Simulation succeeded!")
        print(f"Result keys: {result.keys()}")
    else:
        print(f"Simulation returned None")
        
except Exception as e:
    import traceback
    print(f"Error: {e}")
    traceback.print_exc()
