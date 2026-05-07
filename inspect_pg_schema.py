"""
Throwaway: dump current PostgreSQL schema for the softphone DB so the
tenant migration plans against reality, not the explore-agent map.

Lists every public table with columns + row count. Run once, then delete.

Usage: python inspect_pg_schema.py
"""

from database import get_db_connection


def main():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public'
        ORDER BY table_name
    """)
    tables = [r["table_name"] for r in cur.fetchall()]

    print(f"=== softphone DB has {len(tables)} table(s) ===\n")

    for t in tables:
        cur.execute("""
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
        """, (t,))
        cols = cur.fetchall()

        cur.execute(f'SELECT COUNT(*) AS c FROM "{t}"')
        n = cur.fetchone()["c"]

        print(f"-- {t} ({n} rows)")
        for c in cols:
            nn = "" if c["is_nullable"] == "YES" else " NOT NULL"
            d = f" DEFAULT {c['column_default']}" if c["column_default"] else ""
            print(f"   {c['column_name']:30s} {c['data_type']}{nn}{d}")
        print()

    conn.close()


if __name__ == "__main__":
    main()
