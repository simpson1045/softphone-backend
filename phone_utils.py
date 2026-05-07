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
    """Get contact name for the current tenant.

    PC Reps tenant queries NovaCore customers; HaniTech (and future native
    tenants) query the local softphone.contacts table. Dispatch is handled
    by contact_provider based on tenants.contact_provider.
    """
    try:
        from contact_provider import get_contact_name as cp_get_contact_name
        name = cp_get_contact_name(phone_number)
        return None if name == "Unknown Contact" else name
    except Exception as e:
        print(f"❌ Error getting contact name for {phone_number}: {e}")
        return None
