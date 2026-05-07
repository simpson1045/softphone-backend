"""
Contact provider dispatcher.

Each tenant declares a `contact_provider` in the `tenants` table:
    'novacore' — PC Reps. Reads from the NovaCore customers DB.
    'native'   — HaniTech and any future tenant without an external CRM.
                 Reads from the softphone-local `contacts` table, scoped
                 by tenant_id.

This module mirrors the public surface of `novacore_contacts` so callers
can import from here and get the right backend for the current tenant
without knowing which one is in play.

Public functions (signatures match novacore_contacts.py):
    fetch_all_customers()
    find_customer_by_phone(phone_number)
    bulk_resolve_names(phone_numbers)
    get_contact_name(phone_number)
    search_customers(query, limit=20)

For PC-Reps-only operations (e.g. NovaCore ticket URL lookup), import
directly from novacore_contacts — those are intentionally not part of
this dispatcher.
"""

import re

from database import get_db_connection
from tenant_context import current_tenant, current_tenant_id

# Re-export _strip_to_digits since it's stateless and used elsewhere.
from novacore_contacts import _strip_to_digits  # noqa: F401


def _provider() -> str:
    """Resolve the contact provider name for the current request."""
    try:
        return current_tenant()["contact_provider"]
    except Exception as e:
        print(f"⚠️ contact_provider lookup failed, defaulting to 'native': {e}")
        return "native"


# ───────────────────────── public dispatcher ─────────────────────────

def fetch_all_customers():
    if _provider() == "novacore":
        from novacore_contacts import fetch_all_customers as nc_fetch
        return nc_fetch()
    return _native_fetch_all_customers(current_tenant_id())


def find_customer_by_phone(phone_number):
    if _provider() == "novacore":
        from novacore_contacts import find_customer_by_phone as nc_find
        return nc_find(phone_number)
    return _native_find_customer_by_phone(current_tenant_id(), phone_number)


def bulk_resolve_names(phone_numbers):
    if _provider() == "novacore":
        from novacore_contacts import bulk_resolve_names as nc_bulk
        return nc_bulk(phone_numbers)
    return _native_bulk_resolve_names(current_tenant_id(), phone_numbers)


def get_contact_name(phone_number):
    if _provider() == "novacore":
        from novacore_contacts import get_contact_name as nc_name
        return nc_name(phone_number)
    customer = _native_find_customer_by_phone(current_tenant_id(), phone_number)
    if customer and customer.get("name"):
        return customer["name"]
    return None


def search_customers(query, limit=20):
    if _provider() == "novacore":
        from novacore_contacts import search_customers as nc_search
        return nc_search(query, limit)
    return _native_search_customers(current_tenant_id(), query, limit)


# ───────────────────────── native (local contacts table) ─────────────────────────
#
# The local `contacts` table schema (from migrate_to_postgres + Phase 1b):
#   id, name, phone_primary, phone_secondary, company, email, address,
#   sms_capable, opted_out_sms, suppress_auto_sms, flag_type_id,
#   rs_customer_id (legacy), created_at, updated_at, phone, notes,
#   repairshopr_id (legacy), tenant_id (added Phase 1b)
#
# Native contacts return the same dict shape as novacore_contacts._row_to_contact
# so the frontend doesn't need to care which backend produced the row.


def _native_row_to_contact(row):
    """Transform a softphone.contacts row into the standard contact dict."""
    return {
        "id": row["id"],
        "novacore_id": None,
        "name": (row.get("name") or "").strip() or None,
        "first_name": "",
        "last_name": "",
        "phone_primary": row.get("phone_primary") or row.get("phone") or None,
        "phone_secondary": row.get("phone_secondary") or None,
        "email": (row.get("email") or "").strip() or None,
        "address": (row.get("address") or "").strip() or None,
        "company": (row.get("company") or "").strip() or None,
        "notes": (row.get("notes") or "").strip() or None,
        "suppress_auto_sms": bool(row.get("suppress_auto_sms") or 0),
        "opted_out_sms": bool(row.get("opted_out_sms") or 0),
        "sms_capable": bool(row.get("sms_capable") or 1),
    }


