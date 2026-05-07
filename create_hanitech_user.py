"""
One-shot: create a HaniTech user account in softphone_users.

Interactive by default — prompts for email, name, employee_id, password.
Can also run non-interactively via env vars (useful for re-running
in scripts without typing):

    HANITECH_EMAIL=matt@hanitechsolutions.com \
    HANITECH_FIRST=Matt \
    HANITECH_LAST=Hanington \
    HANITECH_EMPLOYEE_ID=ht-matt \
    HANITECH_PASSWORD=...   \
    python create_hanitech_user.py

Re-runnable safely: if the email already exists in softphone_users,
the script reports that and exits without overwriting.

Run after migrate_softphone_users_employee_id.py.
"""

import os
import sys
import getpass
from werkzeug.security import generate_password_hash
from database import get_db_connection


def prompt(label, default=None, env_key=None, hidden=False):
    if env_key and os.getenv(env_key):
        return os.getenv(env_key)
    suffix = f" [{default}]" if default else ""
    if hidden:
        value = getpass.getpass(f"{label}{suffix}: ").strip()
    else:
        value = input(f"{label}{suffix}: ").strip()
    return value or default


def main():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT id FROM tenants WHERE slug = 'hanitech'")
    row = cur.fetchone()
    if not row:
        print("❌ tenants table has no hanitech row — run migrate_tenants.py first.")
        sys.exit(1)
    hanitech_id = row["id"]
    print(f"✓ hanitech tenant id = {hanitech_id}")

    # Verify employee_id column exists (added by migrate_softphone_users_employee_id.py)
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'softphone_users' AND column_name = 'employee_id'
    """)
    if not cur.fetchone():
        print("❌ softphone_users.employee_id column missing — "
              "run migrate_softphone_users_employee_id.py first.")
        sys.exit(1)

    print("\n--- Create HaniTech user ---\n")
    email = prompt("Email", default="matt@hanitechsolutions.com",
                   env_key="HANITECH_EMAIL")
    first_name = prompt("First name", default="Matt",
                        env_key="HANITECH_FIRST")
    last_name = prompt("Last name", default="Hanington",
                       env_key="HANITECH_LAST")
    employee_id = prompt("Employee ID (used as Twilio client identity)",
                         default="ht-matt", env_key="HANITECH_EMPLOYEE_ID")
    role = prompt("Role", default="admin", env_key="HANITECH_ROLE")
    user_color = prompt("UI color (hex, e.g. #4f46e5)", default="#4f46e5",
                        env_key="HANITECH_COLOR")

    password = prompt("Password (min 8 chars)", env_key="HANITECH_PASSWORD",
                      hidden=True)
    if not password or len(password) < 8:
        print("❌ Password must be at least 8 characters.")
        sys.exit(1)

    if not os.getenv("HANITECH_PASSWORD"):
        confirm = getpass.getpass("Confirm password: ").strip()
        if confirm != password:
            print("❌ Passwords don't match.")
            sys.exit(1)

    # Idempotency: if email already exists, bail
    cur.execute(
        "SELECT id, tenant_id FROM softphone_users WHERE LOWER(email) = LOWER(%s)",
        (email,),
    )
    existing = cur.fetchone()
    if existing:
        print(f"\n⚠️  softphone_users already has email={email} "
              f"(id={existing['id']}, tenant_id={existing['tenant_id']}).")
        print("    Not overwriting. Delete the row manually if you want to recreate.")
        conn.close()
        sys.exit(0)

    # Idempotency: employee_id is unique
    cur.execute(
        "SELECT id FROM softphone_users WHERE employee_id = %s",
        (employee_id,),
    )
    if cur.fetchone():
        print(f"\n⚠️  softphone_users already has employee_id={employee_id}. "
              "Pick a different one.")
        conn.close()
        sys.exit(1)

    password_hash = generate_password_hash(password)

    cur.execute("""
        INSERT INTO softphone_users
            (tenant_id, employee_id, email, password_hash,
             first_name, last_name, role, active, user_color)
        VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s)
        RETURNING id
    """, (
        hanitech_id, employee_id, email, password_hash,
        first_name, last_name, role, user_color,
    ))
    new_id = cur.fetchone()["id"]
    conn.commit()
    conn.close()

    print(f"\n✅ Created softphone_users id={new_id} for {email} "
          f"(tenant_id={hanitech_id}, employee_id={employee_id}).")
    print("\nLogin at https://softphone.pc-reps.com once Phase 5 ships the "
          "tenant-aware frontend, OR test the auth path now via:")
    print(f'    curl -X POST https://softphone.pc-reps.com/api/auth/login \\')
    print(f'         -H "Content-Type: application/json" \\')
    print(f'         -d \'{{"email": "{email}", "password": "<password>"}}\'')


if __name__ == "__main__":
    main()
