#!/usr/bin/env python3

print("""
DISCLAIMER: Beta Version - Not an HPE Product

This script is provided as-is without official support.
It is not an official product of Hewlett Packard Enterprise (HPE).
By continuing, you acknowledge that you are using this script at your own risk.

REQUIREMENTS BEFORE RUNNING:
1. Aruba Controllers must be able to reach the internet.
2. DNS must be configured on the Aruba Controllers.
3. Aruba Activate must be enabled on the Aruba Controllers.

Type 'yes' to agree and continue, or anything else to exit.
""")

user_input = input("Do you agree to the disclaimer above? Type 'yes' to continue: ")
if user_input.strip().lower() != 'yes':
    print("Exiting script. You did not accept the disclaimer.")
    exit(1)

# Aruba Access Point Migration Assistant - Version 1.0 (STABLE)
# This version successfully handles:
# - Discovering Mobility Controllers
# - Collecting LC cluster information
# - Storing AP groups
# - Preparing for migration by disabling LC cluster settings via SSH
# - AP Convert management and cleanup
# - Real-time dashboard monitoring

import requests
from getpass import getpass
from tabulate import tabulate
from collections import defaultdict
import urllib3
import sqlite3
from datetime import datetime
import os
import json
import paramiko
import time
import re
import threading
import sys

# Disable InsecureRequestWarning (useful for self-signed certificates)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Global variables
stored_md_switches = []
mc_username = None
mc_password = None
selected_cluster = None  # Track the currently selected cluster
selected_ap_groups = []  # Track the AP groups that have been added to convert
prep_migration_target = None  # Track the nodepath and cluster used in prep migration
monitoring_active = False  # Flag to control dashboard monitoring
dashboard_thread = None  # Thread for dashboard monitoring
def login(ip, username, password):
    url = f"https://{ip}:4343/v1/api/login"
    payload = {"username": username, "password": password}
    session = requests.Session()
    response = session.post(url, data=payload, verify=False)
    
    if response.ok:
        result = response.json()
        if '_global_result' in result and result['_global_result']['status'] == "0":  # String "0"
            print(f"Login successful to {ip}!")
            
            # Extract tokens from the JSON response body
            uid = result['_global_result'].get('UIDARUBA')
            token = result['_global_result'].get('X-CSRF-Token')
            
            return session, token, uid
    
    print(f"Login failed to {ip}:", response.text)
    return None, None, None

def fetch_switch_data(session, ip, token, uid):
    # Append the UIDARUBA token in the URL as required by the API
    url = f"https://{ip}:4343/v1/configuration/showcommand?command=show+switches+debug&UIDARUBA={uid}"
    
    headers = {}
    if token:
        headers["X-CSRF-Token"] = token
    
    response = session.get(url, headers=headers, verify=False)
    
    if response.ok:
        data = response.json()
        return data
    else:
        print("Failed to fetch data:", response.text)
        return None

def filter_md_switches(data):
    md_list = []
    # The switches are in the "All Switches" array based on your example
    for switch in data.get("All Switches", []):
        # Check if the Type is "MD" (case-sensitive) and Status is not "Down"
        if switch.get("Type") == "MD":
            status = switch.get("Status", "").lower()
            if status != "down":
                md_list.append([
                    switch.get("IP Address"), 
                    switch.get("Name"), 
                    switch.get("Nodepath"),
                    switch.get("Model"),
                    switch.get("Version")
                ])
            else:
                print(f"⚠️  Skipping controller {switch.get('Name')} ({switch.get('IP Address')}) - Status: {switch.get('Status')}")
    return md_list

def fetch_lc_cluster_info(controller_ip, username, password):
    # Login to the controller
    session, token, uid = login(controller_ip, username, password)
    
    if not session:
        return None
    
    # Make API call to get LC cluster information
    url = f"https://{controller_ip}:4343/v1/configuration/showcommand?command=show+lc-cluster+group-membership&UIDARUBA={uid}"
    
    headers = {}
    if token:
        headers["X-CSRF-Token"] = token
    
    response = session.get(url, headers=headers, verify=False)
    
    if response.ok:
        data = response.json()
        return data
    else:
        print(f"Failed to fetch LC cluster info from {controller_ip}:", response.text)
        return None

def parse_lc_cluster_info(cluster_data):
    cluster_info = {
        "cluster_name": "Unknown",
        "is_leader": False,
        "members": []
    }
    
    if "_data" in cluster_data:
        for line in cluster_data["_data"]:
            if isinstance(line, str):
                if "Profile Name =" in line:
                    parts = line.split("Profile Name =")
                    if len(parts) > 1:
                        cluster_info["cluster_name"] = parts[1].strip()
                
                # Check for self and leader status
                if "self" in line and "CONNECTED (Leader)" in line:
                    cluster_info["is_leader"] = True
                
                # Check for peer entries
                if "peer" in line:
                    parts = line.split()
                    if len(parts) > 1:
                        ip_address = parts[1].strip()
                        cluster_info["members"].append(ip_address)
    
    return cluster_info

def fetch_ap_groups(controller_ip, username, password):
    # Login to the controller
    session, token, uid = login(controller_ip, username, password)
    
    if not session:
        return None
    
    # Make API call to get AP groups
    url = f"https://{controller_ip}:4343/v1/configuration/showcommand?command=show+ap-group&UIDARUBA={uid}"
    
    headers = {}
    if token:
        headers["X-CSRF-Token"] = token
    
    response = session.get(url, headers=headers, verify=False)
    
    if response.ok:
        data = response.json()
        return data
    else:
        print(f"Failed to fetch AP groups from {controller_ip}:", response.text)
        return None

def fetch_ap_database(session, ip, token, uid):
    """Fetch AP database from Mobility Conductor"""
    url = f"https://{ip}:4343/v1/configuration/showcommand?command=show+ap+database+long&UIDARUBA={uid}"
    
    headers = {}
    if token:
        headers["X-CSRF-Token"] = token
    
    response = session.get(url, headers=headers, verify=False)
    
    if response.ok:
        data = response.json()
        return data
    else:
        print(f"Failed to fetch AP database:", response.text)
        return None
def fetch_ap_convert_status(controller_ip, username, password):
    """Fetch AP convert status from a specific controller"""
    try:
        # Login to the controller
        session, token, uid = login(controller_ip, username, password)
        
        if not session:
            return None
        
        # Make API call to get AP convert status
        url = f"https://{controller_ip}:4343/v1/configuration/showcommand?command=show+ap+convert-status&UIDARUBA={uid}"
        
        headers = {}
        if token:
            headers["X-CSRF-Token"] = token
        
        response = session.get(url, headers=headers, verify=False)
        
        if response.ok:
            data = response.json()
            return data
        else:
            print(f"Failed to fetch AP convert status from {controller_ip}:", response.text)
            return None
            
    except Exception as e:
        print(f"Error fetching AP convert status from {controller_ip}: {str(e)}")
        return None

def parse_ap_convert_status(convert_status_data):
    """Parse AP convert status data and return list of converting APs and summary info"""
    converting_aps = []
    conversion_summary = {
        'status': 'Unknown',
        'mode': 'Unknown',
        'current_converting': 0,
        'max_converting': 0,
        'start_time': 'Unknown',
        'current_status': 'Unknown',
        'ap_groups': []
    }
    
    if not convert_status_data:
        return converting_aps, conversion_summary
    
    # Parse conversion parameters
    if "AP Conversion Parameters" in convert_status_data:
        for param in convert_status_data["AP Conversion Parameters"]:
            item = param.get("Item", "")
            value = param.get("Value", "")
            
            if item == "Status":
                conversion_summary['status'] = value
            elif item == "Mode":
                conversion_summary['mode'] = value
            elif item == "Current Simultaneous Converting":
                conversion_summary['current_converting'] = int(value) if value.isdigit() else 0
            elif item == "Max Simultaneous Converting":
                conversion_summary['max_converting'] = int(value) if value.isdigit() else 0
            elif item == "Start Time":
                conversion_summary['start_time'] = value
            elif item == "Current Status":
                conversion_summary['current_status'] = value
    
    # Parse AP groups listed for conversion
    if "AP Groups Listed for Conversion" in convert_status_data:
        for group in convert_status_data["AP Groups Listed for Conversion"]:
            if "AP Groups" in group:
                conversion_summary['ap_groups'].append(group["AP Groups"])
    
    # Parse currently converting APs from "AP Image Conversion Status"
    if "AP Image Conversion Status" in convert_status_data:
        for ap_entry in convert_status_data["AP Image Conversion Status"]:
            if isinstance(ap_entry, dict):
                # Extract AP information from the structured data
                ap_name = ap_entry.get("AP Name", "Unknown")
                ap_mac = ap_entry.get("AP Mac", "Unknown")
                upgrade_state = ap_entry.get("Upgrade State", "Unknown")
                start_time = ap_entry.get("Start Time", "Unknown")
                failure_reason = ap_entry.get("Failure Reason", "")
                
                converting_aps.append({
                    'name': ap_name,
                    'mac': ap_mac,
                    'status': upgrade_state,
                    'progress': failure_reason if failure_reason else "In Progress",
                    'timestamp': datetime.now(),
                    'start_time': start_time
                })
    
    # Also check _data for any text-based AP entries (fallback)
    if "_data" in convert_status_data and convert_status_data["_data"]:
        for line in convert_status_data["_data"]:
            if isinstance(line, str) and line.strip():
                # Skip header lines and empty lines
                if any(skip_text in line for skip_text in [
                    "AP Name", "------", "Status", "Total APs", "No APs", "AP Group", "AP Mac"
                ]):
                    continue
                
                # Parse AP entries - format varies
                parts = line.strip().split()
                if len(parts) >= 3:
                    ap_name = parts[0]
                    # Skip if this looks like a summary line
                    if ap_name and not any(summary_text in ap_name for summary_text in [
                        "Total", "Completed", "Failed", "In-Progress"
                    ]):
                        mac_address = parts[1] if len(parts) > 1 else "Unknown"
                        status = parts[2] if len(parts) > 2 else "Unknown"
                        progress = " ".join(parts[3:]) if len(parts) > 3 else ""
                        
                        converting_aps.append({
                            'name': ap_name,
                            'mac': mac_address,
                            'status': status,
                            'progress': progress,
                            'timestamp': datetime.now()
                        })
    
    return converting_aps, conversion_summary
