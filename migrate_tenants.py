"""
Multi-tenant bridge migration: introduces a `tenants` table and adds
`tenant_id` to every user-data table, backfilling existing rows to the
PC Reps tenant. Also creates `softphone_users` for HaniTech logins
(PC Reps continues to auth via NovaCore for now — see auth.py).

Idempotent: re-running is safe. Uses a single transaction — partial
failure rolls back cleanly.

Tables that get tenant_id (10):
    messages, call_log, voicemails, message_templates,
    sms_preferences, contact_flags, auto_sms_log, greeting_analytics,
    greetings, app_settings, contacts

Tables left tenant-shared (intentional, see comments below):
    flag_types  — generic flag categories, both tenants can share

New tables:
    tenants            — slug, name, phone_number
    softphone_users    — for HaniTech accounts (PC Reps still uses NovaCore)

Run:
    python migrate_tenants.py
"""

from database import get_db_connection


# Tables that need a tenant_id column added + backfilled to pc_reps.
# Order matters slightly: smallest first so any error surfaces fast.
# sms_preferences is handled in the same loop; the composite-PK swap
# happens in step 4 after the column is in place.
TABLES_TO_TENANT = [
    "app_settings",
    "auto_sms_log",
    "contact_flags",
    "greetings",
    "message_templates",
    "sms_preferences",
    "voicemails",
    "greeting_analytics",
    "contacts",
    "call_log",
    "messages",  # biggest, do last
]

# Composite-uniqueness fixes: columns that are currently unique but should
# become (tenant_id, <column>) unique once tenant_id is added.
# Format: (table, list_of_columns_in_existing_unique_constraint)
COMPOSITE_UNIQUES = [
    ("app_settings", ["setting_key"]),
    ("sms_preferences", ["phone_number"]),
    ("contact_flags", ["phone_number"]),
]


def table_has_column(cur, table, column):
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    return cur.fetchone() is not None


