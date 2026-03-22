"""
Import Contacts - Stub
CSV import is disabled now that contacts are managed through NovaCore.
This blueprint is kept so app.py doesn't break on import.
"""

from flask import Blueprint, jsonify

import_contacts_bp = Blueprint("import_contacts", __name__)


@import_contacts_bp.route("/address-book/import", methods=["POST"])
def import_contacts():
    """CSV import is disabled — contacts are now managed through NovaCore."""
    return jsonify({
        "error": "Contact import is disabled. Contacts are now managed through NovaCore.",
        "status": "read_only"
    }), 503