def track_conversion_progress(controller_name, conversion_summary, all_time_data):
    """Track conversion progress and estimate completed APs based on summary data"""
    controller_key = f"{controller_name}"
    
    if controller_key not in all_time_data:
        all_time_data[controller_key] = {
            'conversion_started': False,
            'start_time': None,
            'ap_groups': [],
            'peak_converting': 0,
            'total_processed_estimate': 0,
            'last_current_converting': 0
        }
    
    controller_data = all_time_data[controller_key]
    current_converting = conversion_summary.get('current_converting', 0)
    
    # Track if conversion has started
    if conversion_summary.get('status') == 'Active' and not controller_data['conversion_started']:
        controller_data['conversion_started'] = True
        controller_data['start_time'] = conversion_summary.get('start_time')
        controller_data['ap_groups'] = conversion_summary.get('ap_groups', [])
        print(f"🚀 Conversion started on {controller_name} for AP groups: {', '.join(controller_data['ap_groups'])}")
    
    # Track peak converting count
    if current_converting > controller_data['peak_converting']:
        controller_data['peak_converting'] = current_converting
    
    # Estimate total processed APs
    # If current converting dropped from previous cycle, those APs likely completed
    if controller_data['last_current_converting'] > current_converting:
        newly_completed = controller_data['last_current_converting'] - current_converting
        controller_data['total_processed_estimate'] += newly_completed
        if newly_completed > 0:
            print(f"✅ Estimated {newly_completed} APs completed on {controller_name}")
    
    controller_data['last_current_converting'] = current_converting
    
    return controller_data
def get_cluster_controllers_for_monitoring():
    """Get all controllers in the selected cluster for monitoring"""
    global selected_cluster
    
    if not selected_cluster:
        return []
    
    controllers = get_controllers_by_cluster(selected_cluster)
    return controllers

