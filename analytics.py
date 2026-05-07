"""
Analytics event logging for the softphone backend.
Extracted from app.py to avoid circular imports.
"""

import json
from datetime import datetime
from database import get_db_connection


def log_analytics_event(
    greeting_type, greeting_name, event_type, phone_number=None, additional_data=None
):
    """Log analytics events"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO greeting_analytics
            (greeting_type, greeting_name, event_type, phone_number, additional_data, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                greeting_type,
                greeting_name,
                event_type,
                phone_number,
                json.dumps(additional_data) if additional_data else None,
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Analytics logging error: {e}")
