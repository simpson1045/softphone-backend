"""
Softphone SQLite to PostgreSQL Migration Script
Migrates data from softphone_master.sqlite3 to PostgreSQL
"""

import sqlite3
import psycopg2
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# PostgreSQL connection settings
PG_CONFIG = {
    "host": os.getenv("POSTGRES_HOST", "localhost"),
    "port": int(os.getenv("POSTGRES_PORT", 5432)),
    "database": os.getenv("POSTGRES_DB", "softphone"),
    "user": os.getenv("POSTGRES_USER", "postgres"),
    "password": os.getenv("POSTGRES_PASSWORD", ""),
}

# SQLite database path
SQLITE_DB = os.path.join(os.path.dirname(__file__), "..", "softphone_master.sqlite3")


def get_pg_connection():
    """Get PostgreSQL connection"""
    return psycopg2.connect(**PG_CONFIG)


def get_sqlite_connection():
    """Get SQLite connection"""
    conn = sqlite3.connect(SQLITE_DB)
    conn.row_factory = sqlite3.Row
    return conn


def get_pg_columns(pg_conn, table_name):
    """Get list of columns that exist in PostgreSQL table"""
    cursor = pg_conn.cursor()
    cursor.execute(
        """
        SELECT column_name FROM information_schema.columns 
        WHERE table_schema = 'public' AND table_name = %s
    """,
        (table_name,),
    )
    return [row[0] for row in cursor.fetchall()]


def migrate_table(sqlite_conn, pg_conn, table_name):
    """Migrate a single table from SQLite to PostgreSQL"""
    sqlite_cursor = sqlite_conn.cursor()
    pg_cursor = pg_conn.cursor()

    # Get all rows from SQLite
    try:
        sqlite_cursor.execute(f"SELECT * FROM {table_name}")
        rows = sqlite_cursor.fetchall()
    except sqlite3.OperationalError as e:
        print(f"  ⏭️  {table_name}: table doesn't exist in SQLite ({e})")
        return 0

    if not rows:
        print(f"  ⏭️  {table_name}: 0 rows (empty)")
        return 0

    # Get column names from SQLite
    sqlite_columns = [description[0] for description in sqlite_cursor.description]

    # Get columns that exist in PostgreSQL
    pg_columns = get_pg_columns(pg_conn, table_name)

    # Find common columns (exist in both SQLite and PostgreSQL)
    common_columns = [col for col in sqlite_columns if col in pg_columns]

    if not common_columns:
        print(f"  ❌ {table_name}: No common columns found!")
        return 0

    # Get indices of common columns in SQLite result
    col_indices = [sqlite_columns.index(col) for col in common_columns]

    # Build INSERT statement
    placeholders = ", ".join(["%s"] * len(common_columns))
    columns_str = ", ".join([f'"{col}"' for col in common_columns])

    insert_sql = f"INSERT INTO {table_name} ({columns_str}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"

    # Insert rows
    count = 0
    errors = 0
    for row in rows:
        try:
            # Extract only the values for common columns
            values = tuple(row[i] for i in col_indices)
            pg_cursor.execute(insert_sql, values)
            count += 1
        except Exception as e:
            errors += 1
            if errors <= 3:
                print(f"    ⚠️  Error inserting row: {e}")
            pg_conn.rollback()

    pg_conn.commit()

    skipped_cols = set(sqlite_columns) - set(common_columns)
    if skipped_cols:
        print(
            f"  ✅ {table_name}: {count} rows migrated (skipped columns: {skipped_cols})"
        )
    elif errors > 0:
        print(f"  ✅ {table_name}: {count} rows migrated ({errors} errors)")
    else:
        print(f"  ✅ {table_name}: {count} rows migrated")

    return count


def reset_sequences(pg_conn):
    """Reset PostgreSQL sequences to match migrated data"""
    print("\nResetting sequences...")
    cursor = pg_conn.cursor()

    tables = [
        "call_log",
        "messages",
        "contacts",
        "voicemails",
        "greetings",
        "app_settings",
        "greeting_analytics",
        "auto_sms_log",
    ]

    for table in tables:
        try:
            cursor.execute(
                f"""
                SELECT setval(pg_get_serial_sequence('{table}', 'id'), 
                       COALESCE((SELECT MAX(id) FROM {table}), 1), 
                       (SELECT MAX(id) FROM {table}) IS NOT NULL)
            """
            )
        except Exception as e:
            print(f"  ⚠️  Could not reset sequence for {table}: {e}")

    pg_conn.commit()
    print("✅ Sequences reset")


def main():
    print("=" * 60)
    print("Softphone SQLite → PostgreSQL Migration")
    print("=" * 60)

    # Check SQLite file exists
    if not os.path.exists(SQLITE_DB):
        print(f"❌ SQLite database not found: {SQLITE_DB}")
        return

    print(f"📁 Source: {SQLITE_DB}")
    print(f"🐘 Target: PostgreSQL {PG_CONFIG['database']}@{PG_CONFIG['host']}")

    # Connect to PostgreSQL
    print("\nConnecting to PostgreSQL...")
    try:
        pg_conn = get_pg_connection()
        print("✅ Connected to PostgreSQL")
    except Exception as e:
        print(f"❌ Failed to connect to PostgreSQL: {e}")
        return

    # Connect to SQLite
    print("Connecting to SQLite...")
    try:
        sqlite_conn = get_sqlite_connection()
        print("✅ Connected to SQLite")
    except Exception as e:
        print(f"❌ Failed to connect to SQLite: {e}")
        return

    # Clear existing data first
    print("\nClearing existing PostgreSQL data...")
    pg_cursor = pg_conn.cursor()
    tables_to_clear = [
        "auto_sms_log",
        "greeting_analytics",
        "voicemails",
        "messages",
        "call_log",
        "contacts",
        "greetings",
        "app_settings",
    ]
    for table in tables_to_clear:
        try:
            pg_cursor.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE")
        except Exception as e:
            print(f"  ⚠️  Could not truncate {table}: {e}")
            pg_conn.rollback()
    pg_conn.commit()
    print("✅ Existing data cleared")

    # Migrate tables
    print("\n" + "=" * 60)
    print("Migrating tables...")
    print("=" * 60)

    tables_to_migrate = [
        "greetings",
        "app_settings",
        "contacts",
        "call_log",
        "messages",
        "voicemails",
        "greeting_analytics",
        "auto_sms_log",
    ]

    total_rows = 0
    for table in tables_to_migrate:
        total_rows += migrate_table(sqlite_conn, pg_conn, table)

    # Reset sequences
    reset_sequences(pg_conn)

    # Close connections
    sqlite_conn.close()
    pg_conn.close()

    # Summary
    print("\n" + "=" * 60)
    print("Migration Complete!")
    print("=" * 60)
    print(f"Total rows migrated: {total_rows}")
    print("\n✅ Your data is now in PostgreSQL!")


if __name__ == "__main__":
    main()
