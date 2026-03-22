"""
Shared phone number utilities for the softphone backend.
Provides normalize_phone_number() and get_contact_name() used across
address_book, app, incoming, messaging, and voicemails modules.
"""

import re
from database import get_db_connection


def normalize_phone_number(phone):
    """Normalize phone number to E.164 format (+1XXXXXXXXXX)"""
    if not phone:
        return None

    # Remove all non-digit characters except +
    cleaned = re.sub(r"[^\d+]", "", str(phone))

    # Handle different formats
    if cleaned.startswith("+1"):
        return cleaned if len(cleaned) == 12 else None
    elif cleaned.startswith("1") and len(cleaned) == 11:
        return "+" + cleaned
    elif len(cleaned) == 10:
        return "+1" + cleaned
    elif cleaned.startswith("+") and len(cleaned) == 12:
        return cleaned

    return None


def get_contact_name(phone_number):
    """Get contact name by looking up phone number in NovaCore customers table."""
    try:
        from novacore_contacts import get_contact_name as nc_get_contact_name
        name = nc_get_contact_name(phone_number)
        return None if name == "Unknown Contact" else name
    except Exception as e:
        print(f"❌ Error getting contact name for {phone_number}: {e}")
        return None
