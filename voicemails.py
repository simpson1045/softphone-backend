from flask import Blueprint, request, Response, send_file, redirect, url_for, jsonify
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
from dotenv import load_dotenv
import os
import json
from datetime import datetime, timedelta
from io import BytesIO
import requests
import pytz
from zipfile import ZipFile
from dateutil import parser
import traceback
import time
import re
from faster_whisper import WhisperModel
import threading
from database import get_db_connection
from phone_utils import normalize_phone_number, get_contact_name
from twilio_security import validate_twilio_request

load_dotenv()

voicemail_bp = Blueprint("voicemail", __name__)
RECORDINGS_DIR = os.path.join("recordings", "voicemails")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Ensure recordings directory exists
os.makedirs(RECORDINGS_DIR, exist_ok=True)



# normalize_phone_number and get_contact_name imported from phone_utils


def update_voicemail_notification_count():
    """Update the localStorage notification count for new voicemails"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as cnt FROM voicemails WHERE is_read = 0")
        row = cur.fetchone()
        unread_count = row["cnt"] if row else 0
        conn.close()

        print(f"Unread voicemails: {unread_count}")
        return unread_count
    except Exception as e:
        print(f"Warning: Error updating voicemail count: {e}")
        return 0



# Whisper model singleton — loaded once, kept in memory
# With 160GB RAM and RTX 3090, this should be GPU-accelerated
_whisper_model = None


def _check_cuda_available():
    """Check if CUDA and cuDNN are properly available before attempting GPU mode.

    The cuDNN check is critical — if cuDNN DLLs are missing, CTranslate2 will crash
    at the C level (not a Python exception), killing the entire Flask process.
    """
    try:
        import ctypes
        ctypes.cdll.LoadLibrary("cudnn_ops64_9.dll")
        ctypes.cdll.LoadLibrary("cudnn_cnn64_9.dll")
        return True
    except OSError as e:
        print(f"⚠️ cuDNN DLLs not found: {e}")
        return False


def _get_whisper_model():
    """Get or create the cached Whisper model (singleton)"""
    global _whisper_model
    if _whisper_model is None:
        if _check_cuda_available():
            try:
                _whisper_model = WhisperModel("large-v3", device="cuda", compute_type="float16")
                print("🎤 Whisper model loaded (GPU - CUDA float16)")
            except Exception as e:
                print(f"⚠️ GPU Whisper failed ({e}), falling back to CPU")
                _whisper_model = WhisperModel("large-v3", device="cpu", compute_type="int8")
                print("🎤 Whisper model loaded (CPU - int8 fallback)")
        else:
            print("⚠️ cuDNN not available, using CPU for Whisper")
            _whisper_model = WhisperModel("large-v3", device="cpu", compute_type="int8")
            print("🎤 Whisper model loaded (CPU - int8 fallback)")
    return _whisper_model


def transcribe_with_whisper(audio_file_path, voicemail_id):
    """Transcribe audio file using local Whisper model"""
    try:
        print(f"Starting Whisper transcription for voicemail {voicemail_id}")

        model = _get_whisper_model()

        # Transcribe the audio
        segments, info = model.transcribe(audio_file_path, language="en")

        # Combine all segments into one transcript
        transcript = " ".join([segment.text.strip() for segment in segments])

        print(f"Whisper transcription complete: {transcript[:100]}...")

        # Update database with new transcription
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE voicemails 
            SET transcription = ?, whisper_transcribed = 1 
            WHERE id = ?
        """,
            (transcript, voicemail_id),
        )
        conn.commit()
        conn.close()

        print(f"Database updated for voicemail {voicemail_id}")

    except Exception as e:
        print(f"Whisper transcription failed for voicemail {voicemail_id}: {e}")


def format_datetime(value):
    try:
        dt = parser.parse(value)
        return dt.strftime("%B %d, %Y - %I:%M %p")
    except Exception:
        return value


voicemail_bp.add_app_template_filter(format_datetime)