def column_is_not_null(cur, table, column):
    cur.execute(
        """
        SELECT is_nullable FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    row = cur.fetchone()
    return row is not None and row["is_nullable"] == "NO"


def constraint_exists(cur, name):
    cur.execute(
        "SELECT 1 FROM pg_constraint WHERE conname = %s",
        (name,),
    )
    return cur.fetchone() is not None


def index_exists(cur, name):
    cur.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname = %s",
        (name,),
    )
    return cur.fetchone() is not None


def migrate():
    conn = get_db_connection()
    cur = conn.cursor()

    try:
        # ── 1. tenants table ───────────────────────────────────────────────
        print("\n[1/5] Creating tenants table…")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tenants (
                id SERIAL PRIMARY KEY,
                slug VARCHAR(50) UNIQUE NOT NULL,
                name VARCHAR(200) NOT NULL,
                phone_number VARCHAR(20) UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        cur.execute("""
            INSERT INTO tenants (slug, name, phone_number) VALUES
                ('pc_reps', 'PC Reps', '+17754602190'),
                ('hanitech', 'HaniTech Solutions', '+17756185775')
            ON CONFLICT (slug) DO NOTHING
        """)

        cur.execute("SELECT id, slug, name, phone_number FROM tenants ORDER BY id")
        tenants = cur.fetchall()
        for t in tenants:
            print(f"   tenant {t['id']}: {t['slug']} ({t['name']}) → {t['phone_number']}")

        cur.execute("SELECT id FROM tenants WHERE slug = 'pc_reps'")
        pc_reps_id = cur.fetchone()["id"]

        # ── 2. softphone_users table (for HaniTech) ────────────────────────
        print("\n[2/5] Creating softphone_users table…")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS softphone_users (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id),
                email VARCHAR(255) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                first_name VARCHAR(100),
                last_name VARCHAR(100),
                role VARCHAR(50) DEFAULT 'user',
                active BOOLEAN DEFAULT TRUE,
                user_color VARCHAR(20),
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        if not index_exists(cur, "idx_softphone_users_tenant"):
            cur.execute("CREATE INDEX idx_softphone_users_tenant ON softphone_users(tenant_id)")
        print("   softphone_users ready (empty — Phase 2 will create accounts)")

        # ── 3. Add tenant_id to each user-data table + backfill ────────────
        print(f"\n[3/5] Adding tenant_id to {len(TABLES_TO_TENANT)} tables…")
        for table in TABLES_TO_TENANT:
            had_column = table_has_column(cur, table, "tenant_id")
            if not had_column:
                cur.execute(f'ALTER TABLE "{table}" ADD COLUMN tenant_id INTEGER REFERENCES tenants(id)')

            cur.execute(f'UPDATE "{table}" SET tenant_id = %s WHERE tenant_id IS NULL', (pc_reps_id,))
            backfilled = cur.rowcount

            if not column_is_not_null(cur, table, "tenant_id"):
                cur.execute(f'ALTER TABLE "{table}" ALTER COLUMN tenant_id SET NOT NULL')

            idx_name = f"idx_{table}_tenant"
            if not index_exists(cur, idx_name):
                cur.execute(f'CREATE INDEX {idx_name} ON "{table}"(tenant_id)')

            cur.execute(f'SELECT COUNT(*) AS c FROM "{table}"')
            total = cur.fetchone()["c"]
            note = "added" if not had_column else "already present"
            print(f"   {table:25s} ({total:6d} rows) — column {note}, backfilled {backfilled}")

        # ── 4. Composite uniqueness fixes ──────────────────────────────────
        print("\n[4/5] Promoting unique constraints to composite (tenant_id, …)…")
        # app_settings.setting_key  →  (tenant_id, setting_key)
        for old_name in ("app_settings_setting_key_key", "app_settings_setting_key_unique"):
            if constraint_exists(cur, old_name):
                cur.execute(f'ALTER TABLE app_settings DROP CONSTRAINT "{old_name}"')
                print(f"   dropped {old_name}")
        if not constraint_exists(cur, "app_settings_tenant_setting_key_unique"):
            cur.execute("""
                ALTER TABLE app_settings
                ADD CONSTRAINT app_settings_tenant_setting_key_unique
                UNIQUE (tenant_id, setting_key)
            """)
            print("   added app_settings_tenant_setting_key_unique")

        # sms_preferences.phone_number is PRIMARY KEY  →  (tenant_id, phone_number)
        cur.execute("""
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'sms_preferences'::regclass AND contype = 'p'
        """)
        pk_row = cur.fetchone()
        if pk_row:
            cur.execute(f'ALTER TABLE sms_preferences DROP CONSTRAINT "{pk_row["conname"]}"')
            print(f"   dropped {pk_row['conname']} (sms_preferences old PK)")
        if not constraint_exists(cur, "sms_preferences_pkey"):
            cur.execute("""
                ALTER TABLE sms_preferences
                ADD CONSTRAINT sms_preferences_pkey
                PRIMARY KEY (tenant_id, phone_number)
            """)
            print("   added sms_preferences_pkey (tenant_id, phone_number)")

        # contact_flags.phone_number is PRIMARY KEY  →  (tenant_id, phone_number)
        cur.execute("""
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'contact_flags'::regclass AND contype = 'p'
        """)
        pk_row = cur.fetchone()
        if pk_row:
            cur.execute(f'ALTER TABLE contact_flags DROP CONSTRAINT "{pk_row["conname"]}"')
            print(f"   dropped {pk_row['conname']} (contact_flags old PK)")
        if not constraint_exists(cur, "contact_flags_pkey"):
            cur.execute("""
                ALTER TABLE contact_flags
                ADD CONSTRAINT contact_flags_pkey
                PRIMARY KEY (tenant_id, phone_number)
            """)
            print("   added contact_flags_pkey (tenant_id, phone_number)")

        # ── 5. Verify ──────────────────────────────────────────────────────
        print("\n[5/5] Verifying…")
        cur.execute("SELECT COUNT(*) AS c FROM tenants")
        assert cur.fetchone()["c"] >= 2, "expected at least 2 tenants (pc_reps, hanitech)"
        for table in TABLES_TO_TENANT:
            cur.execute(f'SELECT COUNT(*) AS c FROM "{table}" WHERE tenant_id IS NULL')
            null_count = cur.fetchone()["c"]
            assert null_count == 0, f"{table} has {null_count} NULL tenant_id rows after backfill"
        print("   all rows backfilled, no NULL tenant_ids ✓")

        conn.commit()
        print("\n✅ Migration committed successfully.\n")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ Migration FAILED, rolled back: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
