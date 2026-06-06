#!/usr/bin/env python
import os
import sys

# Set proper working directory
os.chdir(r"c:\CS-Projects\BeyondTheStats\Soccer-Result-Predictor\Beyond-the-Stats\files")
sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.getcwd()))

try:
    import Project_World_Cup as pwc
    print("Project_World_Cup imported successfully")
    
    # Get basic setup
    bundle = pwc.ensure_model_bundle(False, "")
    print("Bundle loaded")
    
    fixtures = pwc.fetch_world_cup_fixtures("2026-06-11", "2026-07-19")
    print(f"Fixtures loaded: {len(fixtures)}")
    
    group_fixtures = [row for row in fixtures if row.get("stage") == "group-stage"]
    knockout_fixtures = [row for row in fixtures if row.get("stage") in pwc.STAGE_ORDER]
    print(f"Group fixtures: {len(group_fixtures)}")
    print(f"Knockout fixtures: {len(knockout_fixtures)}")
    
    groups, team_to_group = pwc.infer_groups(group_fixtures)
    print(f"Groups: {list(groups.keys())}")
    
    # Try one simulation
    print("\nTrying one tournament simulation...")
    try:
        simulated_groups = pwc.simulate_group_stage(group_fixtures, groups, team_to_group)
        third_place_rows = []
        for group, group_table in simulated_groups.items():
            if len(group_table) >= 3:
                third_place_rows.append(group_table[2])
        
        third_place_table = sorted(
            third_place_rows,
            key=lambda row: (-row["Pts"], -row["GD"], -row["GF"], row["team"])
        )
        
        # Convert format
        group_tables_for_knockout = []
        for group in sorted(simulated_groups.keys()):
            group_tables_for_knockout.append({
                "group": group,
                "teams": simulated_groups[group]
            })
        
        print(f"Calling simulate_knockout_stage...")
        stage_results, knockout_winners = pwc.simulate_knockout_stage(
            bundle, knockout_fixtures, group_tables_for_knockout, third_place_table
        )
        print(f"✓ Knockout simulation succeeded!")
    except Exception as e:
        import traceback
        print(f"✗ Error in knockout: {e}")
        traceback.print_exc()
        
    result = pwc.run_tournament_simulation(bundle, group_fixtures, knockout_fixtures, groups, team_to_group)
    if result:
        print(f"✓ Full tournament simulation succeeded!")
        print(f"  Keys: {list(result.keys())}")
        print(f"  Champion: {result.get('champion')}")
    else:
        print(f"✗ Full tournament simulation returned None")
    
    # Also try rank_third_place_teams to debug
    print("\n\nDebugging rank_third_place_teams...")
    try:
        simulated_groups = pwc.simulate_group_stage(group_fixtures, groups, team_to_group)
        print(f"Simulated groups keys: {list(simulated_groups.keys())}")
        print(f"Sample: {list(simulated_groups.values())[0]}")
        
        # Convert format
        group_tables_for_knockout = []
        for group in sorted(simulated_groups.keys()):
            group_tables_for_knockout.append({
                "group": group,
                "teams": simulated_groups[group]
            })
        print(f"\nConverted format for rank_third_place_teams:")
        print(f"Type: {type(group_tables_for_knockout)}")
        print(f"First entry: {group_tables_for_knockout[0]}")
        
        thirds = pwc.rank_third_place_teams(group_tables_for_knockout)
        print(f"\n✓ rank_third_place_teams succeeded!")
        print(f"Thirds: {len(thirds)}")
    except Exception as e:
        import traceback
        print(f"Error in rank_third_place_teams: {e}")
        traceback.print_exc()
        
except Exception as e:
    import traceback
    print(f"Error: {e}")
    traceback.print_exc()