@voicemail_bp.route("/voicemail/save", methods=["POST"])
@validate_twilio_request
def save_voicemail():
    print("Hit /voicemail/save route")
    print(f"Incoming form data: {dict(request.form)}")

    recording_url = request.form.get("RecordingUrl")
    recording_sid = request.form.get("RecordingSid") or (
        recording_url.split("/")[-1] if recording_url else ""
    )
    from_number = request.form.get("From")
    to_number = request.form.get("To")
    call_sid = request.form.get("CallSid")
    transcription = request.form.get("TranscriptionText", "")

    # Get timezone-aware timestamp
    pst = pytz.timezone("America/Los_Angeles")
    timestamp = datetime.now(pytz.utc).astimezone(pst).isoformat()

    local_filename = os.path.join(RECORDINGS_DIR, f"{recording_sid}.mp3")

    print(f"Recording URL: {recording_url}")
    print(f"Downloading {recording_url} to {local_filename}")

    # Download the recording
    try:
        if recording_url:
            r = requests.get(
                recording_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            )
            print(f"Download status: {r.status_code}")
            if r.status_code == 200:
                with open(local_filename, "wb") as f:
                    f.write(r.content)
                print("MP3 saved")
            else:
                print("MP3 download failed")
                local_filename = None
        else:
            print("No recording URL provided")
            local_filename = None
    except Exception as e:
        print(f"Error downloading MP3: {e}")
        traceback.print_exc()
        local_filename = None

    # Normalize phone number and get contact info
    normalized_phone = normalize_phone_number(from_number)
    contact_name = get_contact_name(from_number)

    # Store voicemail in master database
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Insert the voicemail record
        cur.execute(
            """
            INSERT INTO voicemails (
                phone_number, caller_name, recording_sid, recording_url, 
                local_filename, transcription, call_sid, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
        """,
            (
                normalized_phone,
                contact_name,
                recording_sid,
                recording_url,
                local_filename,
                transcription,
                call_sid,
                timestamp,
            ),
        )

        result = cur.fetchone()
        voicemail_id = result["id"] if result else None
        conn.commit()
        conn.close()
        print("Voicemail saved to database")

        # Start Whisper transcription if audio file exists
        if voicemail_id and local_filename and os.path.exists(local_filename):
            thread = threading.Thread(
                target=transcribe_with_whisper, args=(local_filename, voicemail_id)
            )
            thread.daemon = True
            thread.start()
            print("Started Whisper transcription in background")

        # Update notification count and emit real-time update
        unread_count = update_voicemail_notification_count()
        try:
            from flask import current_app

            socketio = current_app.extensions.get("socketio")
            if socketio:
                socketio.emit(
                    "voicemail_notification_update", {"unread_count": unread_count}
                )
                print(f"Emitted voicemail notification update: {unread_count}")
        except Exception as e:
            print(f"Error emitting voicemail notification: {e}")

    except Exception as e:
        print(f"Failed to save voicemail to database: {e}")
        traceback.print_exc()

    return ("", 204)


@voicemail_bp.route("/recording/complete", methods=["POST"])
@validate_twilio_request
def recording_complete():
    """Handle recording completion callback"""
    print("Hit /recording/complete route")
    print(f"Recording complete data: {dict(request.form)}")

    recording_sid = request.form.get("RecordingSid")
    recording_url = request.form.get("RecordingUrl")
    recording_status = request.form.get("RecordingStatus")
    recording_duration = request.form.get("RecordingDuration", "0")

    print(f"Recording {recording_sid} completed with status: {recording_status}")
    print(f"Duration: {recording_duration} seconds")

    # Update the voicemail record if needed
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE voicemails 
            SET recording_url = ?, transcription = COALESCE(transcription, 'Processing transcription...')
            WHERE recording_sid = ?
        """,
            (recording_url, recording_sid),
        )
        conn.commit()
        conn.close()

    except Exception as e:
        print(f"Error updating voicemail record: {e}")

    return ("", 204)


@voicemail_bp.route("/voicemails/api", methods=["GET"])
def get_voicemails_json():
    """Get voicemails from master database with optional pagination.

    Query params:
        page (int): Page number, default 1
        per_page (int): Items per page, default 50, max 200
        If no page param is provided, returns all (backward compatible).
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        page = request.args.get("page", type=int)
        per_page = request.args.get("per_page", 50, type=int)
        per_page = min(per_page, 200)

        if page:
            # Paginated mode
            offset = (page - 1) * per_page

            cur.execute("SELECT COUNT(*) as total FROM voicemails")
            total = cur.fetchone()["total"]

            cur.execute(
                "SELECT * FROM voicemails ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (per_page, offset),
            )
            rows = cur.fetchall()
            conn.close()

            voicemails = []
            for row in rows:
                voicemail = dict(row)
                voicemail["from"] = voicemail["phone_number"]
                voicemails.append(voicemail)

            return jsonify({
                "voicemails": voicemails,
                "pagination": {
                    "page": page,
                    "per_page": per_page,
                    "total": total,
                    "pages": (total + per_page - 1) // per_page,
                },
            })
        else:
            # Non-paginated mode (backward compatible)
            cur.execute("SELECT * FROM voicemails ORDER BY timestamp DESC")
            rows = cur.fetchall()
            conn.close()

            voicemails = []
            for row in rows:
                voicemail = dict(row)
                voicemail["from"] = voicemail["phone_number"]
                voicemails.append(voicemail)

            return jsonify(voicemails)

    except Exception as e:
        print(f"Error fetching voicemails: {e}")
        return jsonify([])


