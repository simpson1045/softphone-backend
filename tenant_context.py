"""
Tenant context for multi-tenant query scoping.

Every DB query against user-data tables (messages, call_log, voicemails,
contacts, etc.) must filter by `current_tenant_id()`. This module is the
single source of truth for "which tenant is this request for?"

Resolution order:
    1. Flask `g.tenant_id` if set by middleware (Phase 3 webhook router
       sets this from the inbound `To` number)
    2. `current_user.tenant_id` if authenticated (Phase 2 adds this field
       to the User class)
    3. Fallback: the `pc_reps` tenant (preserves single-tenant behavior
       during the bridge migration)

The pc_reps fallback is intentional during the bridge: it lets Phase 1c
retrofits land and behave correctly even before Phase 2 (auth) and
Phase 3 (webhook routing) wire up explicit tenant resolution. Once
those phases ship, the fallback only fires for genuinely tenant-agnostic
requests (e.g., health-check endpoints).

Usage:
    from tenant_context import current_tenant_id
    cur.execute("SELECT * FROM messages WHERE tenant_id = ?", (current_tenant_id(),))
"""

import threading
from functools import lru_cache
from flask import g, has_request_context
from flask_login import current_user
from database import get_db_connection


# Thread-local override for background threads spawned from a request.
# When a request handler kicks off a daemon thread (e.g. delayed_auto_sms),
# it should call set_thread_tenant_id() at the top of the thread function
# so current_tenant_id() returns the right value inside the thread (which
# has no Flask request context to read g/current_user from).
_thread_local = threading.local()


def set_thread_tenant_id(tenant_id):
    """Pin the tenant for the current OS thread. Call from background thread entry."""
    _thread_local.tenant_id = tenant_id


def clear_thread_tenant_id():
    """Clear the thread-local tenant override. Call in a finally block."""
    _thread_local.tenant_id = None


@lru_cache(maxsize=1)
def _default_tenant_id() -> int:
    """Resolve the pc_reps tenant id once, cached for process lifetime."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM tenants WHERE slug = 'pc_reps'")
        row = cur.fetchone()
        if not row:
            raise RuntimeError(
                "tenants table is missing the pc_reps row — has migrate_tenants.py run?"
            )
        return row["id"]


def current_tenant_id() -> int:
    """Tenant ID for the current request. Falls back to pc_reps."""
    if has_request_context():
        tid = getattr(g, "tenant_id", None)
        if tid is not None:
            return tid
        if current_user.is_authenticated:
            tid = getattr(current_user, "tenant_id", None)
            if tid is not None:
                return tid

    # Background-thread override (set by set_thread_tenant_id when a request
    # handler spawns a daemon thread that needs to keep the tenant context).
    tid = getattr(_thread_local, "tenant_id", None)
    if tid is not None:
        return tid

    return _default_tenant_id()


_TENANT_COLS = "id, slug, name, phone_number, contact_provider, logo_url, color"


@lru_cache(maxsize=8)
def tenant_by_id(tenant_id: int) -> dict:
    """
    Return the tenants-table row for `tenant_id` as a dict, with keys:
        id, slug, name, phone_number, contact_provider, logo_url, color
    Cached because tenants change rarely. Clear via tenant_by_id.cache_clear()
    if you ever update branding live without restarting.
    """
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_TENANT_COLS} FROM tenants WHERE id = ?",
            (tenant_id,),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"no tenant with id={tenant_id}")
        return dict(row)


def tenant_by_phone(phone_number: str) -> dict | None:
    """
    Look up a tenant by its main phone number. Used by the inbound webhook
    router (Phase 3) to resolve `To` → tenant. Returns None if no match.
    """
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_TENANT_COLS} FROM tenants WHERE phone_number = ?",
            (phone_number,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def tenant_id_for_employee_id(employee_id: str) -> int | None:
    """
    Look up a tenant by a Twilio client identity (employee_id).

    Used for outbound webhooks like /call/flow where Twilio sends
    `From=client:<employee_id>` and we need to figure out which tenant
    the originating user belongs to. Checks softphone_users first
    (HaniTech and future tenants), then falls through to NovaCore
    users (PC Reps).

    Returns the tenant id, or None if no user matches.
    """
    if not employee_id:
        return None

    # softphone_users (HaniTech etc.)
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT tenant_id FROM softphone_users WHERE employee_id = ?",
            (employee_id,),
        )
        row = cur.fetchone()
        if row:
            return row["tenant_id"]

    # NovaCore (PC Reps). Lazy import to avoid circular dep.
    try:
        from auth import get_novacore_db_connection, _resolve_pc_reps_tenant_id
        nc_conn = get_novacore_db_connection()
        try:
            nc_cur = nc_conn.cursor()
            nc_cur.execute(
                "SELECT 1 FROM users WHERE employee_id = %s",
                (employee_id,),
            )
            if nc_cur.fetchone():
                return _resolve_pc_reps_tenant_id()
        finally:
            nc_conn.close()
    except Exception as e:
        print(f"⚠️ NovaCore lookup failed in tenant_id_for_employee_id: {e}")

    return None


def current_tenant() -> dict:
    """Convenience: full tenant row for current request."""
    return tenant_by_id(current_tenant_id())