_NATIVE_PHONE_NORM = "RIGHT(REGEXP_REPLACE(COALESCE({col}, ''), '[^0-9]', '', 'g'), 10)"
_NATIVE_PHONE_COLS = ["phone_primary", "phone_secondary", "phone"]


def _native_fetch_all_customers(tenant_id):
    """List all contacts for a tenant, alphabetized."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, name, phone_primary, phone_secondary, phone,
                   company, email, address, notes,
                   sms_capable, opted_out_sms, suppress_auto_sms
            FROM contacts
            WHERE tenant_id = %s
            ORDER BY COALESCE(NULLIF(name, ''), '') ASC
            """,
            (tenant_id,),
        )
        return [_native_row_to_contact(r) for r in cur.fetchall()]


def _native_find_customer_by_phone(tenant_id, phone_number):
    digits = _strip_to_digits(phone_number)
    if not digits:
        return None
    conditions = [f"{_NATIVE_PHONE_NORM.format(col=c)} = %s" for c in _NATIVE_PHONE_COLS]
    sql = f"""
        SELECT id, name, phone_primary, phone_secondary, phone,
               company, email, address, notes,
               sms_capable, opted_out_sms, suppress_auto_sms
        FROM contacts
        WHERE tenant_id = %s
          AND ({' OR '.join(conditions)})
        LIMIT 1
    """
    params = [tenant_id] + [digits] * len(_NATIVE_PHONE_COLS)
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        return _native_row_to_contact(row) if row else None


def _native_bulk_resolve_names(tenant_id, phone_numbers):
    if not phone_numbers:
        return {}
    digit_to_original = {}
    for pn in phone_numbers:
        d = _strip_to_digits(pn)
        if d:
            digit_to_original[d] = pn
    if not digit_to_original:
        return {}

    digit_list = list(digit_to_original.keys())
    placeholders = ",".join(["%s"] * len(digit_list))
    conditions = [
        f"{_NATIVE_PHONE_NORM.format(col=c)} IN ({placeholders})"
        for c in _NATIVE_PHONE_COLS
    ]
    sql = f"""
        SELECT id, name, phone_primary, phone_secondary, phone
        FROM contacts
        WHERE tenant_id = %s AND ({' OR '.join(conditions)})
    """
    params = [tenant_id] + digit_list * len(_NATIVE_PHONE_COLS)

    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()

    result = {}
    for row in rows:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        for col in _NATIVE_PHONE_COLS:
            cd = _strip_to_digits(row.get(col))
            if cd and cd in digit_to_original:
                result[digit_to_original[cd]] = name
    return result


def _native_search_customers(tenant_id, query, limit=20):
    if not query or not query.strip():
        return []
    search_term = f"%{query.strip()}%"
    digits = re.sub(r"[^0-9]", "", query.strip())

    conditions = [
        "name ILIKE %s",
        "company ILIKE %s",
        "email ILIKE %s",
    ]
    params = [search_term, search_term, search_term]
    if digits:
        for col in _NATIVE_PHONE_COLS:
            conditions.append(
                f"REGEXP_REPLACE(COALESCE({col}, ''), '[^0-9]', '', 'g') LIKE %s"
            )
            params.append(f"%{digits}%")

    sql = f"""
        SELECT id, name, phone_primary, phone_secondary, phone,
               company, email, address, notes,
               sms_capable, opted_out_sms, suppress_auto_sms
        FROM contacts
        WHERE tenant_id = %s AND ({' OR '.join(conditions)})
        ORDER BY name
        LIMIT %s
    """
    full_params = [tenant_id] + params + [limit]

    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, full_params)
        return [_native_row_to_contact(r) for r in cur.fetchall()]
