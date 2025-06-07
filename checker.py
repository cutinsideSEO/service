import pandas as pd
import os
import sys
import subprocess
import csv
from collections import defaultdict
import time
import pulp

# --- פרמטרים גלובליים ---
try:
    from params import *
except ImportError:
    print("Warning: params.py not found. Using default values.")
    minGapBetweenLegs = 25
    lengthOfHorizon = 60 * 24 * 7
    preFlightTime = 60
    postFlightTime = 30
    maxShiftLength = 14 * 60
    maxSlipLength = 5 * 24 * 60
    timeBetweenShifts = 10 * 60 + preFlightTime + postFlightTime
    maxNumberOfLegs = 6

FLIGHTS_DATA_FILE = 'flights.xlsx'

# --- פונקציות עזר (ללא שינוי) ---
def build_potential_connections_fast(flights_df):
    print("--- Building Potential Connections (Optimized) ---")
    flights_by_origin = defaultdict(list)
    for flight_id, flight_data in flights_df.iterrows():
        flights_by_origin[flight_data['ORIGIN']].append(flight_id)
    potential_next_flights = defaultdict(list)
    for flight_i_id, flight_i in flights_df.iterrows():
        destination_airport = flight_i['DEST']
        candidate_flights = flights_by_origin.get(destination_airport, [])
        for flight_j_id in candidate_flights:
            if flight_i_id == flight_j_id: continue
            flight_j = flights_df.loc[flight_j_id]
            time_gap = flight_j['DEP_MIN'] - flight_i['ARR_MIN']
            if time_gap < 0: time_gap += lengthOfHorizon
            if time_gap >= minGapBetweenLegs:
                potential_next_flights[flight_i_id].append((flight_j_id, time_gap))
    for flight_id in potential_next_flights:
        potential_next_flights[flight_id].sort(key=lambda x: x[1])
    print("Finished building connections.")
    return potential_next_flights

def is_slip_valid_for_checker(slip, flights_df):
    if not slip or len(slip) > maxNumberOfLegs: return False
    start_flight = flights_df.loc[slip[0]]
    end_flight = flights_df.loc[slip[-1]]
    if start_flight['ORIGIN'] != end_flight['DEST']: return False
    slip_wraps_around = False
    shift_start_dep_min = start_flight['DEP_MIN']
    for i in range(len(slip)):
        current_leg = flights_df.loc[slip[i]]
        if i == len(slip) - 1:
            shift_end_arr_min = current_leg['ARR_MIN']
            current_shift_duration = shift_end_arr_min - shift_start_dep_min
            if shift_start_dep_min > shift_end_arr_min: current_shift_duration += lengthOfHorizon
            current_shift_duration += preFlightTime + postFlightTime
            if current_shift_duration > maxShiftLength: return False
            break
        next_leg = flights_df.loc[slip[i+1]]
        if current_leg['DEST'] != next_leg['ORIGIN']: return False
        if next_leg['DEP_MIN'] < current_leg['ARR_MIN']: slip_wraps_around = True
        gap = next_leg['DEP_MIN'] - current_leg['ARR_MIN']
        if gap < 0: gap += lengthOfHorizon
        if gap < minGapBetweenLegs: return False
        if gap >= timeBetweenShifts:
            shift_end_arr_min = current_leg['ARR_MIN']
            current_shift_duration = shift_end_arr_min - shift_start_dep_min
            if shift_start_dep_min > shift_end_arr_min: current_shift_duration += lengthOfHorizon
            current_shift_duration += preFlightTime + postFlightTime
            if current_shift_duration > maxShiftLength: return False
            shift_start_dep_min = next_leg['DEP_MIN']
    start_time = start_flight['DEP_MIN'] - preFlightTime
    end_time = end_flight['ARR_MIN'] + postFlightTime
    slip_duration = end_time - start_time
    if slip_wraps_around or (not slip_wraps_around and end_time < start_time):
        slip_duration += lengthOfHorizon
    if slip_duration > maxSlipLength: return False
    return True

def calculate_slip_cost(slip, flights_df):
    if not slip: return 0
    start_flight = flights_df.loc[slip[0]]
    end_flight = flights_df.loc[slip[-1]]
    start_time = start_flight['DEP_MIN'] - preFlightTime
    end_time = end_flight['ARR_MIN'] + postFlightTime
    duration = end_time - start_time
    slip_wraps_around = False
    for i in range(len(slip) - 1):
        if flights_df.loc[slip[i+1]]['DEP_MIN'] < flights_df.loc[slip[i]]['ARR_MIN']:
            slip_wraps_around = True
            break
    if slip_wraps_around or (not slip_wraps_around and end_time < start_time):
        duration += lengthOfHorizon
    return duration

