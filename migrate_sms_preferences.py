"""
One-time migration script: Create sms_preferences and contact_flags tables,
then seed them from the existing contacts table before we cut over to NovaCore.

Run this ONCE before deploying the NovaCore contact changes.
"""

from database import get_db_connection


def migrate():
    conn = get_db_connection()
    cur = conn.cursor()

    print("Creating sms_preferences table...")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sms_preferences (
            phone_number TEXT PRIMARY KEY,
            suppress_auto_sms BOOLEAN DEFAULT FALSE,
            opted_out_sms BOOLEAN DEFAULT FALSE,
            sms_capable BOOLEAN DEFAULT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)

    print("Creating contact_flags table...")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS contact_flags (
            phone_number TEXT PRIMARY KEY,
            flag_type_id INTEGER REFERENCES flag_types(id) ON DELETE SET NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # Seed sms_preferences from existing contacts
    print("Seeding sms_preferences from contacts table...")
    cur.execute("""
        INSERT INTO sms_preferences (phone_number, suppress_auto_sms, opted_out_sms, sms_capable, created_at, updated_at)
        SELECT phone_primary,
               COALESCE(suppress_auto_sms, FALSE),
               COALESCE(opted_out_sms, FALSE),
               sms_capable,
               COALESCE(created_at::timestamp, NOW()),
               COALESCE(updated_at::timestamp, NOW())
        FROM contacts
        WHERE phone_primary IS NOT NULL
          AND phone_primary != ''
          AND (suppress_auto_sms = TRUE OR opted_out_sms = TRUE OR sms_capable IS NOT NULL)
        ON CONFLICT (phone_number) DO UPDATE SET
            suppress_auto_sms = EXCLUDED.suppress_auto_sms,
            opted_out_sms = EXCLUDED.opted_out_sms,
            sms_capable = EXCLUDED.sms_capable,
            updated_at = NOW()
    """)
    sms_count = cur.rowcount
    print(f"  Migrated {sms_count} SMS preference records")

    # Seed contact_flags from existing contacts
    print("Seeding contact_flags from contacts table...")
    cur.execute("""
        INSERT INTO contact_flags (phone_number, flag_type_id, created_at, updated_at)
        SELECT phone_primary, flag_type_id,
               COALESCE(created_at::timestamp, NOW()),
               COALESCE(updated_at::timestamp, NOW())
        FROM contacts
        WHERE phone_primary IS NOT NULL
          AND phone_primary != ''
          AND flag_type_id IS NOT NULL
        ON CONFLICT (phone_number) DO UPDATE SET
            flag_type_id = EXCLUDED.flag_type_id,
            updated_at = NOW()
    """)
    flag_count = cur.rowcount
    print(f"  Migrated {flag_count} contact flag records")

    conn.commit()
    conn.close()

    print(f"\nMigration complete!")
    print(f"  sms_preferences: {sms_count} records")
    print(f"  contact_flags: {flag_count} records")
    print(f"\nThe old contacts table is still intact as a backup.")
    print(f"Once verified, you can DROP it when ready.")


if __name__ == "__main__":
    migrate()
