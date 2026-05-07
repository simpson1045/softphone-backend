"""
Address Book Blueprint

Reads via the contact_provider dispatcher so each tenant hits the right
backend:
    pc_reps tenant   -> NovaCore customers DB (read-only here, managed in NovaCore)
    hanitech tenant  -> local softphone.contacts table (full CRUD)

SMS preferences and flags remain in the local softphone database for both.
"""

from flask import Blueprint, request, jsonify
from database import get_db_connection
# Dispatch through contact_provider so HaniTech doesn't see NovaCore customers.
# _strip_to_digits is provider-agnostic and re-exported from novacore_contacts.
from contact_provider import fetch_all_customers, search_customers, _strip_to_digits
from tenant_context import current_tenant_id, current_tenant


def _is_novacore_tenant():
    """PC Reps and any future tenant pointing at NovaCore are read-only here."""
    try:
        return current_tenant().get("contact_provider") == "novacore"
    except Exception:
        return True  # safer to deny writes if we can't determine

address_book_bp = Blueprint("address_book", __name__)

print("Address Book reading from NovaCore customers table")


def _merge_suppress_flags(contacts):
    """
    Merge local suppress_auto_sms flags into the contact list from NovaCore.
    opted_out_sms and sms_capable now come directly from NovaCore.
    """
    if not contacts:
        return contacts

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT phone_number, suppress_auto_sms FROM sms_preferences "
            "WHERE suppress_auto_sms = TRUE AND tenant_id = ?",
            (current_tenant_id(),),
        )
        prefs_rows = cur.fetchall()
    finally:
        conn.close()

    # Build a lookup keyed by last 10 digits
    suppress_digits = set()
    for row in prefs_rows:
        digits = _strip_to_digits(row["phone_number"])
        if digits:
            suppress_digits.add(digits)

    # Merge into contacts
    for contact in contacts:
        for phone_field in ["phone_primary", "phone_secondary"]:
            digits = _strip_to_digits(contact.get(phone_field))
            if digits and digits in suppress_digits:
                contact["suppress_auto_sms"] = True
                break

    return contacts


@address_book_bp.route("/address-book/api")
def get_contacts():
    """Fetch all active customers from NovaCore with local SMS preference overlay."""
    try:
        contacts = fetch_all_customers()
        contacts = _merge_suppress_flags(contacts)
        return jsonify(contacts)
    except Exception as e:
        print(f"Error getting contacts from NovaCore: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@address_book_bp.route("/address-book/search", methods=["GET"])
def search_contacts_route():
    """Search NovaCore customers by name, company, email, or phone."""
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])

    try:
        results = search_customers(query)
        return jsonify(results)
    except Exception as e:
        print(f"Error searching contacts: {e}")
        return jsonify({"error": str(e)}), 500


# ───────── Write endpoints ─────────
# Behavior depends on the tenant's contact_provider:
#   'novacore' (PC Reps) → 503, contacts are managed in NovaCore
#   'native'   (HaniTech, future tenants) → INSERT/UPDATE/DELETE on the
#                                           local contacts table, scoped
#                                           by tenant_id.


_READ_ONLY_RESPONSE = (
    jsonify({
        "error": "Contact creation/editing for this tenant is managed externally (NovaCore).",
        "status": "read_only",
    }),
    503,
)


@address_book_bp.route("/address-book/add", methods=["POST"])
def add_contact():
    if _is_novacore_tenant():
        return _READ_ONLY_RESPONSE

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    phone_primary = (data.get("phone_primary") or data.get("phone") or "").strip() or None
    phone_secondary = (data.get("phone_secondary") or "").strip() or None
    company = (data.get("company") or "").strip() or None
    email = (data.get("email") or "").strip() or None
    address = (data.get("address") or "").strip() or None
    notes = (data.get("notes") or "").strip() or None

    if not name and not phone_primary:
        return jsonify({"error": "name or phone_primary is required"}), 400

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO contacts
                (tenant_id, name, phone_primary, phone_secondary, phone,
                 company, email, address, notes, sms_capable, opted_out_sms,
                 suppress_auto_sms, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0, NOW()::text, NOW()::text)
            RETURNING id, name, phone_primary, phone_secondary, company,
                      email, address, notes
            """,
            (
                current_tenant_id(),
                name or None,
                phone_primary,
                phone_secondary,
                phone_primary,  # legacy `phone` column kept in sync
                company,
                email,
                address,
                notes,
            ),
        )
        row = cur.fetchone()
        conn.commit()
        return jsonify(dict(row)), 201
    except Exception as e:
        conn.rollback()
        print(f"add_contact error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@address_book_bp.route("/address-book/update/<int:contact_id>", methods=["POST"])
def update_contact(contact_id):
    if _is_novacore_tenant():
        return _READ_ONLY_RESPONSE

    data = request.get_json(silent=True) or {}

    # Build a partial UPDATE with only the fields the client sent.
    fields = []
    params = []
    for col in ("name", "phone_primary", "phone_secondary", "company",
                "email", "address", "notes"):
        if col in data:
            fields.append(f"{col} = ?")
            value = (data.get(col) or "").strip() or None
            params.append(value)
            # Keep the legacy `phone` column synced with phone_primary
            if col == "phone_primary":
                fields.append("phone = ?")
                params.append(value)

    if not fields:
        return jsonify({"error": "no editable fields supplied"}), 400

    fields.append("updated_at = NOW()::text")
    params.extend([contact_id, current_tenant_id()])

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            UPDATE contacts SET {', '.join(fields)}
             WHERE id = ? AND tenant_id = ?
         RETURNING id, name, phone_primary, phone_secondary, company,
                   email, address, notes
            """,
            params,
        )
        row = cur.fetchone()
        conn.commit()
        if not row:
            return jsonify({"error": "contact not found"}), 404
        return jsonify(dict(row)), 200
    except Exception as e:
        conn.rollback()
        print(f"update_contact error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@address_book_bp.route("/address-book/delete/<int:id>", methods=["POST"])
def delete_contact(id):
    if _is_novacore_tenant():
        return _READ_ONLY_RESPONSE

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM contacts WHERE id = ? AND tenant_id = ?",
            (id, current_tenant_id()),
        )
        deleted = cur.rowcount
        conn.commit()
        if deleted == 0:
            return jsonify({"error": "contact not found"}), 404
        return jsonify({"status": "deleted", "id": id}), 200
    except Exception as e:
        conn.rollback()
        print(f"delete_contact error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@address_book_bp.route("/address-book/delete-bulk", methods=["POST"])
def delete_bulk_contacts():
    if _is_novacore_tenant():
        return _READ_ONLY_RESPONSE

    data = request.get_json(silent=True) or {}
    ids = data.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "ids array required"}), 400
    try:
        ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        return jsonify({"error": "ids must be integers"}), 400

    placeholders = ",".join(["?"] * len(ids))
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"DELETE FROM contacts WHERE id IN ({placeholders}) AND tenant_id = ?",
            ids + [current_tenant_id()],
        )
        deleted = cur.rowcount
        conn.commit()
        return jsonify({"status": "deleted", "count": deleted}), 200
    except Exception as e:
        conn.rollback()
        print(f"delete_bulk_contacts error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()
