"""
Softphone Database Utility
Provides centralized database connections for PostgreSQL
"""

import os
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

# Load environment variables
from dotenv import load_dotenv

load_dotenv()

# PostgreSQL connection settings from environment
PG_CONFIG = {
    "host": os.getenv("POSTGRES_HOST", "localhost"),
    "port": int(os.getenv("POSTGRES_PORT", 5432)),
    "database": os.getenv("POSTGRES_DB", "softphone"),
    "user": os.getenv("POSTGRES_USER", "postgres"),
    "password": os.getenv("POSTGRES_PASSWORD", ""),
}

# Connection pool for better performance with concurrent users
_connection_pool = None


def init_pool(min_connections=2, max_connections=20):
    """Initialize the connection pool"""
    global _connection_pool
    if _connection_pool is None:
        _connection_pool = pool.ThreadedConnectionPool(
            min_connections, max_connections, **PG_CONFIG
        )
        print(
            f"✅ Softphone PostgreSQL connection pool initialized (max {max_connections} connections)"
        )
    return _connection_pool


def get_pool():
    """Get the connection pool, initializing if necessary"""
    global _connection_pool
    if _connection_pool is None:
        init_pool()
    return _connection_pool


def _convert_placeholders(query):
    """
    Convert SQLite ? placeholders to PostgreSQL %s,
    but only outside of quoted strings.

    Naive str.replace("?", "%s") would break queries containing
    literal ? in string values, JSONB operators (?|, ?&), etc.
    """
    result = []
    i = 0
    length = len(query)
    while i < length:
        ch = query[i]
        if ch in ("'", '"'):
            # Walk to the closing quote, handling escaped quotes ('')
            quote = ch
            j = i + 1
            while j < length:
                if query[j] == quote:
                    # Check for escaped quote (doubled)
                    if j + 1 < length and query[j + 1] == quote:
                        j += 2
                    else:
                        j += 1
                        break
                else:
                    j += 1
            result.append(query[i:j])
            i = j
        elif ch == '?':
            result.append('%s')
            i += 1
        else:
            result.append(ch)
            i += 1
    return ''.join(result)


class PostgresCursorWrapper:
    """
    Wrapper around psycopg2 cursor that converts ? to %s placeholders.
    """

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, query, params=None):
        query = _convert_placeholders(query)
        return self._cursor.execute(query, params)

    def executemany(self, query, params_list):
        query = _convert_placeholders(query)
        return self._cursor.executemany(query, params_list)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchmany(self, size=None):
        return self._cursor.fetchmany(size)

    def close(self):
        return self._cursor.close()

    @property
    def lastrowid(self):
        return None

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    def __iter__(self):
        return iter(self._cursor)


class PostgresConnectionWrapper:
    """
    Wrapper around psycopg2 connection that:
    1. Returns RealDictCursor by default (dict-like rows, similar to sqlite3.Row)
    2. Supports conn.execute() like SQLite
    3. Returns to pool on close()
    4. Auto-converts ? placeholders to %s
    5. Safety net: __del__ returns leaked connections to pool on garbage collection
    """

    def __init__(self, conn, pool):
        self._conn = conn
        self._pool = pool
        self._cursor = None
        self._closed = False

    @property
    def row_factory(self):
        """Dummy property for SQLite compatibility - always uses RealDictCursor"""
        return None

    @row_factory.setter
    def row_factory(self, value):
        """Ignore row_factory assignments - we always use RealDictCursor"""
        pass

    def cursor(self):
        """Return a wrapped RealDictCursor with ? to %s conversion"""
        return PostgresCursorWrapper(self._conn.cursor(cursor_factory=RealDictCursor))

    def execute(self, query, params=None):
        """Execute query and return cursor (SQLite compatibility)"""
        cursor = self.cursor()
        cursor.execute(query, params)  # cursor.execute() calls _convert_placeholders
        return cursor

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        """Return connection to pool (idempotent — safe to call multiple times)"""
        if not self._closed:
            self._closed = True
            try:
                self._pool.putconn(self._conn)
            except Exception:
                pass  # Pool may already be closed during shutdown

    def __del__(self):
        """Safety net: return leaked connections to pool on garbage collection"""
        if not self._closed:
            print(f"⚠️  DB connection leaked! Returning to pool via __del__ safety net.")
            self.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()
        self.close()
        return False


def get_db_connection():
    """
    Get a database connection from the pool.
    Returns a wrapped connection with RealDictCursor (rows as dictionaries).

    Usage:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM messages")
            rows = cursor.fetchall()
    """
    pool = get_pool()
    conn = pool.getconn()
    return PostgresConnectionWrapper(conn, pool)


@contextmanager
def db_connection():
    """
    Context manager for database connections.
    Automatically returns connection to pool when done.
    """
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
    finally:
        pool.putconn(conn)


@contextmanager
def db_cursor(commit=True):
    """
    Context manager that provides a cursor directly.
    Automatically commits (if commit=True) and returns connection to pool.
    """
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            yield cursor
            if commit:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()


# Test connection on import
if __name__ == "__main__":
    print("Testing Softphone PostgreSQL connection...")
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 as test")
        result = cursor.fetchone()
        print(f"✅ Connected! Test query returned: {result}")
        conn.close()
    except Exception as e:
        print(f"❌ Connection failed: {e}")
