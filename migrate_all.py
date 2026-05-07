import sqlite3
import os

BASE_DIR = os.path.dirname(__file__)

DATABASES = {
    "call_log.sqlite3": {
        "call_log": {
            "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
            "to_number": "TEXT",
            "from_number": "TEXT",
            "status": "TEXT",
            "timestamp": "TEXT",
            "caller_name": "TEXT DEFAULT ''",
            "call_type": "TEXT DEFAULT 'unknown'"
        }
    },
    "contacts.sqlite3": {
        "contacts": {
            "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
            "name": "TEXT",
            "phone": "TEXT",
            "email": "TEXT DEFAULT ''",
            "address": "TEXT DEFAULT ''"
        }
    },
    "voicemails.sqlite3": {
        "voicemails": {
            "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
            "recording_sid": "TEXT",
            "from_number": "TEXT",
            "transcription": "TEXT",
            "timestamp": "TEXT",
            "file_path": "TEXT"
        }
    }
}

def migrate_all():
    for db_name, tables in DATABASES.items():
        db_path = os.path.join(BASE_DIR, db_name)
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        print(f"\n🔧 Migrating: {db_name}")

        for table_name, columns in tables.items():
            # Check for existing table
            c.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'")
            exists = c.fetchone()

            if not exists:
                col_defs = ", ".join([f"{name} {type_}" for name, type_ in columns.items()])
                c.execute(f"CREATE TABLE {table_name} ({col_defs})")
                print(f"✅ Created table: {table_name}")
            else:
                # Add any missing columns
                c.execute(f"PRAGMA table_info({table_name})")
                existing_columns = {col[1] for col in c.fetchall()}
                for name, type_ in columns.items():
                    if name not in existing_columns:
                        c.execute(f"ALTER TABLE {table_name} ADD COLUMN {name} {type_}")
                        print(f"✅ Added column '{name}' to {table_name}")

        conn.commit()
        conn.close()

    print("\n🎉 All migrations complete.")

if __name__ == "__main__":
    migrate_all()
