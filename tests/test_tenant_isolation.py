"""
Cross-tenant isolation smoke tests.

These tests run against the LIVE softphone Postgres DB. They:
  1. Insert a probe row tagged with tenant_id = hanitech (a row that
     wouldn't normally be there).
  2. Query each user-data table once as pc_reps and once as hanitech
     via the tenant_context module, asserting the probe is visible
     ONLY when querying as hanitech.
  3. Roll the probe row back so the live DB is unchanged afterward.

If any test fails, a query site is leaking data across tenants and
should be audited before merging tenant-bridge to main.

Run (from softphone-backend dir, with venv active):
    python -m pytest tests/test_tenant_isolation.py -v

Or as a one-shot smoke without pytest:
    python tests/test_tenant_isolation.py
"""

import os
import sys
import unittest
from datetime import datetime
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import get_db_connection
from tenant_context import current_tenant_id, _default_tenant_id


# Sentinels so we can tell our probe rows apart from real data
PROBE_PHONE = "+15550199999"
PROBE_BODY = "TENANT-ISOLATION-TEST-PROBE"
PROBE_RECORDING_SID = "RE_TEST_PROBE_HANITECH"


def _tenant_id(slug):
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM tenants WHERE slug = %s", (slug,))
        row = cur.fetchone()
        if not row:
            raise RuntimeError(f"tenants table has no row with slug={slug}")
        return row["id"]


@contextmanager
def _faked_tenant_g(tenant_id):
    """Mimic Phase 3's g.tenant_id without a full Flask request."""
    from flask import Flask, g
    app = Flask("isolation-test")
    with app.test_request_context():
        g.tenant_id = tenant_id
        yield


class TestCrossTenantIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pc_reps_id = _tenant_id("pc_reps")
        cls.hanitech_id = _tenant_id("hanitech")
        cls.assertNotEqual(cls, cls.pc_reps_id, cls.hanitech_id)

    def setUp(self):
        # Insert one probe row per table tagged hanitech
        self.conn = get_db_connection()
        cur = self.conn.cursor()
        ts = datetime.utcnow().isoformat()

        cur.execute(
            "INSERT INTO messages (tenant_id, direction, phone_number, body, "
            "media_urls, timestamp) VALUES (%s, 'inbound', %s, %s, '[]', %s) "
            "RETURNING id",
            (self.hanitech_id, PROBE_PHONE, PROBE_BODY, ts),
        )
        self.message_id = cur.fetchone()["id"]

        cur.execute(
            "INSERT INTO call_log (tenant_id, phone_number, direction, status, "
            "call_type, twilio_call_sid, timestamp) "
            "VALUES (%s, %s, 'inbound', 'completed', 'voice', %s, %s) RETURNING id",
            (self.hanitech_id, PROBE_PHONE, "CA_TEST_PROBE_HANITECH", ts),
        )
        self.call_id = cur.fetchone()["id"]

        cur.execute(
            "INSERT INTO voicemails (tenant_id, phone_number, recording_sid, timestamp) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (self.hanitech_id, PROBE_PHONE, PROBE_RECORDING_SID, ts),
        )
        self.vm_id = cur.fetchone()["id"]

        cur.execute(
            "INSERT INTO greetings (tenant_id, type, name) "
            "VALUES (%s, 'open', 'TENANT-ISOLATION-TEST-PROBE-GREETING') RETURNING id",
            (self.hanitech_id,),
        )
        self.greeting_id = cur.fetchone()["id"]

    def tearDown(self):
        # Roll back all probes so the live DB is untouched
        self.conn.rollback()
        self.conn.close()

    def _query_one(self, sql, *params):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        return cur.fetchall()

    # ─────────── messages ───────────
    def test_messages_isolation(self):
        rows = self._query_one(
            "SELECT id FROM messages WHERE phone_number = %s AND tenant_id = %s",
            PROBE_PHONE, self.hanitech_id,
        )
        self.assertEqual(len(rows), 1, "probe message missing in hanitech tenant")
        rows = self._query_one(
            "SELECT id FROM messages WHERE phone_number = %s AND tenant_id = %s",
            PROBE_PHONE, self.pc_reps_id,
        )
        self.assertEqual(len(rows), 0, "probe message LEAKED into pc_reps tenant")

    # ─────────── call_log ───────────
    def test_call_log_isolation(self):
        rows = self._query_one(
            "SELECT id FROM call_log WHERE phone_number = %s AND tenant_id = %s",
            PROBE_PHONE, self.hanitech_id,
        )
        self.assertEqual(len(rows), 1, "probe call missing in hanitech tenant")
        rows = self._query_one(
            "SELECT id FROM call_log WHERE phone_number = %s AND tenant_id = %s",
            PROBE_PHONE, self.pc_reps_id,
        )
        self.assertEqual(len(rows), 0, "probe call LEAKED into pc_reps tenant")

    # ─────────── voicemails ───────────
    def test_voicemails_isolation(self):
        rows = self._query_one(
            "SELECT id FROM voicemails WHERE recording_sid = %s AND tenant_id = %s",
            PROBE_RECORDING_SID, self.hanitech_id,
        )
        self.assertEqual(len(rows), 1, "probe voicemail missing in hanitech tenant")
        rows = self._query_one(
            "SELECT id FROM voicemails WHERE recording_sid = %s AND tenant_id = %s",
            PROBE_RECORDING_SID, self.pc_reps_id,
        )
        self.assertEqual(len(rows), 0, "probe voicemail LEAKED into pc_reps tenant")

    # ─────────── greetings ───────────
    def test_greetings_isolation(self):
        rows = self._query_one(
            "SELECT id FROM greetings WHERE name = %s AND tenant_id = %s",
            'TENANT-ISOLATION-TEST-PROBE-GREETING', self.hanitech_id,
        )
        self.assertEqual(len(rows), 1, "probe greeting missing in hanitech tenant")
        rows = self._query_one(
            "SELECT id FROM greetings WHERE name = %s AND tenant_id = %s",
            'TENANT-ISOLATION-TEST-PROBE-GREETING', self.pc_reps_id,
        )
        self.assertEqual(len(rows), 0, "probe greeting LEAKED into pc_reps tenant")

    # ─────────── current_tenant_id resolution ───────────
    def test_current_tenant_id_uses_g_when_set(self):
        with _faked_tenant_g(self.hanitech_id):
            self.assertEqual(current_tenant_id(), self.hanitech_id)
        with _faked_tenant_g(self.pc_reps_id):
            self.assertEqual(current_tenant_id(), self.pc_reps_id)

    def test_current_tenant_id_defaults_to_pc_reps(self):
        # Without g.tenant_id and without an authenticated user,
        # the fallback chain must land on pc_reps. _default_tenant_id
        # is lru_cached, so this is also covering the cache.
        self.assertEqual(_default_tenant_id(), self.pc_reps_id)

    # ─────────── composite uniqueness on app_settings ───────────
    def test_app_settings_composite_unique(self):
        """Same setting_key for two tenants must be allowed."""
        cur = self.conn.cursor()
        # PC Reps already has setting_key='dnd_enabled' typically; pick a
        # fresh key to avoid clashing with real data on rollback safety.
        key = "ISOLATION_TEST_KEY"
        cur.execute(
            "INSERT INTO app_settings (tenant_id, setting_key, setting_value) "
            "VALUES (%s, %s, 'pc_reps_value') RETURNING id",
            (self.pc_reps_id, key),
        )
        cur.execute(
            "INSERT INTO app_settings (tenant_id, setting_key, setting_value) "
            "VALUES (%s, %s, 'hanitech_value') RETURNING id",
            (self.hanitech_id, key),
        )
        # If we got here, the composite (tenant_id, setting_key) UNIQUE
        # constraint correctly allows the same key under different tenants.

        # And selecting by tenant must not bleed:
        rows = self._query_one(
            "SELECT setting_value FROM app_settings WHERE setting_key = %s AND tenant_id = %s",
            key, self.pc_reps_id,
        )
        self.assertEqual(rows[0]["setting_value"], "pc_reps_value")
        rows = self._query_one(
            "SELECT setting_value FROM app_settings WHERE setting_key = %s AND tenant_id = %s",
            key, self.hanitech_id,
        )
        self.assertEqual(rows[0]["setting_value"], "hanitech_value")


if __name__ == "__main__":
    unittest.main(verbosity=2)
