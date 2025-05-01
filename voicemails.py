from flask import Blueprint, request, Response, render_template_string, send_file, redirect, url_for
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
import os
import json
from datetime import datetime
from io import BytesIO
import requests
import pytz
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import shutil

voicemail_bp = Blueprint("voicemail", __name__)
VOICEMAIL_FILE = "voicemails.json"
RECORDINGS_DIR = "recordings"
SHARE_PATH = r"\\192.168.1.100\pc-reps\PC Reps\softphone\voicemails"

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

    if 10 <= hour < 18 and 1 <= weekday <= 5:  # Tues–Sat 10am–6pm
        resp.play("https://softphone-backend-host.onrender.com/open_greeting.mp3")
        resp.pause(length=1)
    else:
        resp.play("https://softphone-backend-host.onrender.com/closed_greeting.mp3")

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
    timestamp_pst = datetime.now(pst).strftime('%B %d, %Y — %I:%M %p %Z')

    local_filename = f"{RECORDINGS_DIR}/{recording_sid}.mp3"
    recording_response = requests.get(f"{recording_url}.mp3", auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
    if recording_response.status_code == 200:
        with open(local_filename, "wb") as f:
            f.write(recording_response.content)

    voicemail_entry = {
        "from": from_number,
        "recording_sid": recording_sid,
        "transcription": transcription,
        "timestamp": timestamp_pst
    }

    with open(VOICEMAIL_FILE, "r+") as f:
        data = json.load(f)
        data.append(voicemail_entry)
        f.seek(0)
        json.dump(data, f, indent=2)

    try:
        shutil.copy(local_filename, os.path.join(SHARE_PATH, f"{recording_sid}.mp3"))
        shutil.copy(VOICEMAIL_FILE, os.path.join(SHARE_PATH, "voicemails.json"))
    except Exception as e:
        print(f"Failed to sync voicemail to file share: {e}", flush=True)

    send_email_notification(voicemail_entry)
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
        <p><strong>Recording:</strong> <a href="https://softphone-backend.onrender.com/recording/{entry['recording_sid']}.mp3">Listen</a></p>
        """
        msg.attach(MIMEText(body, "html"))

        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
            server.sendmail(EMAIL_USERNAME, NOTIFY_EMAIL_TO, msg.as_string())
    except Exception as e:
        print(f"Failed to send email: {e}", flush=True)

@voicemail_bp.route("/voicemails", methods=["GET"])
def list_voicemails():
    with open(VOICEMAIL_FILE, "r") as f:
        data = json.load(f)

    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Voicemail Log</title>
        <link href="https://fonts.googleapis.com/css2?family=Audiowide&display=swap" rel="stylesheet">
        <style>
            body {
                font-family: 'Audiowide', cursive;
                background: linear-gradient(135deg, #0b0c2a, #1b1c4d);
                color: #ffffff;
                padding: 40px;
            }
            h1 {
                text-align: center;
                font-size: 36px;
                margin-bottom: 20px;
                color: #00ffff;
            }
            .logo {
                display: block;
                margin: 0 auto 30px auto;
                max-width: 300px;
            }
            .voicemail {
                background-color: rgba(255, 255, 255, 0.05);
                padding: 20px;
                border-radius: 10px;
                margin-bottom: 25px;
                box-shadow: 0 0 15px rgba(0,255,255,0.3);
            }
            .voicemail audio {
                width: 100%;
                margin-top: 10px;
            }
            .label {
                color: #00ffff;
            }
            .value {
                color: #ffffff;
            }
            .delete-btn, .sync-btn {
                margin-top: 10px;
                background: #0080ff;
                color: white;
                border: none;
                padding: 6px 10px;
                border-radius: 6px;
                cursor: pointer;
            }
            .delete-btn {
                background: red;
            }
        </style>
    </head>
    <body>
        <img src="/static/logo4_blue&white.png" alt="PC Reps Logo" class="logo">
        <h1>📡 Incoming Voicemails</h1>
        <form action="/voicemail/manual_sync" method="POST">
            <button class="sync-btn">🔁 Manual Sync</button>
        </form>
        {% for vm in voicemails %}
            <div class="voicemail">
                <div><span class="label">From:</span> <span class="value">{{ vm.from }}</span></div>
                <div><span class="label">Time:</span> <span class="value">{{ vm.timestamp }}</span></div>
                <div><span class="label">Recording:</span><br>
                    <audio controls>
                        <source src="/recording/{{ vm.recording_sid }}.mp3" type="audio/mpeg">
                    </audio>
                </div>
                <div><span class="label">Transcription:</span> <span class="value">{{ vm.transcription }}</span></div>
                <form method="POST" action="/voicemail/delete/{{ vm.recording_sid }}">
                    <button class="delete-btn" onclick="return confirm('Delete this voicemail?')">🗑️ Delete</button>
                </form>
            </div>
        {% endfor %}
    </body>
    </html>
    """
    return render_template_string(html, voicemails=data)

@voicemail_bp.route("/voicemail/manual_sync", methods=["POST"])
def manual_sync():
    try:
        with open(VOICEMAIL_FILE, "rb") as f:
            shutil.copyfileobj(f, open(os.path.join(SHARE_PATH, "voicemails.json"), "wb"))
        for file in os.listdir(RECORDINGS_DIR):
            if file.endswith(".mp3"):
                shutil.copy(os.path.join(RECORDINGS_DIR, file), os.path.join(SHARE_PATH, file))
    except Exception as e:
        return f"Manual sync failed: {e}", 500
    return redirect(url_for("voicemail.list_voicemails"))

@voicemail_bp.route("/voicemail/delete/<sid>", methods=["POST"])
def delete_voicemail(sid):
    with open(VOICEMAIL_FILE, "r+") as f:
        data = json.load(f)
        data = [vm for vm in data if vm["recording_sid"] != sid]
        f.seek(0)
        f.truncate()
        json.dump(data, f, indent=2)

    mp3_path = os.path.join(RECORDINGS_DIR, f"{sid}.mp3")
    if os.path.exists(mp3_path):
        os.remove(mp3_path)
    return redirect(url_for("voicemail.list_voicemails"))

@voicemail_bp.route("/recording/<sid>.mp3", methods=["GET"])
def serve_recording(sid):
    local_path = f"{RECORDINGS_DIR}/{sid}.mp3"
    if os.path.exists(local_path):
        return send_file(local_path, mimetype="audio/mpeg", download_name=f"{sid}.mp3")
    else:
        return f"Recording not found: {sid}", 404
