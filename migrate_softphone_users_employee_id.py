"""
One-shot migration: add employee_id column to softphone_users.

The original migrate_tenants.py created softphone_users without an
employee_id field. The Twilio access-token endpoint uses
current_user.employee_id as the Twilio client identity, so HaniTech
users need this field too.

Idempotent: ADD COLUMN IF NOT EXISTS, safe to re-run.

Usage:
    python migrate_softphone_users_employee_id.py
"""

from database import get_db_connection


def migrate():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            ALTER TABLE softphone_users
            ADD COLUMN IF NOT EXISTS employee_id VARCHAR(50)
        """)
        # employee_id must be unique across the table (Twilio identity is
        # the routing key) but nullable for backfill safety. New rows get
        # one; existing rows (none yet) wouldn't.
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'softphone_users_employee_id_key'
                ) THEN
                    ALTER TABLE softphone_users
                    ADD CONSTRAINT softphone_users_employee_id_key
                    UNIQUE (employee_id);
                END IF;
            END $$;
        """)
        conn.commit()
        print("✅ softphone_users.employee_id ready (nullable, unique).")
    except Exception as e:
        conn.rollback()
        print(f"❌ migration failed: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
