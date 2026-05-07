"""
Export contacts as CSV. Pulls from the current tenant's contact provider:
PC Reps -> NovaCore customers, HaniTech (and future native tenants) ->
local softphone.contacts table scoped by tenant_id.
"""

from flask import Blueprint, Response
import csv
import io
from datetime import datetime
from contact_provider import fetch_all_customers

export_contacts_bp = Blueprint("export_contacts", __name__)


@export_contacts_bp.route("/address-book/export", methods=["GET"])
def export_contacts():
    """Export all contacts for the current tenant as a CSV download."""
    customers = fetch_all_customers()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["name", "first_name", "last_name", "phone_primary", "phone_secondary",
         "email", "address", "company", "notes"]
    )

    for c in customers:
        writer.writerow([
            c.get("name", ""),
            c.get("first_name", ""),
            c.get("last_name", ""),
            c.get("phone_primary", ""),
            c.get("phone_secondary", ""),
            c.get("email", ""),
            c.get("address", ""),
            c.get("company", ""),
            c.get("notes", ""),
        ])

    csv_data = output.getvalue()
    output.close()

    filename = f"contacts_export_{datetime.now().strftime('%Y-%m-%d')}.csv"
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
