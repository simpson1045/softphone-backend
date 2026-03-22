import sqlite3
import re
from datetime import datetime
from config import MASTER_DB

def normalize_phone_number(phone):
    """Normalize phone number to E.164 format (+1XXXXXXXXXX)"""
    if not phone:
        return None
    
    # Remove all non-digit characters except +
    cleaned = re.sub(r'[^\d+]', '', str(phone))
    
    # Handle different formats
    if cleaned.startswith('+1'):
        return cleaned if len(cleaned) == 12 else None
    elif cleaned.startswith('1') and len(cleaned) == 11:
        return '+' + cleaned
    elif len(cleaned) == 10:
        return '+1' + cleaned
    elif cleaned.startswith('+') and len(cleaned) == 12:
        return cleaned
    
    return None

def get_contact_name(phone_number):
    """Get contact name by looking up phone number in NovaCore customers table."""
    try:
        from novacore_contacts import get_contact_name as nc_get_contact_name
        return nc_get_contact_name(phone_number)
    except Exception as e:
        print(f"❌ Error getting contact name for {phone_number}: {e}")
        return None

def log_call(to_number, from_number, status, call_type="voice", caller_name=None):
    """UPDATED: Log call to master database with proper normalization"""
    try:
        normalized_phone = normalize_phone_number(from_number)
        if not normalized_phone:
            print(f"⚠️ Skipping call log - invalid phone: {from_number}")
            return
        
        # Get contact name if not provided
        if not caller_name:
            caller_name = get_contact_name(normalized_phone)
        
        with sqlite3.connect(MASTER_DB) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO call_log (
                    phone_number, direction, status, call_type, 
                    caller_name, timestamp
                ) VALUES (?, 'inbound', ?, ?, ?, ?)
            """, (
                normalized_phone,
                status,
                call_type,
                caller_name,
                datetime.utcnow().isoformat()
            ))
            conn.commit()
            
            return cursor.lastrowid
    except Exception as e:
        print(f"❌ Error logging call: {e}")
        return None