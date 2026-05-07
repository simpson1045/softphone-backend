import sqlite3

CONTACTS_DB_PATH = r"\\192.168.1.100\pc-reps\PC Reps\softphone\contacts.sqlite3"

REQUIRED_COLUMNS = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "name": "TEXT",
    "phone": "TEXT",
    "email": "TEXT DEFAULT ''",
   "address": "TEXT DEFAULT ''"
}

def migrate_contacts():
    conn = sqlite3.connect(CONTACTS_DB_PATH)
    c = conn.cursor()

    # Check current schema
    c.execute("PRAGMA table_info(contacts)")
    existing_columns = {col[1] for col in c.fetchall()}

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
    print("🎉 Contacts table is ready.")

if __name__ == "__main__":
    migrate_contacts()