def format_time(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s"

def generate_initial_slips(flights_df, potential_next_flights):
    print("\n--- Generating an initial set of simple slips ---")
    initial_slips = {}
    for flight1_id in flights_df.index:
        for flight2_id, _ in potential_next_flights.get(flight1_id, []):
            slip = [flight1_id, flight2_id]
            if is_slip_valid_for_checker(slip, flights_df):
                cost = calculate_slip_cost(slip, flights_df)
                initial_slips[tuple(slip)] = cost
                break
    print(f"Finished. Generated {len(initial_slips)} initial slips.")
    return initial_slips

def find_improving_slips_tuned(flights_df, potential_next_flights, dual_prices, existing_slips, max_search_depth=4, branch_limit=20, find_limit=200):
    new_slips = {}
    total_flights = len(flights_df)
    start_time = time.time()
    def find_paths_recursive(current_path):
        if len(current_path) >= max_search_depth or len(new_slips) >= find_limit: return
        last_flight_id = current_path[-1]
        for next_flight_id, _ in potential_next_flights.get(last_flight_id, [])[:branch_limit]:
            if next_flight_id in current_path: continue
            new_path = current_path + [next_flight_id]
            if is_slip_valid_for_checker(new_path, flights_df):
                slip_tuple = tuple(new_path)
                if slip_tuple not in existing_slips and slip_tuple not in new_slips:
                    cost = calculate_slip_cost(new_path, flights_df)
                    sum_of_duals = sum(dual_prices.get(f_id, 0) for f_id in new_path)
                    reduced_cost = cost - sum_of_duals
                    if reduced_cost < -0.001:
                        new_slips[slip_tuple] = cost
                        if len(new_slips) >= find_limit: return
            find_paths_recursive(new_path)
    for i, start_flight_id in enumerate(flights_df.index):
        if (i + 1) % 10 == 0 or i == total_flights - 1:
            elapsed = time.time() - start_time
            eta = (elapsed / (i + 1)) * (total_flights - (i + 1)) if i < total_flights - 1 else 0
            print(f"\r  Searching for new slips: [ETA: {format_time(eta)}] {100*(i+1)/total_flights:.1f}% ({i+1}/{total_flights}) | Found {len(new_slips)} new profitable slips.", end="")
        if len(new_slips) >= find_limit:
            print("\nFound enough new slips for this iteration.")
            break
        find_paths_recursive([start_flight_id])
    print(f"\nFinished search in {format_time(time.time() - start_time)}.")
    return new_slips

def verify_slip_pool(slip_map, flights_df, check_name=""):
    print(f"\n--- Verifying the '{check_name}' slip pool ({len(slip_map)} slips)... ---")
    invalid_count = 0
    start_time = time.time()
    if not slip_map:
        print("Warning: Slip pool is empty, nothing to verify.")
        return
    total_to_check = len(slip_map)
    for i, slip_tuple in enumerate(slip_map.keys()):
        if (i + 1) % 100 == 0 or i == total_to_check - 1:
             print(f"\r  Verifying: {100*(i+1)/total_to_check:.1f}% complete", end="")
        if not is_slip_valid_for_checker(list(slip_tuple), flights_df):
            invalid_count += 1
            print(f"\n  ERROR: Found invalid slip in pool: {slip_tuple}")
    print(f"\nVerification finished in {format_time(time.time() - start_time)}.")
    if invalid_count == 0:
        print(f"Verification successful: All {total_to_check} slips in the '{check_name}' pool are valid.")
    else:
        print(f"VERIFICATION FAILED: Found {invalid_count} invalid slips in the pool.")
        exit("Stopping due to invalid slips in the initial pool.")
    print("-" * 50)

def generate_solution_with_column_generation(airline_code: str):
    start_time_total = time.time()
    print("--- Loading and Preparing Data ---")
    df = pd.read_excel(FLIGHTS_DATA_FILE, engine='openpyxl')
    df['ID'] = pd.to_numeric(df['ID'], errors='coerce').astype('Int64')
    df.dropna(subset=['ID'], inplace=True)
    df.set_index('ID', inplace=True)
    flights_df = df[df['OP_UNIQUE_CARRIER'] == airline_code].copy()
    all_flight_ids = set(flights_df.index)
    print(f"Loaded {len(flights_df)} flights for airline {airline_code}.")
    potential_next_flights = build_potential_connections_fast(flights_df)

    print("\n" + "="*50)
    print("--- Starting Column Generation Process ---")
    print("="*50)
    
    current_slips_map = generate_initial_slips(flights_df, potential_next_flights)
    verify_slip_pool(current_slips_map, flights_df, check_name="Initial Slips")
    
    for iteration in range(25):
        print(f"\n--- Iteration {iteration + 1} ---")
        print(f"Solving RMP with {len(current_slips_map)} slips in the pool.")
        master_slip_list = list(current_slips_map.items())
        prob = pulp.LpProblem(f"CrewScheduling_RMP_Iter_{iteration}", pulp.LpMinimize)
        slip_vars = [pulp.LpVariable(f"slip_{i}", cat='Binary') for i in range(len(master_slip_list))]
        prob += pulp.lpSum([cost * slip_vars[i] for i, (_, cost) in enumerate(master_slip_list)])
        
        constraints = {}
        for flight_id in all_flight_ids:
            covering_slips_indices = [i for i, (slip, _) in enumerate(master_slip_list) if flight_id in slip]
            if covering_slips_indices:
                constraint = pulp.lpSum([slip_vars[i] for i in covering_slips_indices]) >= 1
                prob += constraint, f"cover_flight_{flight_id}"
                constraints[flight_id] = constraint
        
        prob.solve(pulp.PULP_CBC_CMD(msg=0))
        
        current_objective = pulp.value(prob.objective) if prob.objective else "N/A"
        print(f"RMP solved. Current Objective Value (Lower Bound): {current_objective}")
        if pulp.LpStatus[prob.status] != 'Optimal': print("Warning: RMP could not be solved to optimality.")
        
        dual_prices = {flight_id: -c.pi for flight_id, c in constraints.items() if hasattr(c, 'pi')}
        print("Pricing Problem: Searching for new profitable slips...")
        new_profitable_slips = find_improving_slips_tuned(flights_df, potential_next_flights, dual_prices, current_slips_map)
        
        new_slips_added = 0
        for slip, cost in new_profitable_slips.items():
            if slip not in current_slips_map:
                current_slips_map[slip] = cost
                new_slips_added += 1
        
        print(f"Found and added {new_slips_added} new unique slips to the pool.")
        if new_slips_added == 0:
            print("\nTermination: No more profitable slips found. Proceeding to final solve.")
            break
    else:
        print("\nTermination: Reached maximum number of iterations.")
            
    print("\n--- Performing final solve with all generated columns ---")
    final_master_slip_list = list(current_slips_map.items())
    final_prob = pulp.LpProblem("CrewScheduling_Final", pulp.LpMinimize)
    final_slip_vars = [pulp.LpVariable(f"final_slip_{i}", cat='Binary') for i in range(len(final_master_slip_list))]
    final_prob += pulp.lpSum([cost * final_slip_vars[i] for i, (_, cost) in enumerate(final_master_slip_list)])
    print("Applying final constraints (each flight covered AT LEAST once)...")
    for flight_id in all_flight_ids:
        covering_slips_indices = [i for i, (slip, _) in enumerate(final_master_slip_list) if flight_id in slip]
        if not covering_slips_indices:
            print(f"Error: Flight {flight_id} has no covering slips in the final pool. Solution will be infeasible.")
            continue
        final_prob += pulp.lpSum([final_slip_vars[i] for i in covering_slips_indices]) >= 1, f"final_cover_{flight_id}"
    print("Solving final problem...")
    final_prob.solve(pulp.PULP_CBC_CMD(msg=0))
    
    print(f"\n--- Solver Finished ---")
    print(f"Status: {pulp.LpStatus[final_prob.status]}")
    if pulp.LpStatus[final_prob.status] == 'Optimal':
        final_slip_list = [final_master_slip_list[i][0] for i in range(len(final_master_slip_list)) if final_slip_vars[i].varValue == 1]
        print(f"Optimal solution found with {len(final_slip_list)} slips.")
        output_filename = f"{airline_code}.csv"
        print(f"\nWriting final solution to '{output_filename}'...")
        with open(output_filename, 'w', newline='') as f: writer = csv.writer(f); writer.writerows(final_slip_list)
        print("Done.")
    else:
        print("Could not find an optimal solution for the final problem.")
    
    end_time_total = time.time()
    print(f"\nTotal process took {format_time(end_time_total - start_time_total)}.")


if __name__ == '__main__':
    airline_to_run = 'HA'
    generate_solution_with_column_generation(airline_to_run)
    
    # --- שלב אחרון: הרצה אוטומטית של ה-checker ---
    print("\n" + "="*50)
    print("--- Automatically Running Checker ---")
    print("="*50)
    
    checker_file = 'checker.py'
    if not os.path.exists(checker_file):
        print(f"Warning: '{checker_file}' not found. Skipping automatic check.")
    else:
        try:
            command = [sys.executable, checker_file, airline_to_run]
            print(f"Running command: {' '.join(command)}")
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding='utf-8', # To handle different text encodings
                check=False # Set to False to handle output even if checker fails
            )
            print("\n--- Checker Output ---")
            print(result.stdout)
            if result.stderr:
                print("\n--- Checker Errors ---")
                print(result.stderr)
        except Exception as e:
            print(f"An unexpected error occurred while running the checker: {e}")