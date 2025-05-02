from flask import Blueprint, request, Response, render_template_string, send_file, redirect, url_for
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
import json
from datetime import datetime
import requests
import pytz
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import shutil
import os

voicemail_bp = Blueprint("voicemail", __name__)
RECORDINGS_DIR = "recordings"
VOICEMAIL_FILE = "voicemails.json"

# Twilio + Email Configuration
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", 465))
EMAIL_USERNAME = os.getenv("EMAIL_USERNAME")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
NOTIFY_EMAIL_TO = os.getenv("NOTIFY_EMAIL_TO")

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

os.makedirs(RECORDINGS_DIR, exist_ok=True)
if not os.path.exists(VOICEMAIL_FILE):
    with open(VOICEMAIL_FILE, "w") as f:
        json.dump([], f)

@voicemail_bp.route("/call/incoming", methods=["POST"])
def handle_incoming_call():
    resp = VoiceResponse()
    now = datetime.now(pytz.timezone("America/Los_Angeles"))
    hour = now.hour
    weekday = now.weekday()  # Monday = 0

    if 10 <= hour < 18 and 1 <= weekday <= 5:
        resp.play("/static/open_greeting.mp3")
    else:
        resp.play("/static/closed_greeting.mp3")

    resp.record(
        max_length=60,
        timeout=10,
        play_beep=True,
        transcribe=True,
        transcribe_callback="/voicemail/save"
    )
    resp.hangup()
    return Response(str(resp), status=200, mimetype="text/xml")

@voicemail_bp.route("/voicemail/save", methods=["POST"])
def save_voicemail():
    recording_url = request.form.get("RecordingUrl")
    recording_sid = recording_url.split("/")[-1] if recording_url else ""
    from_number = request.form.get("From")
    transcription = request.form.get("TranscriptionText", "(no transcription)")

    pst = pytz.timezone("America/Los_Angeles")
    timestamp = datetime.now(pytz.utc).astimezone(pst).strftime('%B %d, %Y — %I:%M %p %Z')

    local_filename = os.path.join(RECORDINGS_DIR, f"{recording_sid}.mp3")
    recording_response = requests.get(f"{recording_url}.mp3", auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
    if recording_response.status_code == 200:
        with open(local_filename, "wb") as f:
            f.write(recording_response.content)

    entry = {
        "from": from_number,
        "recording_sid": recording_sid,
        "transcription": transcription,
        "timestamp": timestamp
    }

    with open(VOICEMAIL_FILE, "r+") as f:
        data = json.load(f)
        data.append(entry)
        f.seek(0)
        json.dump(data, f, indent=2)

    send_email_notification(entry)
    sync_voicemail(recording_sid)

    return ("", 204)

def send_email_notification(entry):
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"New Voicemail from {entry['from']}"
        msg["From"] = EMAIL_USERNAME
        msg["To"] = NOTIFY_EMAIL_TO
        body = f"""
        <p><strong>From:</strong> {entry['from']}</p>
        <p><strong>Time:</strong> {entry['timestamp']}</p>
        <p><strong>Transcription:</strong> {entry['transcription']}</p>
        <p><strong>Recording:</strong> <a href='https://softphone-backend.onrender.com/recording/{entry['recording_sid']}.mp3'>Listen</a></p>
        """
        msg.attach(MIMEText(body, "html"))
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
            server.sendmail(EMAIL_USERNAME, NOTIFY_EMAIL_TO, msg.as_string())
    except Exception as e:
        print(f"Failed to send email: {e}", flush=True)

def sync_voicemail(recording_sid):
    try:
        source_path = os.path.join(RECORDINGS_DIR, f"{recording_sid}.mp3")
        sync_path = os.path.join(r"\\192.168.1.100\\pc-reps\\PC Reps\\softphone\\voicemails", f"{recording_sid}.mp3")
        shutil.copy(source_path, sync_path)
    except Exception as e:
        print(f"Failed to sync voicemail to file share: {e}", flush=True)

@voicemail_bp.route("/recording/<sid>.mp3", methods=["GET"])
def serve_recording(sid):
    local_path = os.path.join(RECORDINGS_DIR, f"{sid}.mp3")
    if os.path.exists(local_path):
        return send_file(local_path, mimetype="audio/mpeg", download_name=f"{sid}.mp3")
    else:
        return f"Recording not found: {sid}", 404
