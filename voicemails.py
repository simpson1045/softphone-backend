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
from smbprotocol.connection import Connection
from smbprotocol.session import Session
from smbprotocol.tree import TreeConnect
from smbprotocol.open import Open, CreateOptions, FileAttributes, FilePipePrinterAccessMask, ShareAccess, CreateDisposition

voicemail_bp = Blueprint("voicemail", __name__)
LOCAL_JSON_PATH = "voicemails.json"
LOCAL_MP3_DIR = "recordings"
REMOTE_SHARE = r"\\\\192.168.1.100\\pc-reps\\PC Reps\\softphone\\voicemails"

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", 465))
EMAIL_USERNAME = os.getenv("EMAIL_USERNAME")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
NOTIFY_EMAIL_TO = os.getenv("NOTIFY_EMAIL_TO")

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
os.makedirs(LOCAL_MP3_DIR, exist_ok=True)
if not os.path.exists(LOCAL_JSON_PATH):
    with open(LOCAL_JSON_PATH, "w") as f:
        json.dump([], f)

@voicemail_bp.route("/call/incoming", methods=["POST"])
def handle_incoming_call():
    resp = VoiceResponse()
    now = datetime.now(pytz.timezone("America/Los_Angeles"))
    hour = now.hour
    weekday = now.weekday()
    if 1 <= weekday <= 5 and 10 <= hour < 18:
        resp.play("/static/open_greeting.mp3")
        resp.pause(length=1)
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
    sid = request.form.get("RecordingSid")
    url = request.form.get("RecordingUrl") + ".mp3"
    caller = request.form.get("From")
    transcription = request.form.get("TranscriptionText", "(no transcription)")
    timestamp = datetime.utcnow().astimezone(pytz.timezone("America/Los_Angeles")).strftime("%B %d, %Y — %I:%M %p %Z")
    filename = f"{sid}.mp3"
    local_path = os.path.join(LOCAL_MP3_DIR, filename)
    r = requests.get(url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
    if r.status_code == 200:
        with open(local_path, "wb") as f:
            f.write(r.content)
    entry = {
        "from": caller,
        "recording_sid": sid,
        "transcription": transcription,
        "timestamp": timestamp
    }
    with open(LOCAL_JSON_PATH, "r+") as f:
        data = json.load(f)
        data.append(entry)
        f.seek(0)
        json.dump(data, f, indent=2)
    send_email_notification(entry)
    try_sync_to_file_share(filename, local_path)
    return ("", 204)

def send_email_notification(entry):
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"New Voicemail from {entry['from']}"
        msg["From"] = EMAIL_USERNAME
        msg["To"] = NOTIFY_EMAIL_TO
        html = f"""<p><strong>From:</strong> {entry['from']}</p>
<p><strong>Time:</strong> {entry['timestamp']}</p>
<p><strong>Transcription:</strong> {entry['transcription']}</p>
<p><strong>Recording:</strong> <a href="https://softphone-backend.onrender.com/recording/{entry['recording_sid']}.mp3">Listen</a></p>"""
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
            server.sendmail(EMAIL_USERNAME, NOTIFY_EMAIL_TO, msg.as_string())
    except Exception as e:
        print(f"Email failed: {e}", flush=True)

def try_sync_to_file_share(name, local_path):
    try:
        conn = Connection(uuid="sync-conn", server="192.168.1.100", port=445)
        conn.connect()
        session = Session(conn, username="Administrator", password="Computerrepair2019@")
        session.connect()
        tree = TreeConnect(session, REMOTE_SHARE)
        tree.connect()
        file = Open(tree, name, access=FilePipePrinterAccessMask.GENERIC_WRITE,
                    disposition=CreateDisposition.FILE_OVERWRITE_IF,
                    options=CreateOptions.NON_DIRECTORY_FILE,
                    share=ShareAccess.FILE_SHARE_WRITE)
        file.create()
        with open(local_path, "rb") as f:
            file.write(f.read(), 0)
        file.close()
    except Exception as e:
        print(f"Failed to sync voicemail to file share: {e}", flush=True)

@voicemail_bp.route("/voicemails", methods=["GET", "POST"])
def list_voicemails():
    search = request.form.get("search", "").lower()
    with open(LOCAL_JSON_PATH) as f:
        data = json.load(f)
    if search:
        data = [vm for vm in data if search in vm["from"].lower() or search in vm["timestamp"].lower()]
    return render_template_string(open("templates/voicemails.html").read(), voicemails=data)

@voicemail_bp.route("/recording/<sid>.mp3")
def serve_recording(sid):
    path = os.path.join(LOCAL_MP3_DIR, f"{sid}.mp3")
    return send_file(path, mimetype="audio/mpeg") if os.path.exists(path) else ("Not found", 404)

@voicemail_bp.route("/voicemail/delete/<sid>", methods=["POST"])
def delete_voicemail(sid):
    path = os.path.join(LOCAL_MP3_DIR, f"{sid}.mp3")
    if os.path.exists(path): os.remove(path)
    with open(LOCAL_JSON_PATH, "r+") as f:
        data = json.load(f)
        data = [v for v in data if v["recording_sid"] != sid]
        f.seek(0)
        f.truncate()
        json.dump(data, f, indent=2)
    return redirect(url_for("voicemail.list_voicemails"))

@voicemail_bp.route("/voicemail/manual-sync")
def manual_sync_all():
    with open(LOCAL_JSON_PATH) as f:
        for vm in json.load(f):
            mp3 = f"{vm['recording_sid']}.mp3"
            path = os.path.join(LOCAL_MP3_DIR, mp3)
            if os.path.exists(path):
                try_sync_to_file_share(mp3, path)
    return redirect(url_for("voicemail.list_voicemails"))
