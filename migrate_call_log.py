import sqlite3

DB_PATH = "call_log.sqlite3"

REQUIRED_COLUMNS = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "to_number": "TEXT",
    "from_number": "TEXT",
    "status": "TEXT",
    "timestamp": "TEXT",
    "caller_name": "TEXT DEFAULT ''",
    "call_type": "TEXT DEFAULT 'unknown'"
}

def migrate():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Get current columns
    c.execute("PRAGMA table_info(call_log)")
    existing_columns = {col[1] for col in c.fetchall()}

    # If the table doesn't exist at all, create it fresh
    if not existing_columns:
        print("ℹ️ Creating call_log table from scratch.")
        col_defs = ", ".join([f"{name} {type_}" for name, type_ in REQUIRED_COLUMNS.items()])
        c.execute(f"CREATE TABLE call_log ({col_defs})")
        print("✅ call_log table created with all required columns.")
    else:
        # Add any missing columns one-by-one
        for name, type_ in REQUIRED_COLUMNS.items():
            if name not in existing_columns:
                c.execute(f"ALTER TABLE call_log ADD COLUMN {name} {type_}")
                print(f"✅ Added missing column: {name}")

    conn.commit()
    conn.close()
    print("🎉 Migration complete.")

if __name__ == "__main__":
    migrate()
