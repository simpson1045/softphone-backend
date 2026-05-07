"""
Phase 5: Add branding columns to the tenants table.

Adds:
    logo_url   VARCHAR(255)  — optional URL/path to tenant logo (rendered
                              in the navbar). NULL falls back to a generic
                              text-only navbar.
    color      VARCHAR(20)   — optional hex like '#237bf7' driving the
                              accent CSS variable. NULL falls back to the
                              app's default blue (#0a3d62).

Idempotent: ADD COLUMN IF NOT EXISTS, safe to re-run.

Usage:
    python migrate_tenant_branding.py
"""

from database import get_db_connection


def migrate():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            ALTER TABLE tenants
            ADD COLUMN IF NOT EXISTS logo_url VARCHAR(255)
        """)
        cur.execute("""
            ALTER TABLE tenants
            ADD COLUMN IF NOT EXISTS color VARCHAR(20)
        """)

        # Sensible defaults — tenants can override later via an admin UI.
        # PC Reps gets the existing build asset path; HaniTech starts with
        # NULL logo (text-only navbar) and the blue Matt set during user
        # creation.
        cur.execute("""
            UPDATE tenants
            SET logo_url = '/assets/logo4_blue_white-Bd2iz3nx.png'
            WHERE slug = 'pc_reps' AND logo_url IS NULL
        """)
        cur.execute("""
            UPDATE tenants
            SET color = '#0a3d62'
            WHERE slug = 'pc_reps' AND color IS NULL
        """)
        cur.execute("""
            UPDATE tenants
            SET color = '#237bf7'
            WHERE slug = 'hanitech' AND color IS NULL
        """)

        conn.commit()
        print("✅ tenants.logo_url + tenants.color ready, defaults seeded.")

        cur.execute("SELECT slug, name, logo_url, color FROM tenants ORDER BY id")
        for row in cur.fetchall():
            print(f"   {row['slug']:10s}  logo={row['logo_url']!r:50s}  color={row['color']!r}")
    except Exception as e:
        conn.rollback()
        print(f"❌ migration failed: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