def monitor_ap_conversion():
    """Monitor AP conversion status across all controllers in the selected cluster"""
    global monitoring_active, mc_username, mc_password
    
    print(f"\n{'='*80}")
    print("LIVE AP CONVERSION MONITORING DASHBOARD")
    print(f"{'='*80}")
    print(f"Monitoring Cluster: {selected_cluster}")
    print("Press Ctrl+C to stop monitoring and return to main menu")
    print(f"{'='*80}")
    
    # Get controllers to monitor
    controllers = get_cluster_controllers_for_monitoring()
    
    if not controllers:
        print("No controllers found for monitoring.")
        return
    
    print(f"Monitoring {len(controllers)} controllers:")
    for controller in controllers:
        print(f"  - {controller['name']} ({controller['ip_address']})")
    
    # Initialize tracking variables
    all_time_aps = {}  # Track all APs we've ever seen
    completed_aps = set()  # APs that are no longer in the conversion list
    previous_converting = set()  # APs that were converting in the last check
    
    start_time = datetime.now()
    check_count = 0
    
    try:
        while monitoring_active:
            check_count += 1
            current_time = datetime.now()
            elapsed_time = current_time - start_time
            
            # Clear screen for dashboard refresh
            os.system('cls' if os.name == 'nt' else 'clear')
            
            print(f"\n{'='*80}")
            print("🔄 LIVE AP CONVERSION MONITORING DASHBOARD")
            print(f"{'='*80}")
            print(f"Cluster: {selected_cluster}")
            print(f"Runtime: {str(elapsed_time).split('.')[0]}")
            print(f"Last Update: {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"Refresh Count: {check_count}")
            print(f"{'='*80}")
            
            # Collect current status from all controllers
            current_converting = set()
            controller_status = {}
            total_api_calls = 0
            successful_api_calls = 0
            
            for controller in controllers:
                controller_ip = controller['ip_address']
                controller_name = controller['name']
                
                print(f"📡 Querying {controller_name}...", end=" ", flush=True)
                
                # Fetch AP convert status
                convert_data = fetch_ap_convert_status(controller_ip, mc_username, mc_password)
                total_api_calls += 1
                
                if convert_data:
                    successful_api_calls += 1
                    converting_aps, conversion_summary = parse_ap_convert_status(convert_data)
                    
                    # Track conversion progress for this controller
                    controller_progress = track_conversion_progress(controller_name, conversion_summary, all_time_aps)
                    
                    controller_status[controller_name] = {
                        'status': 'Online',
                        'aps': converting_aps,
                        'count': len(converting_aps),
                        'summary': conversion_summary,
                        'progress': controller_progress
                    }
                    
                    # Add to current converting set
                    for ap in converting_aps:
                        ap_name = ap['name']
                        current_converting.add(ap_name)
                        
                        # Track this AP
                        if ap_name not in all_time_aps:
                            all_time_aps[ap_name] = {
                                'first_seen': current_time,
                                'last_seen': current_time,
                                'controller': controller_name,
                                'mac': ap['mac'],
                                'status_history': []
                            }
                        
                        all_time_aps[ap_name]['last_seen'] = current_time
                        all_time_aps[ap_name]['status_history'].append({
                            'time': current_time,
                            'status': ap['status'],
                            'progress': ap['progress']
                        })
                    
                    print("✅")
                else:
                    controller_status[controller_name] = {
                        'status': 'Offline/Error',
                        'aps': [],
                        'count': 0,
                        'summary': {},
                        'progress': {}
                    }
                    print("❌")
            
            # Identify newly completed APs
            newly_completed = previous_converting - current_converting
            if newly_completed:
                for ap_name in newly_completed:
                    completed_aps.add(ap_name)
                    if ap_name in all_time_aps:
                        all_time_aps[ap_name]['completed_time'] = current_time
            
            # Update previous_converting for next iteration
            previous_converting = current_converting.copy()
            
            # Display controller status
            print(f"\n📊 CONTROLLER STATUS:")
            print("-" * 80)
            controller_table = []
            
            for controller_name, status in controller_status.items():
                controller_table.append([
                    controller_name,
                    status['status'],
                    status['count']
                ])
            
            print(tabulate(controller_table, 
                         headers=['Controller', 'API Status', 'Converting APs'], 
                         tablefmt='grid'))
            
            # Display enhanced conversion summary
            print(f"\n📈 CONVERSION SUMMARY:")
            print("-" * 80)
            print(f"🔄 Currently Converting: {len(current_converting)} APs")
            print(f"✅ Completed: {len(completed_aps)} APs")
            print(f"📋 Total Tracked: {len(all_time_aps)} APs")
            print(f"🌐 API Success Rate: {successful_api_calls}/{total_api_calls} ({(successful_api_calls/total_api_calls*100):.1f}%)" if total_api_calls > 0 else "🌐 API Success Rate: N/A")
            
            # Display per-controller conversion details
            print(f"\n🏗️  CONTROLLER CONVERSION DETAILS:")
            print("-" * 80)
            conversion_details_table = []
            total_estimated_processed = 0
            
            for controller_name, status in controller_status.items():
                if 'summary' in status and status['summary']:
                    summary = status['summary']
                    progress = status.get('progress', {})
                    
                    estimated_processed = progress.get('total_processed_estimate', 0)
                    total_estimated_processed += estimated_processed
                    
                    conversion_details_table.append([
                        controller_name,
                        summary.get('current_status', 'Unknown'),
                        f"{summary.get('current_converting', 0)}/{summary.get('max_converting', 0)}",
                        ', '.join(summary.get('ap_groups', [])),
                        estimated_processed,
                        summary.get('start_time', 'Unknown')
                    ])
            
            if conversion_details_table:
                print(tabulate(conversion_details_table,
                             headers=['Controller', 'Status', 'Converting', 'AP Groups', 'Est. Completed', 'Started'],
                             tablefmt='grid'))
                print(f"\n📊 Total Estimated Processed APs: {total_estimated_processed}")
            else:
                print(f"\n✅ NO APs CURRENTLY CONVERTING")
                if len(all_time_aps) > 0:
                    print("🎉 All tracked APs have completed conversion!")
            
            # Display recently completed APs
            if completed_aps:
                print(f"\n✅ COMPLETED APs ({len(completed_aps)}):")
                print("-" * 80)
                completed_table = []
                
                for ap_name in sorted(completed_aps):
                    if ap_name in all_time_aps:
                        ap_info = all_time_aps[ap_name]
                        completed_time = ap_info.get('completed_time', 'Unknown')
                        duration = "Unknown"
                        
                        if completed_time != 'Unknown' and 'first_seen' in ap_info:
                            duration = str(completed_time - ap_info['first_seen']).split('.')[0]
                        
                        completed_table.append([
                            ap_name,
                            ap_info.get('controller', 'Unknown'),
                            ap_info.get('mac', 'Unknown'),
                            completed_time.strftime('%H:%M:%S') if completed_time != 'Unknown' else 'Unknown',
                            duration
                        ])
                
                if completed_table:
                    # Show only the last 10 completed APs to keep the display manageable
                    display_completed = completed_table[-10:] if len(completed_table) > 10 else completed_table
                    print(tabulate(display_completed,
                                 headers=['AP Name', 'Controller', 'MAC Address', 'Completed At', 'Total Duration'],
                                 tablefmt='grid'))
                    
                    if len(completed_table) > 10:
                        print(f"... and {len(completed_table) - 10} more completed APs")
            
            print(f"\n{'='*80}")
            print("⏱️  Refreshing in 10 seconds... (Press Ctrl+C to stop monitoring)")
            print(f"{'='*80}")
            
            # Wait for next refresh
            time.sleep(10)
            
    except KeyboardInterrupt:
        print(f"\n\n🛑 Monitoring stopped by user.")
    except Exception as e:
        print(f"\n\n❌ Error during monitoring: {str(e)}")
    finally:
        monitoring_active = False
        
        # Display final summary
        print(f"\n{'='*80}")
        print("📊 FINAL MONITORING SUMMARY")
        print(f"{'='*80}")
        print(f"Total Runtime: {str(datetime.now() - start_time).split('.')[0]}")
        print(f"Total Refresh Cycles: {check_count}")
        print(f"Total APs Tracked: {len(all_time_aps)}")
        print(f"APs Completed: {len(completed_aps)}")
        print(f"APs Still Converting: {len(current_converting)}")
        
        if len(all_time_aps) > 0 and len(completed_aps) == len(all_time_aps):
            print("\n🎉 ALL ACCESS POINTS HAVE COMPLETED CONVERSION! 🎉")
            print("Migration appears to be complete.")
        elif len(current_converting) > 0:
            print(f"\n⚠️  {len(current_converting)} APs are still converting.")
            print("You may want to continue monitoring or check controller status manually.")
        
        print(f"{'='*80}")

def start_monitoring_dashboard():
    """Start the monitoring dashboard in a controlled manner"""
    global monitoring_active, selected_cluster, mc_username, mc_password
    
    if not selected_cluster:
        print("No cluster selected. Please select a cluster first (option 4).")
        return False
    
    if not mc_username or not mc_password:
        print("MC credentials not available. Please run discovery first (option 1).")
        return False
    
    controllers = get_cluster_controllers_for_monitoring()
    if not controllers:
        print(f"No controllers found for cluster: {selected_cluster}")
        return False
    
    print(f"\n🚀 Starting Live AP Conversion Monitoring...")
    print(f"Cluster: {selected_cluster}")
    print(f"Controllers to monitor: {len(controllers)}")
    
    # Confirm start
    confirm = input("Start live monitoring dashboard? (y/n): ")
    if confirm.lower() != 'y':
        print("Monitoring cancelled.")
        return False
    
    # Set monitoring flag and start
    monitoring_active = True
    
    try:
        monitor_ap_conversion()
    except Exception as e:
        print(f"Error during monitoring: {str(e)}")
        monitoring_active = False
    
    return True
def clear_database():
    """Clear all data from the database tables"""
    db_path = 'aruba_migration.db'
    
    if os.path.exists(db_path):
        print("Clearing existing database...")
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            # Clear all tables
            cursor.execute('DELETE FROM ap_types')
            cursor.execute('DELETE FROM ap_groups') 
            cursor.execute('DELETE FROM lc_clusters')
            cursor.execute('DELETE FROM controllers')
            
            conn.commit()
            conn.close()
            print("✓ Database cleared successfully.")
        except Exception as e:
            print(f"Error clearing database: {str(e)}")
    else:
        print("No existing database found.")
        
def count_ap_types(ap_database_data):
    """Count AP types from AP database data"""
    type_counts = {}
    
    # Debug - print structure of the response
    print("\nDebug - AP Database Response Structure:")
    print(f"Response Keys: {ap_database_data.keys() if isinstance(ap_database_data, dict) else 'Not a dictionary'}")
    
    # Check if we have the AP Database key in the response
    if not isinstance(ap_database_data, dict) or "AP Database" not in ap_database_data:
        print("AP Database key not found in response.")
        
        # If there's _data key, let's check its contents as a fallback
        if "_data" in ap_database_data:
            print("Found _data key, checking contents...")
            for line in ap_database_data["_data"][:5]:  # Print first 5 lines for debugging
                print(f"Line: {line}")
            
        return type_counts
    
    # Get the AP Database array
    ap_database = ap_database_data["AP Database"]
    print(f"Found {len(ap_database)} APs in the database")
    
    # Print sample data for debugging
    if len(ap_database) > 0:
        print("Sample AP entry:")
        print(ap_database[0])
    
    # Count AP types from the structured data
    for ap in ap_database:
        if isinstance(ap, dict) and "AP Type" in ap:
            ap_type = ap["AP Type"]
            if ap_type in type_counts:
                type_counts[ap_type] += 1
            else:
                type_counts[ap_type] = 1
    
    print(f"Identified {len(type_counts)} different AP types")
    print(f"AP Types found: {', '.join(type_counts.keys())}")
    
    return type_counts

def display_ap_type_counts(type_counts):
    """Display counts of each AP type"""
    print("\nAP Type Distribution:")
    print("--------------------")
    
    if not type_counts:
        print("No AP types found.")
        return
    
    table_data = []
    for ap_type, count in sorted(type_counts.items()):
        table_data.append([ap_type, count])
    
    headers = ["AP Type", "Count"]
    print(tabulate(table_data, headers=headers, tablefmt="grid"))
    
    total = sum(type_counts.values())
    print(f"\nTotal APs: {total}")

def store_ap_type_counts(type_counts):
    """Store AP type counts in the database"""
    conn = sqlite3.connect('aruba_migration.db')
    cursor = conn.cursor()
    
    # Check if the table exists, create if not
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS ap_types (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ap_type TEXT UNIQUE,
        count INTEGER,
        added_on TIMESTAMP
    )
    ''')
    
    # Clear existing data
    cursor.execute('DELETE FROM ap_types')
    
    # Insert new data
    for ap_type, count in type_counts.items():
        cursor.execute('''
        INSERT INTO ap_types (ap_type, count, added_on)
        VALUES (?, ?, ?)
        ''', (ap_type, count, datetime.now()))
    
    conn.commit()
    conn.close()

def init_database():
    """Initialize the database and create tables if they don't exist"""
    db_path = 'aruba_migration.db'
    
    # Create a new connection
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check if controllers table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='controllers'")
    table_exists = cursor.fetchone()
    
    if not table_exists:
        print("Creating database tables...")
        
        # Create controllers table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS controllers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address TEXT UNIQUE,
            name TEXT,
            nodepath TEXT,
            model TEXT,
            version TEXT,
            added_on TIMESTAMP
        )
        ''')
        
        # Create lc_clusters table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS lc_clusters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            controller_id INTEGER,
            cluster_name TEXT,
            is_leader BOOLEAN,
            members TEXT,
            added_on TIMESTAMP,
            FOREIGN KEY (controller_id) REFERENCES controllers(id)
        )
        ''')
        
        # Create ap_groups table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS ap_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            controller_id INTEGER,
            name TEXT,
            profile_status TEXT,
            added_on TIMESTAMP,
            FOREIGN KEY (controller_id) REFERENCES controllers(id)
        )
        ''')
        
        # Create ap_types table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS ap_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ap_type TEXT UNIQUE,
            count INTEGER,
            added_on TIMESTAMP
        )
        ''')
        
        conn.commit()
        print("Database tables created successfully.")
    else:
        print("Database exists, tables already created.")
    
    conn.close()

def store_controller(controller_data):
    conn = sqlite3.connect('aruba_migration.db')
    cursor = conn.cursor()
    
    ip_address = controller_data[0]
    name = controller_data[1]
    nodepath = controller_data[2]
    model = controller_data[3]
    version = controller_data[4]
    
    # Check if controller already exists
    cursor.execute('SELECT id FROM controllers WHERE ip_address = ?', (ip_address,))
    result = cursor.fetchone()
    
    if result:
        controller_id = result[0]
        # Update existing controller
        cursor.execute('''
        UPDATE controllers 
        SET name = ?, nodepath = ?, model = ?, version = ?
        WHERE id = ?
        ''', (name, nodepath, model, version, controller_id))
    else:
        # Insert new controller
        cursor.execute('''
        INSERT INTO controllers (ip_address, name, nodepath, model, version, added_on)
        VALUES (?, ?, ?, ?, ?, ?)
        ''', (ip_address, name, nodepath, model, version, datetime.now()))
        controller_id = cursor.lastrowid
    
    conn.commit()
    conn.close()
    
    return controller_id

def store_lc_cluster(controller_id, cluster_info):
    conn = sqlite3.connect('aruba_migration.db')
    cursor = conn.cursor()
    
    # Delete existing entries for this controller
    cursor.execute('DELETE FROM lc_clusters WHERE controller_id = ?', (controller_id,))
    
    # Store new cluster info
    cursor.execute('''
    INSERT INTO lc_clusters (controller_id, cluster_name, is_leader, members, added_on)
    VALUES (?, ?, ?, ?, ?)
    ''', (
        controller_id,
        cluster_info['cluster_name'],
        cluster_info['is_leader'],
        json.dumps(cluster_info['members']),
        datetime.now()
    ))
    
    conn.commit()
    conn.close()
    print(f"Stored cluster information: {cluster_info['cluster_name']} for controller ID {controller_id}")

def store_ap_groups(controller_id, ap_groups_data):
    conn = sqlite3.connect('aruba_migration.db')
    cursor = conn.cursor()
    
    # Delete existing AP groups for this controller
    cursor.execute('DELETE FROM ap_groups WHERE controller_id = ?', (controller_id,))
    
    # Store new AP groups
    if "AP group List" in ap_groups_data:
        for group in ap_groups_data["AP group List"]:
            name = group.get("Name", "Unknown")
            profile_status = group.get("Profile Status")
            
            cursor.execute('''
            INSERT INTO ap_groups (controller_id, name, profile_status, added_on)
            VALUES (?, ?, ?, ?)
            ''', (controller_id, name, profile_status, datetime.now()))
    
    conn.commit()
    conn.close()

def display_database_info():
    conn = sqlite3.connect('aruba_migration.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Get all controllers
    cursor.execute('SELECT * FROM controllers')
    controllers = cursor.fetchall()
    
    if not controllers:
        print("No controllers found in the database.")
        conn.close()
        return
    
    print("\n=== Stored Controller Information ===\n")
    
    for controller in controllers:
        print(f"Controller: {controller['name']} ({controller['ip_address']})")
        print(f"Model: {controller['model']}")
        print(f"Version: {controller['version']}")
        print(f"Nodepath: {controller['nodepath']}")
        
        # Get LC cluster info
        cursor.execute('SELECT * FROM lc_clusters WHERE controller_id = ?', (controller['id'],))
        lc_cluster = cursor.fetchone()
        
        if lc_cluster:
            print("\nLC Cluster Information:")
            print(f"Cluster Name: {lc_cluster['cluster_name']}")
            print(f"Role: {'Leader' if lc_cluster['is_leader'] else 'Member'}")
            members = json.loads(lc_cluster['members'])
            if members:
                print("Cluster Members:")
                for member in members:
                    print(f"  - {member}")
        else:
            print("\nLC Cluster Information: Not Available")
        
        # Get AP groups
        cursor.execute('SELECT * FROM ap_groups WHERE controller_id = ?', (controller['id'],))
        ap_groups = cursor.fetchall()
        
        if ap_groups:
            print("\nAP Groups:")
            ap_group_data = []
            for group in ap_groups:
                ap_group_data.append([group['name'], group['profile_status'] or 'Regular'])
            
            headers = ["Name", "Profile Status"]
            print(tabulate(ap_group_data, headers=headers, tablefmt="grid"))
        else:
            print("\nAP Groups: Not Available")
        
        print("\n" + "="*50 + "\n")
    
    # Display AP type counts if available
    cursor.execute('SELECT * FROM ap_types')
    ap_types = cursor.fetchall()
    
    if ap_types:
        print("\n=== AP Type Distribution ===\n")
        ap_type_data = []
        total_aps = 0
        
        for ap in ap_types:
            ap_type_data.append([ap['ap_type'], ap['count']])
            total_aps += ap['count']
        
        headers = ["AP Type", "Count"]
        print(tabulate(ap_type_data, headers=headers, tablefmt="grid"))
        print(f"\nTotal APs: {total_aps}")
    
    conn.close()

def display_md_switches(md_switches):
    global stored_md_switches
    
    if md_switches:
        headers = ["IP Address", "Name", "Nodepath", "Model", "Version"]
        print("\nMD Controllers:")
        print(tabulate(md_switches, headers=headers, tablefmt="grid"))
        
        # Store MDs for future command use
        stored_md_switches = md_switches
        print(f"\nFound and stored {len(md_switches)} MD controllers for future use.")
    else:
        print("No MD controllers found.")

def collect_mc_credentials():
    global mc_username, mc_password
    print("\nEnter credentials for Mobility Controllers:")
    mc_username = input("MC Username: ")
    mc_password = getpass("MC Password: ")
    return mc_username, mc_password

def ssh_to_mm(ip, username, password):
    """Establish SSH connection to Mobility Conductor"""
    try:
        # Initialize SSH client
        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        print(f"Connecting to {ip} via SSH...")
        ssh_client.connect(hostname=ip, username=username, password=password, timeout=10)
        
        # Get interactive shell
        shell = ssh_client.invoke_shell()
        shell.settimeout(10)
        
        # Wait for initial prompt
        output = read_ssh_output(shell)
        print(output)
        
        return ssh_client, shell
    except Exception as e:
        print(f"SSH connection failed: {str(e)}")
        return None, None

def read_ssh_output(shell, wait_time=1):
    """Read output from SSH shell"""
    time.sleep(wait_time)  # Give time for output to be ready
    output = ""
    # Try to read for up to 5 seconds
    start_time = time.time()
    while time.time() - start_time < 5:
        if shell.recv_ready():
            chunk = shell.recv(4096).decode('utf-8', errors='ignore')
            output += chunk
            # If we got a prompt, we can stop reading
            if "#" in chunk or ">" in chunk:
                break
        else:
            time.sleep(0.1)
    return output

def send_ssh_command(shell, command, wait_time=1):
    """Send command to SSH shell and return output"""
    print(f"Sending command: {command}")
    shell.send(command + "\n")
    time.sleep(wait_time)  # Give time for command to execute
    output = read_ssh_output(shell)
    print(output)
    return output

def get_clusters_for_nodepath(nodepath):
    """Get all cluster names for a specific nodepath"""
    conn = sqlite3.connect('aruba_migration.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Get controller IDs for this nodepath
    cursor.execute('''
    SELECT id FROM controllers WHERE nodepath = ?
    ''', (nodepath,))
    
    controller_ids = [row['id'] for row in cursor.fetchall()]
    
    if not controller_ids:
        conn.close()
        return []
    
    # Get cluster names for these controllers (excluding 'Unknown')
    placeholders = ','.join(['?' for _ in controller_ids])
    cursor.execute(f'''
    SELECT DISTINCT cluster_name FROM lc_clusters 
    WHERE controller_id IN ({placeholders})
    AND cluster_name != 'Unknown' AND cluster_name IS NOT NULL
    ''', controller_ids)
    
    cluster_names = [row['cluster_name'] for row in cursor.fetchall()]
    conn.close()
    return cluster_names

def get_all_cluster_names_including_unknown():
    """Get all cluster names including 'Unknown' ones (for counting purposes)"""
    conn = sqlite3.connect('aruba_migration.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('''
    SELECT DISTINCT cluster_name FROM lc_clusters
    ''')
    
    cluster_names = [row['cluster_name'] for row in cursor.fetchall()]
    conn.close()
    return cluster_names

def get_all_clusters_with_nodepaths():
    """Get all unique clusters with their corresponding nodepaths, excluding 'Unknown' clusters"""
    conn = sqlite3.connect('aruba_migration.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Get cluster info with controller details, excluding 'Unknown' clusters
    cursor.execute('''
    SELECT DISTINCT lc.cluster_name, c.nodepath
    FROM lc_clusters lc
    JOIN controllers c ON lc.controller_id = c.id
    WHERE lc.cluster_name != 'Unknown' AND lc.cluster_name IS NOT NULL
    ''')
    
    clusters_info = [(row['cluster_name'], row['nodepath']) for row in cursor.fetchall()]
    conn.close()
    return clusters_info

def get_all_cluster_names():
    """Get all unique cluster names from the database, excluding 'Unknown' clusters"""
    conn = sqlite3.connect('aruba_migration.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('''
    SELECT DISTINCT cluster_name FROM lc_clusters
    WHERE cluster_name != 'Unknown' AND cluster_name IS NOT NULL
    ''')
    
    cluster_names = [row['cluster_name'] for row in cursor.fetchall()]
    conn.close()
    return cluster_names

def get_nodepath_for_cluster(cluster_name):
    """Get the nodepath for a specific cluster"""
    conn = sqlite3.connect('aruba_migration.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Get controller IDs for this cluster
    cursor.execute('''
    SELECT controller_id FROM lc_clusters WHERE cluster_name = ?
    ''', (cluster_name,))
    
    controller_ids = [row['controller_id'] for row in cursor.fetchall()]
    
    if not controller_ids:
        conn.close()
        return None
    
    # Get nodepath from the first controller (they should all be the same for a cluster)
    cursor.execute('''
    SELECT nodepath FROM controllers WHERE id = ?
    ''', (controller_ids[0],))
    
    result = cursor.fetchone()
    conn.close()
    
    if result:
        return result['nodepath']
    return None

def get_cluster_name_for_controller(controller_name):
    """Get the cluster name for a specific controller by name"""
    conn = sqlite3.connect('aruba_migration.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Get controller ID by name
    cursor.execute('''
    SELECT id FROM controllers WHERE name = ?
    ''', (controller_name,))
    
    result = cursor.fetchone()
    if not result:
        conn.close()
        return None
    
    controller_id = result['id']
    
    # Get cluster name for this controller
    cursor.execute('''
    SELECT cluster_name FROM lc_clusters WHERE controller_id = ?
    ''', (controller_id,))
    
    result = cursor.fetchone()
    conn.close()
    
    if result:
        return result['cluster_name']
    return None

def get_lc_cluster_for_nodepath(nodepath):
    """Get the appropriate LC cluster name for a specific nodepath from the database"""
    conn = sqlite3.connect('aruba_migration.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Get controllers with this nodepath
    cursor.execute('''
    SELECT id FROM controllers WHERE nodepath = ?
    ''', (nodepath,))
    
    controller_ids = [row['id'] for row in cursor.fetchall()]
    
    if not controller_ids:
        conn.close()
        return None
    
    # Find LC clusters for these controllers
    placeholders = ','.join(['?' for _ in controller_ids])
    cursor.execute(f'''
    SELECT DISTINCT cluster_name FROM lc_clusters 
    WHERE controller_id IN ({placeholders})
    ''', controller_ids)
    
    result = cursor.fetchone()
    conn.close()
    
    if result:
        return result['cluster_name']
    return None

def get_available_clusters():
    """Get a list of available clusters from the database, excluding 'Unknown' clusters"""
    clusters = []
    
    conn = sqlite3.connect('aruba_migration.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('''
    SELECT DISTINCT cluster_name FROM lc_clusters
    WHERE cluster_name != 'Unknown' AND cluster_name IS NOT NULL
    ''')
    
    for row in cursor.fetchall():
        clusters.append(row['cluster_name'])
    
    conn.close()
    return clusters

def get_controllers_by_cluster(cluster_name):
    """Get controller information for a specific cluster"""
    controllers = []
    
    conn = sqlite3.connect('aruba_migration.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Get cluster IDs matching the cluster name
    cursor.execute('''
    SELECT controller_id FROM lc_clusters WHERE cluster_name = ?
    ''', (cluster_name,))
    
    controller_ids = [row['controller_id'] for row in cursor.fetchall()]
    
    if not controller_ids:
        print(f"No controllers found for cluster: {cluster_name}")
        conn.close()
        return controllers
    
    # Get controller information
    placeholders = ','.join(['?' for _ in controller_ids])
    cursor.execute(f'''
    SELECT * FROM controllers WHERE id IN ({placeholders})
    ''', controller_ids)
    
    for row in cursor.fetchall():
        controllers.append({
            'id': row['id'],
            'ip_address': row['ip_address'],
            'name': row['name'],
            'nodepath': row['nodepath']
        })
    
    conn.close()
    return controllers

def get_all_controllers():
    """Get all controller information from the database"""
    controllers = []
    
    conn = sqlite3.connect('aruba_migration.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM controllers')
    
    for row in cursor.fetchall():
        controllers.append({
            'id': row['id'],
            'ip_address': row['ip_address'],
            'name': row['name'],
            'nodepath': row['nodepath']
        })
    
    conn.close()
    return controllers

def get_ap_groups_for_controller(controller_id):
    """Get a list of AP groups for a specific controller"""
    ap_groups = []
    
    conn = sqlite3.connect('aruba_migration.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('''
    SELECT name FROM ap_groups WHERE controller_id = ?
    ''', (controller_id,))
    
    for row in cursor.fetchall():
        ap_groups.append(row['name'])
    
    conn.close()
    return ap_groups

def select_cluster():
    """Allow user to select a cluster to work with"""
    global selected_cluster, selected_ap_groups
    
    clusters = get_available_clusters()
    
    if not clusters:
        print("No clusters found in the database. Please run the automated discovery first (option 1).")
        return False
    
    print("\nAvailable Clusters:")
    for i, cluster in enumerate(clusters, 1):
        print(f"{i}. {cluster}")
    
    selection = input("Select cluster number (or 0 to cancel): ")
    
    try:
        selection = int(selection)
        if selection == 0:
            return False
        
        if 1 <= selection <= len(clusters):
            # Clear selected AP groups when changing clusters
            if selected_cluster != clusters[selection - 1]:
                selected_ap_groups = []
                print("Note: AP group selection cleared due to cluster change.")
            
            selected_cluster = clusters[selection - 1]
            controllers = get_controllers_by_cluster(selected_cluster)
            print(f"\nSelected Cluster: {selected_cluster}")
            print(f"Controllers in this cluster: {len(controllers)}")
            for controller in controllers:
                print(f"  - {controller['name']} ({controller['ip_address']})")
            return True
        else:
            print("Invalid selection.")
            return False
            
    except ValueError:
        print("Invalid input. Please enter a number.")
        return False

def execute_ap_convert_init(mc_username, mc_password):
    """Execute the initial AP convert command on all controllers in the selected cluster"""
    global selected_cluster
    
    if not selected_cluster:
        print("No cluster selected. Please select a cluster first.")
        return False
    
    controllers = get_controllers_by_cluster(selected_cluster)
    
    if not controllers:
        print(f"No controllers found for cluster: {selected_cluster}")
        return False
    
    command = "ap convert active specific-aps activate max-downloads 20 no-pre-validation"
    
    success_count = 0
    
    for controller in controllers:
        print(f"\nConnecting to {controller['name']} ({controller['ip_address']})...")
        
        # Connect to controller via SSH
        ssh_client, shell = ssh_to_mm(controller['ip_address'], mc_username, mc_password)
        
        if not ssh_client or not shell:
            print(f"Failed to establish SSH connection to {controller['name']}. Skipping.")
            continue
        
        try:
            print(f"Executing AP convert command on {controller['name']}...")
            
            # Execute the convert command
            output = send_ssh_command(shell, command)
            
            # Check if we got the warning prompt
            if "WARNING:" in output and "Do you want to proceed with the operation? [y/n]:" in output:
                print("Received confirmation prompt. Sending 'y'...")
                output = send_ssh_command(shell, "y")
                print("AP convert command activated.")
                success_count += 1
            else:
                print("Did not receive expected confirmation prompt. The command may have failed.")
            
        except Exception as e:
            print(f"Error during SSH session with {controller['name']}: {str(e)}")
        
        finally:
            # Close SSH connection
            if ssh_client:
                ssh_client.close()
                print(f"SSH connection to {controller['name']} closed.")
    
    print(f"\nAP convert command executed successfully on {success_count} out of {len(controllers)} controllers.")
    return success_count > 0

def select_and_add_ap_group():
    """Select an AP group and add it to the AP convert command on all controllers in the selected cluster"""
    global selected_cluster, selected_ap_groups
    
    if not selected_cluster:
        print("No cluster selected. Please select a cluster first.")
        return False
    
    controllers = get_controllers_by_cluster(selected_cluster)
    
    if not controllers:
        print(f"No controllers found for cluster: {selected_cluster}")
        return False
    
    # Get all AP groups across all controllers in the cluster
    all_ap_groups = set()
    
    for controller in controllers:
        ap_groups = get_ap_groups_for_controller(controller['id'])
        all_ap_groups.update(ap_groups)
    
    if not all_ap_groups:
        print("No AP groups found for the selected cluster.")
        return False
    
    # Filter out already selected AP groups
    available_groups = [group for group in sorted(list(all_ap_groups)) if group not in selected_ap_groups]
    
    if not available_groups:
        print("All available AP groups have already been selected for conversion.")
        print(f"Currently selected AP groups: {', '.join(selected_ap_groups)}")
        return False
    
    # Display available AP groups (excluding already selected ones)
    print("\nAvailable AP Groups (not yet selected):")
    for i, group in enumerate(available_groups, 1):
        print(f"{i}. {group}")
    
    if selected_ap_groups:
        print(f"\n📋 Already Selected AP Groups: {', '.join(selected_ap_groups)}")
    
    selection = input("Select AP group number (or 0 to cancel): ")
    
    try:
        selection = int(selection)
        if selection == 0:
            return False
        
        if 1 <= selection <= len(available_groups):
            selected_group = available_groups[selection - 1]
            
            # ADD CONFIRMATION STEP HERE
            print(f"\n{'='*60}")
            print("CONFIRMATION")
            print(f"{'='*60}")
            print(f"Selected AP Group: {selected_group}")
            print(f"Selected Cluster: {selected_cluster}")
            print(f"Command to execute: ap convert add ap-group {selected_group}")
            print(f"Controllers that will be affected:")
            for controller in controllers:
                print(f"  - {controller['name']} ({controller['ip_address']})")
            
            if selected_ap_groups:
                print(f"\nPreviously selected AP groups: {', '.join(selected_ap_groups)}")
            print(f"{'='*60}")
            
            confirm = input(f"Proceed with adding AP group '{selected_group}' to all controllers? (y/n): ")
            
            if confirm.lower() != 'y':
                print("Operation cancelled.")
                return False
            
            # PROCEED WITH EXECUTION
            command = f"ap convert add ap-group {selected_group}"
            print(f"\nAdding AP group '{selected_group}' to AP convert command on all controllers in cluster {selected_cluster}...")
            
            success_count = 0
            
            for controller in controllers:
                print(f"\nConnecting to {controller['name']} ({controller['ip_address']})...")
                
                # Connect to controller via SSH
                ssh_client, shell = ssh_to_mm(controller['ip_address'], mc_username, mc_password)
                
                if not ssh_client or not shell:
                    print(f"Failed to establish SSH connection to {controller['name']}. Skipping.")
                    continue
                
                try:
                    print(f"Executing command on {controller['name']}: {command}")
                    
                    # Execute the command
                    output = send_ssh_command(shell, command)
                    
                    if "Error" in output or "Invalid" in output:
                        print(f"Command failed on {controller['name']}: {output}")
                    else:
                        print(f"AP group successfully added on {controller['name']}.")
                        success_count += 1
                    
                except Exception as e:
                    print(f"Error during SSH session with {controller['name']}: {str(e)}")
                
                finally:
                    # Close SSH connection
                    if ssh_client:
                        ssh_client.close()
                        print(f"SSH connection to {controller['name']} closed.")
            
            # Add to selected AP groups list if successful on at least one controller
            if success_count > 0:
                selected_ap_groups.append(selected_group)
                print(f"\n✅ AP group '{selected_group}' successfully added to conversion list!")
                print(f"📋 Total selected AP groups: {', '.join(selected_ap_groups)}")
            
            print(f"\nAP group '{selected_group}' added to AP convert command on {success_count} out of {len(controllers)} controllers.")
            return success_count > 0
        else:
            print("Invalid selection.")
            return False
            
    except ValueError:
        print("Invalid input. Please enter a number.")
        return False

def prep_migration_ssh(ip, username, password):
    """Prepare for migration using SSH instead of API"""
    global stored_md_switches, prep_migration_target
    
    if not stored_md_switches:
        print("No MD controllers found. Please run the automated discovery first (option 1).")
        return
    
    # Get unique nodepaths from stored controllers
    nodepaths = set()
    for controller in stored_md_switches:
        nodepaths.add(controller[2])  # nodepath is at index 2
    
    selected_nodepath = None
    if len(nodepaths) == 1:
        selected_nodepath = list(nodepaths)[0]
        print(f"Only one nodepath found: {selected_nodepath}")
        confirm = input("Use this nodepath? (y/n): ")
        if confirm.lower() != 'y':
            print("Operation cancelled.")
            return
    else:
        print("Multiple nodepaths found:")
        nodepath_list = list(nodepaths)
        for i, path in enumerate(nodepath_list, 1):
            print(f"{i}. {path}")
        selection = input("Select nodepath number: ")
        try:
            index = int(selection) - 1
            if 0 <= index < len(nodepath_list):
                selected_nodepath = nodepath_list[index]
            else:
                print("Invalid selection. Aborting.")
                return
        except (ValueError, IndexError):
            print("Invalid selection. Aborting.")
            return
    
    print(f"Selected nodepath: {selected_nodepath}")
    
    # Get all clusters for the selected nodepath
    available_clusters = get_clusters_for_nodepath(selected_nodepath)
    
    if not available_clusters:
        print(f"No LC cluster information found for nodepath {selected_nodepath}.")
        print("Would you like to manually enter the LC cluster name? (y/n)")
        if input().lower() == 'y':
            cluster_name = input("Enter LC cluster name: ")
        else:
            return
    elif len(available_clusters) == 1:
        cluster_name = available_clusters[0]
        print(f"Only one cluster found for this nodepath: {cluster_name}")
        confirm = input("Use this cluster? (y/n): ")
        if confirm.lower() != 'y':
            print("Operation cancelled.")
            return
    else:
        print(f"\nMultiple clusters found for nodepath {selected_nodepath}:")
        for i, cluster in enumerate(available_clusters, 1):
            print(f"{i}. {cluster}")
        selection = input("Select cluster number: ")
        try:
            index = int(selection) - 1
            if 0 <= index < len(available_clusters):
                cluster_name = available_clusters[index]
            else:
                print("Invalid selection. Aborting.")
                return
        except (ValueError, IndexError):
            print("Invalid selection. Aborting.")
            return
    
    print(f"Selected cluster: {cluster_name}")
    print(f"Target: Nodepath '{selected_nodepath}' -> Cluster '{cluster_name}'")
    
    # Final confirmation
    confirm = input(f"\nProceed with disabling LC cluster settings for:\n  Nodepath: {selected_nodepath}\n  Cluster: {cluster_name}\n(y/n): ")
    if confirm.lower() != 'y':
        print("Operation cancelled.")
        return
    
    # Connect to MM via SSH
    ssh_client, shell = ssh_to_mm(ip, username, password)
    if not ssh_client or not shell:
        print("Failed to establish SSH connection. Aborting.")
        return
    
    try:
        print(f"\n=== Starting Prep Migration Process via SSH ===")
        print(f"Target: {selected_nodepath} -> {cluster_name}")
        print("="*60)
        
        # Change to the nodepath
        print(f"[STEP 1/6] Changing to nodepath: {selected_nodepath}")
        output = send_ssh_command(shell, f"change-config-node {selected_nodepath}")
        
        # Enter configuration mode
        print("[STEP 2/6] Entering configuration mode...")
        output = send_ssh_command(shell, "configure terminal")
        
        # First try to enter existing LC cluster profile
        print(f"[STEP 3/6] Entering LC cluster profile: {cluster_name}")
        output = send_ssh_command(shell, f"lc-cluster group-profile {cluster_name}")
        
        # Check if we're in the cluster profile context by looking for various possible prompts
        in_cluster_context = False
        if any(x in output for x in [
            "Classic Controller Cluster Profile", 
            "(lc-cluster-profile)", 
            "(LC-CLUSTER-PROFILE)",
            "lc-cluster-profile"
        ]):
            in_cluster_context = True
            print("✓ Successfully entered LC cluster profile context.")
        
        # If we hit an error, try alternative approaches
        if "Error:" in output or "Invalid" in output or not in_cluster_context:
            print("Would you like to:")
            print("1. List available LC cluster profiles for this nodepath")
            print("2. Try a different cluster name")
            print("3. Abort operation")
            choice = input("Choose an option: ")
            
            if choice == "1":
                # List available profiles for this nodepath
                send_ssh_command(shell, "exit")  # Exit config mode
                print(f"Showing LC cluster information for nodepath {selected_nodepath}:")
                output = send_ssh_command(shell, "show lc-cluster group-membership")
                
                # Extract cluster name from output
                match = re.search(r"Profile Name = ([^\s,]+)", output)
                if match:
                    found_cluster_name = match.group(1)
                    print(f"Found cluster name: {found_cluster_name}")
                    
                    use_found = input(f"Use found cluster '{found_cluster_name}'? (y/n): ")
                    if use_found.lower() == 'y':
                        cluster_name = found_cluster_name
                        
                        # Re-enter config mode
                        send_ssh_command(shell, "configure terminal")
                        output = send_ssh_command(shell, f"lc-cluster group-profile {cluster_name}")
                        
                        if any(x in output for x in [
                            "Classic Controller Cluster Profile", 
                            "(lc-cluster-profile)", 
                            "(LC-CLUSTER-PROFILE)",
                            "lc-cluster-profile"
                        ]):
                            in_cluster_context = True
                            print("✓ Successfully entered LC cluster profile context.")
                    else:
                        raise Exception("User chose not to use found cluster name")
                else:
                    print("Could not find cluster name in output. Aborting.")
                    raise Exception("Could not determine cluster name")
            
            elif choice == "2":
                cluster_name = input("Enter the correct LC cluster name: ")
                output = send_ssh_command(shell, f"lc-cluster group-profile {cluster_name}")
                
                if any(x in output for x in [
                    "Classic Controller Cluster Profile", 
                    "(lc-cluster-profile)", 
                    "(LC-CLUSTER-PROFILE)",
                    "lc-cluster-profile"
                ]):
                    in_cluster_context = True
                    print("✓ Successfully entered LC cluster profile context.")
            
            else:
                raise Exception("Operation aborted by user")
        
        if not in_cluster_context:
            raise Exception("Could not enter LC cluster configuration context after multiple attempts")
        
        # Now we should be in the cluster profile context, proceed with commands
        # Disable active AP load balancing
        print("[STEP 4/6] Disabling active AP load balancing...")
        send_ssh_command(shell, "no active-ap-lb")
        print("✓ Active AP load balancing disabled.")
        
        # Disable redundancy
        print("[STEP 5/6] Disabling redundancy...")
        send_ssh_command(shell, "no redundancy")
        print("✓ Redundancy disabled.")
        
        # Exit LC cluster configuration
        send_ssh_command(shell, "exit")
        
        # Exit configuration mode
        send_ssh_command(shell, "exit")
        
        # Save configuration
        print("[STEP 6/6] Saving configuration...")
        send_ssh_command(shell, "write memory", wait_time=5)  # Longer wait for write memory
        print("✓ Configuration saved.")
        
        # STORE THE PREP MIGRATION TARGET FOR LATER USE IN OPTION 7
        prep_migration_target = {
            'nodepath': selected_nodepath,
            'cluster_name': cluster_name
        }
        print(f"\n📝 Prep migration target saved: {selected_nodepath} -> {cluster_name}")
        
        print(f"\n{'='*60}")
        print("=== Prep Migration Process Complete ===")
        print(f"{'='*60}")
        print(f"Target processed: {selected_nodepath} -> {cluster_name}")
        print("✓ AP load balancing disabled")
        print("✓ Redundancy disabled") 
        print("✓ Configuration saved")
        print("✓ Target information stored for cleanup process")
        print("\nThe cluster is now prepared for migration.")
    
    except Exception as e:
        print(f"✗ Error during SSH session: {str(e)}")
    
    finally:
        # Close SSH connection
        if ssh_client:
            ssh_client.close()
            print("SSH connection closed.")

def cleanup_ap_convert(mc_username, mc_password, mm_ip, mm_username, mm_password):
    """Clean up AP convert configuration and re-enable LC cluster settings on all discovered controllers"""
    global selected_ap_groups, prep_migration_target
    
    controllers = get_all_controllers()
    
    if not controllers:
        print("No controllers found in the database. Please run the automated discovery first (option 1).")
        return False
    
    print(f"\n=== Starting AP Convert Cleanup & LC Cluster Restoration Process ===")
    print(f"This will execute the following commands:")
    print("ON MOBILITY CONTROLLERS:")
    print("1. ap convert clear-all")
    print("2. ap convert cancel")
    print("ON MOBILITY CONDUCTOR:")
    print("3. Re-enable LC cluster redundancy")
    print("4. Re-enable active AP load balancing")  
    print("5. Save configuration (write memory)")
    print(f"\nMobility Controllers to be processed ({len(controllers)}):")
    
    for controller in controllers:
        print(f"  - {controller['name']} ({controller['ip_address']})")
    
    print(f"\nMobility Conductor to be configured: {mm_ip}")
    
    # Show which cluster will be restored based on prep migration target
    if prep_migration_target:
        print(f"\n🎯 LC Cluster Restoration Target (from Option 2):")
        print(f"  Nodepath: {prep_migration_target['nodepath']}")
        print(f"  Cluster: {prep_migration_target['cluster_name']}")
        print("  📝 This matches the cluster that was prepared for migration.")
    else:
        print(f"\n⚠️  No prep migration target found from Option 2.")
        print("  The script will attempt to restore ALL discovered clusters.")
    
    if selected_ap_groups:
        print(f"\nThis will also clear the selected AP groups list: {', '.join(selected_ap_groups)}")
    
    confirm = input(f"\nProceed with cleanup and LC cluster restoration? (y/n): ")
    if confirm.lower() != 'y':
        print("Cleanup cancelled.")
        return False
    
    success_count = 0
    failed_controllers = []
    
    # PHASE 1: Clean up AP convert on all MCs
    print(f"\n{'='*60}")
    print("PHASE 1: AP Convert Cleanup on Mobility Controllers")
    print(f"{'='*60}")
    
    for controller in controllers:
        print(f"\n[MC] Processing: {controller['name']} ({controller['ip_address']})")
        print('-'*50)
        
        # Connect to controller via SSH
        ssh_client, shell = ssh_to_mm(controller['ip_address'], mc_username, mc_password)
        
        if not ssh_client or not shell:
            print(f"Failed to establish SSH connection to {controller['name']}. Skipping.")
            failed_controllers.append(controller['name'])
            continue
        
        try:
            # Step 1: Execute ap convert clear-all command
            print(f"[STEP 1/2] Executing 'ap convert clear-all' on {controller['name']}...")
            output = send_ssh_command(shell, "ap convert clear-all", wait_time=2)
            
            # Check for confirmation prompt
            if "Do you want to proceed with the operation? [y/n]:" in output:
                print("Received confirmation prompt for clear-all. Sending 'y'...")
                output = send_ssh_command(shell, "y", wait_time=2)
                print("Clear-all command executed.")
            else:
                print("Clear-all command completed (no confirmation required).")
            
            # Step 2: Execute ap convert cancel command
            print(f"[STEP 2/2] Executing 'ap convert cancel' on {controller['name']}...")
            output = send_ssh_command(shell, "ap convert cancel", wait_time=2)
            
            # Check for confirmation prompt
            if "Do you want to proceed with the operation? [y/n]:" in output:
                print("Received confirmation prompt for cancel. Sending 'y'...")
                output = send_ssh_command(shell, "y", wait_time=2)
                print("Cancel command executed.")
            else:
                print("Cancel command completed (no confirmation required).")
                
            print(f"✓ AP Convert cleanup completed successfully on {controller['name']}")
            success_count += 1
            
        except Exception as e:
            print(f"✗ Error during SSH session with {controller['name']}: {str(e)}")
            failed_controllers.append(controller['name'])
        
        finally:
            # Close SSH connection
            if ssh_client:
                ssh_client.close()
                print(f"SSH connection to {controller['name']} closed.")
    
    # PHASE 2: Restore LC cluster settings on MM
    print(f"\n{'='*60}")
    print("PHASE 2: LC Cluster Restoration on Mobility Conductor")
    print(f"{'='*60}")
    
    # Determine which clusters to restore based on prep migration target
    if prep_migration_target:
        # Use the specific cluster that was prepared for migration
        clusters_info = [(prep_migration_target['cluster_name'], prep_migration_target['nodepath'])]
        print(f"\n[MM] Processing Mobility Conductor: {mm_ip}")
        print(f"🎯 Restoring SPECIFIC cluster from prep migration:")
        print(f"  - {prep_migration_target['cluster_name']} (nodepath: {prep_migration_target['nodepath']})")
        print("📝 This matches exactly what was disabled in Option 2.")
    else:
        # Fallback to all discovered clusters (old behavior)
        clusters_info = get_all_clusters_with_nodepaths()
        if clusters_info:
            print(f"\n[MM] Processing Mobility Conductor: {mm_ip}")
            print(f"⚠️  No prep migration target found - restoring ALL discovered clusters:")
            for cluster_name, nodepath in clusters_info:
                print(f"  - {cluster_name} (nodepath: {nodepath})")
    
    if not clusters_info:
        print("No valid LC cluster information found for restoration.")
        print("Skipping LC cluster restoration.")
    else:
        print('-'*50)
        
        # Connect to MM via SSH
        mm_ssh_client, mm_shell = ssh_to_mm(mm_ip, mm_username, mm_password)
        
        if not mm_ssh_client or not mm_shell:
            print("Failed to establish SSH connection to Mobility Conductor. Skipping LC cluster restoration.")
        else:
            try:
                lc_restoration_success = False
                
                for cluster_name, nodepath in clusters_info:
                    print(f"\n[STEP 3/5] Restoring LC cluster settings for cluster: {cluster_name}")
                    print(f"Using nodepath: {nodepath}")
                    
                    # Change to the nodepath first
                    print(f"Changing to nodepath: {nodepath}")
                    send_ssh_command(mm_shell, f"change-config-node {nodepath}")
                    
                    # Enter configuration mode
                    print("Entering configuration mode...")
                    send_ssh_command(mm_shell, "configure terminal")
                    
                    # Enter LC cluster profile
                    print(f"Entering LC cluster profile: {cluster_name}")
                    output = send_ssh_command(mm_shell, f"lc-cluster group-profile {cluster_name}")
                    
                    # Check if we're in the cluster profile context
                    in_cluster_context = False
                    if any(x in output for x in [
                        "Classic Controller Cluster Profile", 
                        "(lc-cluster-profile)", 
                        "(LC-CLUSTER-PROFILE)",
                        "lc-cluster-profile"
                    ]):
                        in_cluster_context = True
                        print("✓ Successfully entered LC cluster profile context.")
                    else:
                        print("Checking alternative methods to enter cluster context...")
                        # Try alternative approach - sometimes the profile might exist but output differently
                        print("Attempting to configure redundancy directly...")
                    
                    # Try to configure settings regardless - sometimes the context check fails but commands work
                    try:
                        # Re-enable redundancy
                        print("[STEP 4/5] Re-enabling redundancy...")
                        redundancy_output = send_ssh_command(mm_shell, "redundancy")
                        if "Error" not in redundancy_output and "Invalid" not in redundancy_output:
                            print("✓ Redundancy re-enabled successfully.")
                        else:
                            print(f"⚠️  Redundancy command output: {redundancy_output}")
                        
                        # Re-enable active AP load balancing
                        print("[STEP 4/5] Re-enabling active AP load balancing...")
                        aplb_output = send_ssh_command(mm_shell, "active-ap-lb")
                        if "Error" not in aplb_output and "Invalid" not in aplb_output:
                            print("✓ Active AP load balancing re-enabled successfully.")
                        else:
                            print(f"⚠️  Active AP load balancing command output: {aplb_output}")
                        
                        lc_restoration_success = True
                        
                    except Exception as config_error:
                        print(f"✗ Error configuring cluster settings: {str(config_error)}")
                    
                    # Exit LC cluster configuration
                    print("Exiting LC cluster profile...")
                    send_ssh_command(mm_shell, "exit")
                    
                    # Exit configuration mode  
                    print("Exiting configuration mode...")
                    send_ssh_command(mm_shell, "exit")
                    
                    # CRITICAL: Save configuration at this nodepath before moving to next cluster
                    print(f"[STEP 5/5] Saving configuration for nodepath {nodepath}...")
                    send_ssh_command(mm_shell, "write memory", wait_time=5)
                    print(f"✓ Configuration saved for nodepath {nodepath}")
                    
                    print(f"{'✓' if lc_restoration_success else '⚠️'} LC cluster '{cluster_name}' processing completed.")
                
                # Final save at root level for good measure
                print("\n[FINAL STEP] Performing final configuration save at root level...")
                send_ssh_command(mm_shell, "write memory", wait_time=5)
                print("✓ Final configuration save completed.")
                
            except Exception as e:
                print(f"✗ Error during SSH session with Mobility Conductor: {str(e)}")
            
            finally:
                # Close MM SSH connection
                if mm_ssh_client:
                    mm_ssh_client.close()
                    print("SSH connection to Mobility Conductor closed.")
    
    # Clear selected AP groups list and prep migration target after cleanup
    if success_count > 0:
        selected_ap_groups = []
        prep_migration_target = None  # Clear the prep migration target
        print(f"\n🗑️  Selected AP groups list has been cleared.")
        print(f"🗑️  Prep migration target has been cleared.")
    
    print(f"\n{'='*60}")
    print("=== AP Convert Cleanup & LC Cluster Restoration Summary ===")
    print(f"{'='*60}")
    print(f"Total controllers processed: {len(controllers)}")
    print(f"Successful AP convert cleanups: {success_count}")
    print(f"Failed AP convert cleanups: {len(failed_controllers)}")
    
    if clusters_info:
        if prep_migration_target:
            print(f"LC cluster restored: {prep_migration_target['cluster_name']} (targeted restoration)")
        else:
            print(f"LC clusters processed: {len(clusters_info)} (full restoration)")
            for cluster_name, nodepath in clusters_info:
                print(f"  - {cluster_name}")
    
    if failed_controllers:
        print(f"\nControllers that failed AP convert cleanup:")
        for controller_name in failed_controllers:
            print(f"  - {controller_name}")
        print("\nYou may want to manually clean up these controllers.")
    
    if success_count > 0:
        print(f"\n✓ AP Convert cleanup completed successfully on {success_count} controllers.")
        if clusters_info:
            if prep_migration_target:
                print("✓ LC cluster restoration completed for the SPECIFIC cluster prepared in Option 2.")
            else:
                print("✓ LC cluster restoration attempted on Mobility Conductor.")
            print("\n⚠️  IMPORTANT: Please verify LC cluster settings manually:")
            print("   1. SSH to each controller and run: show lc-cluster group-membership")
            print("   2. Verify 'Redundancy Mode' shows 'On'")
            print("   3. Verify 'AP Load Balancing' shows 'Enabled'")
            print("   4. If settings are still disabled, you may need to manually re-enable them.")
    else:
        print(f"\n✗ No controllers were successfully processed.")
    
    return success_count > 0

def run_all_steps(session, ip, token, uid, mc_username, mc_password):
    """Run steps 1-5 automatically in sequence"""
    print("\n=== Running Complete Discovery Process ===\n")
    
    # Step 1: Fetch and display MD controllers
    print("\n[STEP 1/5] Discovering Mobility Controllers...")
    data = fetch_switch_data(session, ip, token, uid)
    if not data:
        print("Failed to fetch controller data. Aborting.")
        return False
    
    md_switches = filter_md_switches(data)
    if not md_switches:
        print("No MD controllers found. Aborting.")
        return False
    
    display_md_switches(md_switches)
    
    # Store controllers in database
    for controller in md_switches:
        store_controller(controller)
    
    # Set global variable
    global stored_md_switches
    stored_md_switches = md_switches
    
    # Step 2: Get LC cluster information for all MDs
    print("\n[STEP 2/5] Collecting LC Cluster Information...")
    for controller in stored_md_switches:
        controller_ip = controller[0]
        controller_name = controller[1]
        print(f"\nFetching LC cluster info from {controller_name} ({controller_ip})...")
        
        cluster_data = fetch_lc_cluster_info(controller_ip, mc_username, mc_password)
        if cluster_data:
            print(f"Successfully retrieved LC cluster info from {controller_name}")
            cluster_info = parse_lc_cluster_info(cluster_data)
            controller_id = store_controller(controller)
            store_lc_cluster(controller_id, cluster_info)
            print(f"Stored LC cluster info for {controller_name}")
    
    # Step 3: Get AP groups from all MDs
    print("\n[STEP 3/5] Collecting AP Groups...")
    for controller in stored_md_switches:
        controller_ip = controller[0]
        controller_name = controller[1]
        print(f"\nFetching AP groups from {controller_name} ({controller_ip})...")
        
        ap_groups_data = fetch_ap_groups(controller_ip, mc_username, mc_password)
        if ap_groups_data:
            print(f"Successfully retrieved AP groups from {controller_name}")
            controller_id = store_controller(controller)
            store_ap_groups(controller_id, ap_groups_data)
            print(f"Stored AP groups for {controller_name}")
    
    # Step 4: Get AP database information
    print("\n[STEP 4/5] Collecting AP Database Information...")
    print("Fetching AP database from Mobility Conductor...")
    ap_database_data = fetch_ap_database(session, ip, token, uid)
    
    if ap_database_data:
        print("Successfully retrieved AP database.")
        type_counts = count_ap_types(ap_database_data)
        if type_counts:
            print("Counting AP types...")
            store_ap_type_counts(type_counts)
            display_ap_type_counts(type_counts)
        else:
            print("No AP types found in the database.")
    else:
        print("Failed to retrieve AP database.")
    
    # Step 5: Display database information
    print("\n[STEP 5/5] Displaying Collected Information...")
    display_database_info()
    
    print("\n=== Complete Discovery Process Completed ===\n")
    print("To prepare for migration by disabling LC-cluster settings, use option 2 from the main menu.")
    return True

def main():
    print("===================================================")
    print("Welcome to the Access Point Migration Assistant")
    print("Version 1.0 (STABLE)")
    print("Not an offical HPE Aruba Networking product")
    print("===================================================")
    
    # Declare global variables
    global mc_username, mc_password
    
    # Gather all required credentials upfront
    print("\nPlease enter all required credentials:")
    print("\nMobility Conductor (MM) Credentials:")
    mm_ip = input("MM IP: ")
    mm_username = input("MM Username: ")
    mm_password = getpass("MM Password: ")
    
    print("\nMobility Controller (MC) Credentials:")
    mc_username = input("MC Username: ")
    mc_password = getpass("MC Password: ")
    
    # Initialize database
    # Clear and initialize database
    clear_database()
    init_database()
    
    # Login to MM
    session, token, uid = login(mm_ip, mm_username, mm_password)
    if not session:
        return
    
    while True:
        # Display selected cluster if any
        cluster_info = f" (Selected Cluster: {selected_cluster})" if selected_cluster else ""
        
        # Display prep migration target if any
        prep_target_info = ""
        if prep_migration_target:
            prep_target_info = f"\n🎯 Prep Migration Target: {prep_migration_target['nodepath']} -> {prep_migration_target['cluster_name']}"
        
        # Display selected AP groups if any
        ap_groups_info = ""
        if selected_ap_groups:
            if len(selected_ap_groups) == 1:
                ap_groups_info = f"\n📋 Selected AP Group: {selected_ap_groups[0]}"
            else:
                ap_groups_info = f"\n📋 Selected AP Groups ({len(selected_ap_groups)}): {', '.join(selected_ap_groups)}"
        
        print("\nMenu:")
        print("1. Run Complete Discovery Process (Recommended)")
        print("2. Prep Migration (Disable LC-cluster settings)")
        print("3. View Collected Information")
        print("4. Select Cluster for AP Convert" + cluster_info)
        print("5. Initialize AP Convert on Selected Cluster")
        print("6. Add AP Group to AP Convert")
        print("7. Cleanup AP Convert & Restore LC Cluster Settings")
        print("8. Live AP Conversion Monitoring Dashboard")
        print("9. Exit")
        
        if prep_target_info:
            print(prep_target_info)
        if ap_groups_info:
            print(ap_groups_info)
        
        choice = input("Choose an option: ")
        
        if choice == "1":
            run_all_steps(session, mm_ip, token, uid, mc_username, mc_password)
        
        elif choice == "2":
            if not stored_md_switches:
                print("No MD controllers found. Please run the complete discovery process first (option 1).")
                continue
                
            print("\nPreparing for migration by disabling LC-cluster settings...")
            print("This will use SSH to connect to the Mobility Conductor.")
            confirm = input("Continue? (y/n): ")
            if confirm.lower() == 'y':
                mm_password = getpass("Re-enter MM Password: ")
                prep_migration_ssh(mm_ip, mm_username, mm_password)
        
        elif choice == "3":
            display_database_info()
        
        elif choice == "4":
            select_cluster()
        
        elif choice == "5":
            if not selected_cluster:
                print("No cluster selected. Please select a cluster first (option 4).")
                continue
                
            print(f"\nInitializing AP Convert on cluster: {selected_cluster}")
            print("This will execute the AP convert command on all controllers in the selected cluster.")
            confirm = input("Continue? (y/n): ")
            if confirm.lower() == 'y':
                execute_ap_convert_init(mc_username, mc_password)
        
        elif choice == "6":
            if not selected_cluster:
                print("No cluster selected. Please select a cluster first (option 4).")
                continue
                
            select_and_add_ap_group()
        
        elif choice == "7":
            print("\n" + "="*60)
            print("AP Convert Cleanup & LC Cluster Restoration")
            print("="*60)
            print("This will execute the following commands:")
            print("ON MOBILITY CONTROLLERS:")
            print("• ap convert clear-all    - Removes all AP groups from conversion")
            print("• ap convert cancel       - Cancels any active conversion process")
            print("ON MOBILITY CONDUCTOR:")
            print("• redundancy              - Re-enables LC cluster redundancy")
            print("• active-ap-lb            - Re-enables active AP load balancing")
            print("• write memory            - Saves configuration to memory")
            print("\nThis action will:")
            print("1. Reset AP convert configuration to default state")
            print("2. Restore LC cluster settings that were disabled for migration")
            print("3. Return controllers to normal operational state")
            
            cleanup_ap_convert(mc_username, mc_password, mm_ip, mm_username, mm_password)
        
        elif choice == "8":
            start_monitoring_dashboard()

        elif choice == "9":  # Update this from "8"
            print("Exiting...")
            break
        
        else:
            print("Invalid option. Please try again.")

if __name__ == "__main__":
    main()