from flask import Blueprint, jsonify
import json
import re
import os
from database import get_db_connection
from phone_utils import normalize_phone_number, get_contact_name
from novacore_contacts import bulk_resolve_names

messages_api = Blueprint("messages_api", __name__)


@messages_api.route("/messages/thread/<phone_number>", methods=["GET"])
def get_thread(phone_number):
    """UPDATED: Get thread messages from master database"""
    try:
        # Normalize phone number for lookup
        normalized_phone = normalize_phone_number(phone_number)
        if not normalized_phone:
            return jsonify([])
            
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, direction, body, media_urls, timestamp, status, status_reason, twilio_sid
            FROM messages
            WHERE phone_number = ?
            ORDER BY timestamp ASC
        """, (normalized_phone,))
        
        rows = cur.fetchall()
        conn.close()
        
        messages = []
        for row in rows:
            try:
                media = json.loads(row["media_urls"]) if row["media_urls"] and row["media_urls"].strip().startswith("[") else []
            except Exception as e:
                print(f"⚠️ JSON decode failed for media_urls: {row['media_urls']} — {e}")
                media = []
                
            messages.append({
                "id": row["id"],
                "direction": row["direction"],
                "body": row["body"],
                "media_urls": media,
                "timestamp": row["timestamp"],
                "status": row["status"],
                "status_reason": row["status_reason"],
                "twilio_sid": row["twilio_sid"]
            })
        
        return jsonify(messages)
            
    except Exception as e:
        print(f"❌ Error in get_thread: {e}")
        return jsonify([]), 500


@messages_api.route("/messages/threads", methods=["GET"])
def get_threads():
    """Get message threads — single query + batch contact lookup"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Single query: latest message + unread count per thread
        cur.execute("""
            SELECT
                latest.phone_number,
                latest.body   AS latest_message,
                latest.direction AS latest_direction,
                latest.timestamp AS latest_timestamp,
                COALESCE(u.unread_count, 0) AS unread_count
            FROM (
                SELECT DISTINCT ON (phone_number)
                    phone_number, body, direction, timestamp
                FROM messages
                ORDER BY phone_number, timestamp DESC
            ) latest
            LEFT JOIN (
                SELECT phone_number, COUNT(*) AS unread_count
                FROM messages
                WHERE direction = 'inbound' AND read = 0
                GROUP BY phone_number
            ) u ON latest.phone_number = u.phone_number
            ORDER BY latest.timestamp DESC
        """)

        rows = cur.fetchall()
        conn.close()

        # Batch-resolve all contact names in one NovaCore query
        phone_numbers = [row["phone_number"] for row in rows]
        name_map = bulk_resolve_names(phone_numbers)

        threads = []
        for row in rows:
            phone = row["phone_number"]
            threads.append({
                "phone_number": phone,
                "contact_name": name_map.get(phone),
                "latest_message": row["latest_message"] or "",
                "latest_direction": row["latest_direction"] or "inbound",
                "latest_timestamp": row["latest_timestamp"],
                "unread_count": row["unread_count"]
            })

        return jsonify(threads)

    except Exception as e:
        print(f"❌ Error in get_threads: {e}")
        return jsonify([]), 500


@messages_api.route("/messages/mark-read/<phone_number>", methods=["POST"])
def mark_thread_read(phone_number):
    """UPDATED: Mark thread as read in master database"""
    try:
        # Normalize phone number for lookup
        normalized_phone = normalize_phone_number(phone_number)
        if not normalized_phone:
            return jsonify({"error": "Invalid phone number"}), 400
            
        conn = get_db_connection()
        cur = conn.cursor()
        # Only mark INBOUND messages as read
        cur.execute("""
            UPDATE messages
            SET read = 1
            WHERE phone_number = ? AND direction = 'inbound' AND read = 0
        """, (normalized_phone,))
        updated = cur.rowcount
        conn.commit()
        conn.close()
        print(f"✅ Marked {updated} inbound messages as read for {normalized_phone}")
        
        return jsonify({"status": "ok", "updated": updated})
        
    except Exception as e:
        print(f"❌ Error in mark-read: {e}")
        return jsonify({"error": str(e)}), 500


@messages_api.route("/messages/mark-unread/<phone_number>", methods=["POST"])
def mark_thread_unread(phone_number):
    """UPDATED: Mark thread as unread in master database"""
    try:
        # Normalize phone number for lookup
        normalized_phone = normalize_phone_number(phone_number)
        if not normalized_phone:
            return jsonify({"error": "Invalid phone number"}), 400
            
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE messages
            SET read = 0
            WHERE phone_number = ? AND direction = 'inbound'
        """, (normalized_phone,))
        updated = cur.rowcount
        conn.commit()
        conn.close()
        
        return jsonify({"status": "ok", "updated": updated})
        
    except Exception as e:
        print(f"❌ Error in mark-unread: {e}")
        return jsonify({"error": str(e)}), 500
