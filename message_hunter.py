#!/usr/bin/env python3
"""
Check the network database location where production messages might be stored
"""

import os
import sqlite3
from datetime import datetime

def check_network_location():
    """Check if we can access the network database location"""
    print("🌐 CHECKING NETWORK DATABASE LOCATION")
    print("=" * 50)
    
    network_base = r"\\192.168.1.100\pc-reps\PC Reps\softphone"
    
    print(f"Checking: {network_base}")
    
    if not os.path.exists(network_base):
        print("❌ Network location not accessible")
        print("Possible reasons:")
        print("- Network share is not mounted")
        print("- You don't have permissions")
        print("- The server is offline")
        print("- VPN required")
        return False
    
    print("✅ Network location is accessible!")
    
    # Check for database files
    db_files = [
        'messages.sqlite3',
        'messages.db',
        'contacts.sqlite3',
        'call_log.sqlite3'
    ]
    
    for filename in db_files:
        file_path = os.path.join(network_base, filename)
        
        print(f"\n📁 {filename}")
        print("-" * 25)
        
        if os.path.exists(file_path):
            size = os.path.getsize(file_path)
            modified = datetime.fromtimestamp(os.path.getmtime(file_path))
            
            print(f"✅ Exists: {size:,} bytes")
            print(f"Modified: {modified}")
            
            # Check if it's a messages database
            if 'message' in filename.lower():
                check_message_database(file_path)
                
        else:
            print("❌ Does not exist")
    
    return True

def check_message_database(file_path):
    """Check what's in a messages database"""
    try:
        conn = sqlite3.connect(file_path)
        cursor = conn.cursor()
        
        # Get tables
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = cursor.fetchall()
        
        for table_name, in tables:
            if 'message' in table_name.lower():
                cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
                count = cursor.fetchone()[0]
                print(f"💬 {table_name}: {count} messages")
                
                if count > 0:
                    # Get most recent message
                    cursor.execute(f"SELECT * FROM {table_name} ORDER BY timestamp DESC LIMIT 1")
                    recent = cursor.fetchone()
                    if recent:
                        print(f"📅 Most recent: {recent}")
        
        conn.close()
        
    except Exception as e:
        print(f"❌ Error checking database: {e}")

def check_production_api():
    """Get more details from the production API"""
    print(f"\n🌐 PRODUCTION API DETAILS")
    print("=" * 30)
    
    import requests
    
    try:
        # Get a specific thread to see the data structure
        response = requests.get("https://softphone.pc-reps.com/messages/threads", timeout=10)
        
        if response.status_code == 200:
            threads = response.json()
            
            print(f"📊 Total threads: {len(threads)}")
            
            # Show most recent threads
            print("\n🔥 Most recent threads:")
            for i, thread in enumerate(threads[:5]):
                timestamp = thread.get('latest_timestamp', 'Unknown')
                phone = thread.get('phone_number', 'Unknown')
                message = thread.get('latest_message', '')[:50]
                
                print(f"  {i+1}. {timestamp} - {phone}")
                print(f"     {message}...")
                
                # Check if this is very recent (last 24 hours)
                if '2025-07' in timestamp:
                    print("     🚨 VERY RECENT MESSAGE!")
        
    except Exception as e:
        print(f"❌ Error checking production API: {e}")

if __name__ == "__main__":
    print("🔍 NETWORK DATABASE DETECTIVE")
    print("=" * 60)
    
    network_accessible = check_network_location()
    
    if not network_accessible:
        print(f"\n💡 ALTERNATIVE APPROACH")
        print("=" * 25)
        print("Since network location isn't accessible, you can:")
        print("1. Export messages from the production web app")
        print("2. Use the Twilio API to fetch all messages")
        print("3. Access the production server directly")
        print("4. Copy the production database files")
    
    check_production_api()
    
    print(f"\n🎯 CONCLUSION")
    print("=" * 15)
    print("Your production Softphone app has 130+ current message threads,")
    print("but your local databases only have old data up to May 24th.")
    print("The production app is likely using:")
    print("- Network databases (\\\\192.168.1.100\\...)")
    print("- Or a different local path")
    print("- Or fetching directly from Twilio API")