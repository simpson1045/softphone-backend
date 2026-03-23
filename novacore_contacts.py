"""
NovaCore Contacts Module
Centralized interface for querying customer data from the NovaCore database.
Replaces the local softphone contacts table for all read operations.
"""

import os
import re
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()


def get_novacore_connection():
    """Get a database connection to the NovaCore PostgreSQL database."""
    password = os.getenv("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError("POSTGRES_PASSWORD environment variable is not set")
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", 5432)),
        database="novacore",
        user=os.getenv("POSTGRES_USER", "postgres"),
        password=password,
        cursor_factory=RealDictCursor,
    )


def _strip_to_digits(phone_str):
    """Strip a phone string down to just its last 10 digits for comparison."""
    if not phone_str:
        return None
    digits = re.sub(r"[^0-9]", "", phone_str)
    if len(digits) >= 10:
        return digits[-10:]
    return digits if digits else None


def _build_display_name(row):
    """Build a display name from a NovaCore customer row."""
    first = (row.get("first_name") or "").strip()
    last = (row.get("last_name") or "").strip()
    name = f"{first} {last}".strip()
    if not name:
        name = (row.get("business_name") or "").strip()
    return name or None


def _pick_phones(row):
    """
    Pick the two best phone numbers from a NovaCore customer row.
    Priority: mobile_phone > phone > mobile > home_phone > office_phone > other_phone
    Returns (phone_primary, phone_secondary).
    """
    candidates = []
    for field in ["mobile_phone", "phone", "mobile", "home_phone", "office_phone", "other_phone"]:
        val = (row.get(field) or "").strip()
        if val and val not in candidates:
            candidates.append(val)

    primary = candidates[0] if len(candidates) > 0 else None
    secondary = candidates[1] if len(candidates) > 1 else None
    return primary, secondary


def _build_address(row):
    """Concatenate NovaCore address fields into a single string."""
    parts = []
    for field in ["address", "address_2"]:
        val = (row.get(field) or "").strip()
        if val:
            parts.append(val)

    city = (row.get("city") or "").strip()
    state = (row.get("state") or "").strip()
    zip_code = (row.get("zip") or "").strip()

    city_state_zip = ""
    if city and state:
        city_state_zip = f"{city}, {state}"
    elif city:
        city_state_zip = city
    elif state:
        city_state_zip = state

    if zip_code:
        city_state_zip = f"{city_state_zip} {zip_code}".strip()

    if city_state_zip:
        parts.append(city_state_zip)

    return ", ".join(parts) if parts else None


def _row_to_contact(row):
    """
    Transform a NovaCore customers row into the contact dict shape
    that the softphone frontend expects.
    """
    phone_primary, phone_secondary = _pick_phones(row)
    return {
        "id": row["id"],
        "novacore_id": row["id"],
        "name": _build_display_name(row),
        "first_name": (row.get("first_name") or "").strip(),
        "last_name": (row.get("last_name") or "").strip(),
        "phone_primary": phone_primary,
        "phone_secondary": phone_secondary,
        "email": (row.get("email") or "").strip() or None,
        "address": _build_address(row),
        "company": (row.get("business_name") or "").strip() or None,
        "notes": (row.get("notes") or "").strip() or None,
        # opt_out_sms and sms_capable from NovaCore, suppress_auto_sms from local table
        "suppress_auto_sms": False,
        "opted_out_sms": bool(row.get("opt_out_sms")) if row.get("opt_out_sms") is not None else False,
        "sms_capable": bool(row.get("sms_capable")) if row.get("sms_capable") is not None else None,
    }


