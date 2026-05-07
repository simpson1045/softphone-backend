import sqlite3

DB_PATH = "contacts.db"  # Replace with your actual path if different

REQUIRED_COLUMNS = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "name": "TEXT",
    "phone": "TEXT",
    "email": "TEXT DEFAULT ''",
    "address": "TEXT DEFAULT ''"
}

def migrate_contacts():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Check current schema
    c.execute("PRAGMA table_info(contacts)")
    existing_columns = {col[1] for col in c.fetchall()}

    # If the table doesn't exist, create it from scratch
    if not existing_columns:
        print("ℹ️ Creating contacts table from scratch.")
        col_defs = ", ".join([f"{name} {type_}" for name, type_ in REQUIRED_COLUMNS.items()])
        c.execute(f"CREATE TABLE contacts ({col_defs})")
        print("✅ contacts table created.")
    else:
        for name, type_ in REQUIRED_COLUMNS.items():
            if name not in existing_columns:
                c.execute(f"ALTER TABLE contacts ADD COLUMN {name} {type_}")
                print(f"✅ Added missing column: {name}")

    conn.commit()
    conn.close()
    print("🎉 Contacts migration complete.")

if __name__ == "__main__":
    migrate_contacts()
