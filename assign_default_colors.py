import psycopg2
from psycopg2.extras import RealDictCursor
import random
import os
import sys
from dotenv import load_dotenv

load_dotenv()


def get_novacore_connection():
    """Get a connection to the NovaCore PostgreSQL database"""
    password = os.getenv("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError("POSTGRES_PASSWORD environment variable is not set")
    return psycopg2.connect(
        host="localhost",
        port=5432,
        database="novacore",
        user="postgres",
        password=password,
        cursor_factory=RealDictCursor
    )


def generate_random_color():
    """Generate a random hex color"""
    r = random.randint(100, 255)  # Avoid too dark colors
    g = random.randint(100, 255)
    b = random.randint(100, 255)
    return f"#{r:02x}{g:02x}{b:02x}"


def assign_default_colors():
    """Assign random colors to users who don't have one"""
    try:
        conn = get_novacore_connection()
        cur = conn.cursor()
        
        # Get all users without colors
        cur.execute("SELECT id, first_name, last_name FROM users WHERE user_color IS NULL OR user_color = ''")
        users = cur.fetchall()
        
        if not users:
            print("All users already have colors assigned!")
            conn.close()
            return
        
        print(f"Found {len(users)} users without colors. Assigning...")
        
        for user in users:
            user_id = user['id']
            first_name = user['first_name']
            last_name = user['last_name']
            color = generate_random_color()
            cur.execute("UPDATE users SET user_color = %s WHERE id = %s", (color, user_id))
            print(f"Assigned {color} to {first_name} {last_name}")
        
        conn.commit()
        conn.close()
        print(f"\nSuccessfully assigned colors to {len(users)} users!")
        
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    assign_default_colors()