@voicemail_bp.route("/recording/<sid>.mp3", methods=["GET"])
def serve_recording(sid):
    """Serve local recording file"""
    local_path = os.path.join(RECORDINGS_DIR, f"{sid}.mp3")
    if os.path.exists(local_path):
        return send_file(local_path, mimetype="audio/mpeg", download_name=f"{sid}.mp3")
    else:
        print(f"Recording not found: {local_path}")
        return f"Recording not found: {sid}", 404


@voicemail_bp.route("/voicemail/delete/<sid>", methods=["POST"])
def delete_voicemail(sid):
    """Delete voicemail from database and file system"""
    try:
        # Remove from database
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM voicemails WHERE recording_sid = ?", (sid,))
        conn.commit()
        conn.close()

        # Remove local file
        mp3_path = os.path.join(RECORDINGS_DIR, f"{sid}.mp3")
        if os.path.exists(mp3_path):
            os.remove(mp3_path)

        return jsonify({"status": "deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@voicemail_bp.route("/voicemails/mark-read/<int:voicemail_id>", methods=["POST"])
def mark_voicemail_read(voicemail_id):
    """Mark voicemail as read"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE voicemails 
            SET is_read = 1 
            WHERE id = ?
        """,
            (voicemail_id,),
        )
        conn.commit()
        conn.close()

        # Update notification count
        unread_count = update_voicemail_notification_count()
        try:
            from flask import current_app

            socketio = current_app.extensions.get("socketio")
            if socketio:
                socketio.emit(
                    "voicemail_notification_update", {"unread_count": unread_count}
                )
        except Exception as e:
            print(f"Error emitting voicemail update: {e}")

        return jsonify({"status": "marked_read", "unread_count": unread_count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@voicemail_bp.route("/voicemails/unread-count", methods=["GET"])
def get_unread_voicemail_count():
    """Get count of unread voicemails"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as cnt FROM voicemails WHERE is_read = 0")
        row = cur.fetchone()
        count = row["cnt"] if row else 0
        conn.close()
        return jsonify({"count": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@voicemail_bp.route("/voicemails/download-all", methods=["GET"])
def download_all_voicemails():
    """Download all voicemails as a ZIP file"""
    zip_buffer = BytesIO()
    with ZipFile(zip_buffer, "w") as zipf:
        for file in os.listdir(RECORDINGS_DIR):
            if file.endswith(".mp3"):
                zipf.write(os.path.join(RECORDINGS_DIR, file), arcname=file)
    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name="voicemails.zip",
    )


@voicemail_bp.route("/voicemail/retranscribe/<int:voicemail_id>", methods=["POST"])
def retranscribe_voicemail(voicemail_id):
    """Re-transcribe a voicemail using Whisper"""
    try:
        # Get voicemail info
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM voicemails WHERE id = ?", (voicemail_id,))
        voicemail = cur.fetchone()
        conn.close()

        if not voicemail:
            return jsonify({"error": "Voicemail not found"}), 404

        if not voicemail["local_filename"] or not os.path.exists(
            voicemail["local_filename"]
        ):
            return jsonify({"error": "Audio file not found"}), 404

        # Start transcription in background thread
        thread = threading.Thread(
            target=transcribe_with_whisper,
            args=(voicemail["local_filename"], voicemail_id),
        )
        thread.daemon = True
        thread.start()

        return jsonify({"status": "transcription_started"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@voicemail_bp.route("/voicemails/stats", methods=["GET"])
def get_voicemail_stats():
    """Get voicemail statistics"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Total count
        cur.execute("SELECT COUNT(*) as cnt FROM voicemails")
        row = cur.fetchone()
        total = row["cnt"] if row else 0

        # Unread count
        cur.execute("SELECT COUNT(*) as cnt FROM voicemails WHERE is_read = 0")
        row = cur.fetchone()
        unread = row["cnt"] if row else 0

        # Recent count (last 7 days)
        week_ago = (datetime.now() - timedelta(days=7)).isoformat()
        cur.execute(
            "SELECT COUNT(*) as cnt FROM voicemails WHERE timestamp > ?", (week_ago,)
        )
        row = cur.fetchone()
        recent = row["cnt"] if row else 0

        conn.close()
        return jsonify({"total": total, "unread": unread, "recent": recent})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