def fetch_all_customers():
    """
    Fetch all active customers from NovaCore, returning them in the
    contact dict shape the softphone frontend expects.
    """
    conn = get_novacore_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, first_name, last_name, email,
                   phone, mobile_phone, home_phone, office_phone, other_phone, mobile,
                   business_name, address, address_2, city, state, zip, notes,
                   opt_out_sms, sms_capable
            FROM customers
            WHERE disabled IS NOT TRUE
            ORDER BY
                COALESCE(NULLIF(last_name, ''), NULLIF(business_name, ''), '') ASC,
                COALESCE(NULLIF(first_name, ''), '') ASC
        """)
        rows = cursor.fetchall()
        return [_row_to_contact(row) for row in rows]
    finally:
        conn.close()


# SQL fragment for normalized 10-digit phone comparison
_PHONE_NORM_SQL = "RIGHT(REGEXP_REPLACE(COALESCE({col}, ''), '[^0-9]', '', 'g'), 10)"
_PHONE_COLUMNS = ["phone", "mobile_phone", "home_phone", "office_phone", "other_phone", "mobile"]


def find_customer_by_phone(phone_number):
    """
    Look up a NovaCore customer by phone number.
    Searches all 6 phone columns on the customers table plus the phones table.
    Returns a contact dict or None.
    """
    digits = _strip_to_digits(phone_number)
    if not digits:
        return None

    conn = get_novacore_connection()
    try:
        cursor = conn.cursor()

        # Build WHERE clause checking all phone columns
        conditions = []
        params = []
        for col in _PHONE_COLUMNS:
            conditions.append(f"{_PHONE_NORM_SQL.format(col=col)} = %s")
            params.append(digits)

        query = f"""
            SELECT id, first_name, last_name, email,
                   phone, mobile_phone, home_phone, office_phone, other_phone, mobile,
                   business_name, address, address_2, city, state, zip, notes,
                   opt_out_sms, sms_capable
            FROM customers
            WHERE disabled IS NOT TRUE
              AND ({' OR '.join(conditions)})
            LIMIT 1
        """
        cursor.execute(query, params)
        row = cursor.fetchone()

        if row:
            return _row_to_contact(row)

        # Fallback: check the phones table
        cursor.execute(f"""
            SELECT c.id, c.first_name, c.last_name, c.email,
                   c.phone, c.mobile_phone, c.home_phone, c.office_phone, c.other_phone, c.mobile,
                   c.business_name, c.address, c.address_2, c.city, c.state, c.zip, c.notes,
                   c.opt_out_sms, c.sms_capable
            FROM phones p
            JOIN customers c ON c.id = p.customer_id
            WHERE c.disabled IS NOT TRUE
              AND {_PHONE_NORM_SQL.format(col='p.phone')} = %s
            LIMIT 1
        """, (digits,))
        row = cursor.fetchone()

        if row:
            return _row_to_contact(row)

        return None
    finally:
        conn.close()


def bulk_resolve_names(phone_numbers):
    """
    Resolve a list of phone numbers to display names in a single query.
    Returns a dict of {normalized_phone: display_name}.
    """
    if not phone_numbers:
        return {}

    # Normalize all numbers to 10-digit
    digit_to_original = {}
    for pn in phone_numbers:
        digits = _strip_to_digits(pn)
        if digits:
            digit_to_original[digits] = pn

    if not digit_to_original:
        return {}

    digit_list = list(digit_to_original.keys())

    conn = get_novacore_connection()
    try:
        cursor = conn.cursor()

        # Build a single query that checks all phone columns against all numbers
        placeholders = ",".join(["%s"] * len(digit_list))
        conditions = []
        for col in _PHONE_COLUMNS:
            conditions.append(f"{_PHONE_NORM_SQL.format(col=col)} IN ({placeholders})")

        query = f"""
            SELECT id, first_name, last_name, business_name,
                   phone, mobile_phone, home_phone, office_phone, other_phone, mobile
            FROM customers
            WHERE disabled IS NOT TRUE
              AND ({' OR '.join(conditions)})
        """
        # Each condition needs the full digit_list
        params = digit_list * len(_PHONE_COLUMNS)
        cursor.execute(query, params)
        rows = cursor.fetchall()

        # Build result mapping: for each customer row, figure out which
        # of our input numbers it matches
        result = {}
        for row in rows:
            name = _build_display_name(row)
            if not name:
                continue
            # Check which of our search digits match this customer
            for col in _PHONE_COLUMNS:
                col_digits = _strip_to_digits(row.get(col))
                if col_digits and col_digits in digit_to_original:
                    original_phone = digit_to_original[col_digits]
                    result[original_phone] = name

        return result
    finally:
        conn.close()


def get_contact_name(phone_number):
    """
    Resolve a phone number to a display name via NovaCore.
    Returns the name string, or None if not found.
    """
    customer = find_customer_by_phone(phone_number)
    if customer and customer.get("name"):
        return customer["name"]
    return None


def search_customers(query, limit=20):
    """
    Search NovaCore customers by name, company, email, or phone number.
    Returns a list of contact dicts.
    """
    if not query or not query.strip():
        return []

    search_term = f"%{query.strip()}%"
    digits = re.sub(r"[^0-9]", "", query.strip())

    conn = get_novacore_connection()
    try:
        cursor = conn.cursor()

        # Build conditions for text search
        conditions = [
            "first_name ILIKE %s",
            "last_name ILIKE %s",
            "business_name ILIKE %s",
            "email ILIKE %s",
            "(first_name || ' ' || last_name) ILIKE %s",
        ]
        params = [search_term, search_term, search_term, search_term, search_term]

        # If the query has digits, also search phone columns
        if digits:
            for col in _PHONE_COLUMNS:
                conditions.append(
                    f"REGEXP_REPLACE(COALESCE({col}, ''), '[^0-9]', '', 'g') LIKE %s"
                )
                params.append(f"%{digits}%")

        query_sql = f"""
            SELECT id, first_name, last_name, email,
                   phone, mobile_phone, home_phone, office_phone, other_phone, mobile,
                   business_name, address, address_2, city, state, zip, notes,
                   opt_out_sms, sms_capable
            FROM customers
            WHERE disabled IS NOT TRUE
              AND ({' OR '.join(conditions)})
            ORDER BY last_name, first_name
            LIMIT %s
        """
        params.append(limit)

        cursor.execute(query_sql, params)
        rows = cursor.fetchall()
        return [_row_to_contact(row) for row in rows]
    finally:
        conn.close()
