"""
Address Book Blueprint
Reads customer data from NovaCore's customers table.
SMS preferences and flags are stored locally in the softphone database.
"""

from flask import Blueprint, request, jsonify
from database import get_db_connection
from novacore_contacts import fetch_all_customers, search_customers, _strip_to_digits

address_book_bp = Blueprint("address_book", __name__)

print("Address Book reading from NovaCore customers table")


def _merge_sms_preferences(contacts):
    """
    Merge local sms_preferences data into the contact list from NovaCore.
    Matches on normalized phone digits.
    """
    if not contacts:
        return contacts

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT phone_number, suppress_auto_sms, opted_out_sms, sms_capable FROM sms_preferences")
        prefs_rows = cur.fetchall()
    finally:
        conn.close()

    # Build a lookup keyed by last 10 digits
    prefs_by_digits = {}
    for row in prefs_rows:
        digits = _strip_to_digits(row["phone_number"])
        if digits:
            prefs_by_digits[digits] = {
                "suppress_auto_sms": bool(row["suppress_auto_sms"]),
                "opted_out_sms": bool(row["opted_out_sms"]),
                "sms_capable": row["sms_capable"] if row["sms_capable"] is not None else None,
            }

    # Merge into contacts
    for contact in contacts:
        for phone_field in ["phone_primary", "phone_secondary"]:
            digits = _strip_to_digits(contact.get(phone_field))
            if digits and digits in prefs_by_digits:
                pref = prefs_by_digits[digits]
                contact["suppress_auto_sms"] = pref["suppress_auto_sms"]
                contact["opted_out_sms"] = pref["opted_out_sms"]
                if pref["sms_capable"] is not None:
                    contact["sms_capable"] = bool(pref["sms_capable"])
                break  # First match wins

    return contacts


@address_book_bp.route("/address-book/api")
def get_contacts():
    """Fetch all active customers from NovaCore with local SMS preference overlay."""
    try:
        contacts = fetch_all_customers()
        contacts = _merge_sms_preferences(contacts)
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


# --- Write endpoints: stubbed until NovaCore API is built ---

@address_book_bp.route("/address-book/add", methods=["POST"])
def add_contact():
    """Contact creation is now managed through NovaCore."""
    return jsonify({
        "error": "Contact creation is now managed through NovaCore. Please add contacts there.",
        "status": "read_only"
    }), 503


@address_book_bp.route("/address-book/update/<int:contact_id>", methods=["POST"])
def update_contact(contact_id):
    """Contact editing is now managed through NovaCore."""
    return jsonify({
        "error": "Contact editing is now managed through NovaCore. Please update contacts there.",
        "status": "read_only"
    }), 503


@address_book_bp.route("/address-book/delete/<int:id>", methods=["POST"])
def delete_contact(id):
    """Contact deletion is now managed through NovaCore."""
    return jsonify({
        "error": "Contact deletion is now managed through NovaCore.",
        "status": "read_only"
    }), 503


@address_book_bp.route("/address-book/delete-bulk", methods=["POST"])
def delete_bulk_contacts():
    """Bulk deletion is now managed through NovaCore."""
    return jsonify({
        "error": "Contact deletion is now managed through NovaCore.",
        "status": "read_only"
    }), 503
