"""
Call Recording Handler
Handles Twilio recording callbacks and local storage
"""

from flask import Blueprint, request, Response
from datetime import datetime
from database import get_db_connection
import os
import requests
import threading
from twilio_security import validate_twilio_request

call_recording_bp = Blueprint("call_recording", __name__)

# Local storage path for recordings
RECORDINGS_DIR = os.path.join(os.path.dirname(__file__), "recordings", "calls")

# Ensure recordings directory exists
os.makedirs(RECORDINGS_DIR, exist_ok=True)


def download_recording_async(recording_url, recording_sid, call_sid):
    """Download recording from Twilio to local storage in background thread"""
    try:
        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")

        if not account_sid or not auth_token:
            print(f"❌ Missing Twilio credentials for recording download")
            return

        # Twilio recording URL needs .mp3 extension for MP3 format
        download_url = f"{recording_url}.mp3"

        print(f"📥 Downloading recording: {recording_sid}")

        response = requests.get(
            download_url, auth=(account_sid, auth_token), timeout=60
        )

        if response.status_code == 200:
            # Generate filename with timestamp and call_sid for uniqueness
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"call_{timestamp}_{recording_sid}.mp3"
            filepath = os.path.join(RECORDINGS_DIR, filename)

            with open(filepath, "wb") as f:
                f.write(response.content)

            print(f"✅ Recording saved: {filename}")

            # Update database with local path
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE call_log 
                    SET recording_local_path = ?
                    WHERE twilio_call_sid = ?
                    """,
                    (filepath, call_sid),
                )
                conn.commit()
                conn.close()
                print(f"✅ Database updated with local path")
            except Exception as e:
                print(f"❌ Error updating local path in DB: {e}")
        else:
            print(f"❌ Failed to download recording: {response.status_code}")

    except Exception as e:
        print(f"❌ Error downloading recording: {e}")


@call_recording_bp.route("/recording/call-complete", methods=["POST"])
@validate_twilio_request
def call_recording_complete():
    """
    Twilio callback when call recording is complete.
    Updates call_log with recording details and triggers local download.
    """
    try:
        # Get recording details from Twilio callback
        call_sid = request.values.get("CallSid")
        recording_sid = request.values.get("RecordingSid")
        recording_url = request.values.get("RecordingUrl")
        recording_duration = request.values.get("RecordingDuration")  # in seconds
        recording_status = request.values.get("RecordingStatus")

        print(f"🎙️ Recording callback received:")
        print(f"   CallSid: {call_sid}")
        print(f"   RecordingSid: {recording_sid}")
        print(f"   Duration: {recording_duration}s")
        print(f"   Status: {recording_status}")
        print(f"   URL: {recording_url}")

        if recording_status != "completed":
            print(f"⚠️ Recording not completed, status: {recording_status}")
            return Response("OK", status=200)

        if not call_sid or not recording_sid:
            print(f"❌ Missing CallSid or RecordingSid")
            return Response("Missing data", status=400)

        # Update call_log with recording details
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute(
            """
            UPDATE call_log 
            SET recording_url = ?,
                recording_sid = ?,
                recording_duration = ?
            WHERE twilio_call_sid = ?
            """,
            (
                recording_url,
                recording_sid,
                int(recording_duration) if recording_duration else None,
                call_sid,
            ),
        )

        rows_updated = cur.rowcount
        conn.commit()
        conn.close()

        if rows_updated > 0:
            print(f"✅ Call log updated with recording details")

            # Start background download of recording
            if recording_url:
                thread = threading.Thread(
                    target=download_recording_async,
                    args=(recording_url, recording_sid, call_sid),
                )
                thread.daemon = True
                thread.start()
        else:
            print(f"⚠️ No call_log entry found for CallSid: {call_sid}")

        return Response("OK", status=200)

    except Exception as e:
        print(f"❌ Error in recording callback: {e}")
        import traceback

        traceback.print_exc()
        return Response("Error", status=500)


@call_recording_bp.route("/api/recordings/<call_id>")
def get_recording(call_id):
    """Get recording URL for a specific call"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT recording_url, recording_sid, recording_duration, recording_local_path
            FROM call_log 
            WHERE id = ?
            """,
            (call_id,),
        )
        row = cur.fetchone()
        conn.close()

        if not row:
            return {"error": "Call not found"}, 404

        return {
            "recording_url": row["recording_url"],
            "recording_sid": row["recording_sid"],
            "recording_duration": row["recording_duration"],
            "recording_local_path": row["recording_local_path"],
        }

    except Exception as e:
        return {"error": str(e)}, 500
