"""
One-time migration: create the message_templates table and seed it from
becca_templates.json (exported from Becca's browser localStorage).

Safe to re-run: table creation uses IF NOT EXISTS, and seeding is skipped
if the table already has rows.

Usage:
    python migrate_message_templates.py
"""

import json
import os
from database import get_db_connection


SEED_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "becca_templates.json",
)


def migrate():
    conn = get_db_connection()
    cur = conn.cursor()

    print("Creating message_templates table...")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS message_templates (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            content TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'General',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)

    cur.execute("SELECT COUNT(*) AS c FROM message_templates")
    existing = cur.fetchone()["c"]
    if existing > 0:
        print(f"Table already has {existing} rows — skipping seed.")
        conn.commit()
        conn.close()
        return

    if not os.path.exists(SEED_FILE):
        print(f"No seed file at {SEED_FILE} — table created empty.")
        conn.commit()
        conn.close()
        return

    with open(SEED_FILE, "r", encoding="utf-8") as f:
        templates = json.load(f)

    print(f"Seeding {len(templates)} template(s) from {SEED_FILE}...")
    for t in templates:
        cur.execute(
            """
            INSERT INTO message_templates (name, content, category)
            VALUES (?, ?, ?)
            """,
            (t["name"], t["content"], t.get("category") or "General"),
        )

    conn.commit()
    conn.close()
    print(f"Done. Seeded {len(templates)} template(s).")


if __name__ == "__main__":
    migrate()
