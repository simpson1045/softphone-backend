from flask import Blueprint, request, Response, render_template_string, send_file, redirect, url_for, jsonify
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
from smbprotocol.open import Open, CreateOptions, FileAttributes, FilePipePrinterAccessMask, ShareAccess, CreateDisposition, DirectoryAccessMask

voicemail_bp = Blueprint("voicemail", __name__)
VOICEMAIL_FILE = "voicemails.json"
RECORDINGS_DIR = "recordings"

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", 465))
EMAIL_USERNAME = os.getenv("EMAIL_USERNAME")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
NOTIFY_EMAIL_TO = os.getenv("NOTIFY_EMAIL_TO")

FILESHARE_USER = "Administrator"
FILESHARE_PASS = "Computerrepair2019@"
FILESHARE_PATH = r"\192.168.1.100\pc-reps\PC Reps\softphoneoicemails"

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
os.makedirs(RECORDINGS_DIR, exist_ok=True)
if not os.path.exists(VOICEMAIL_FILE):
    with open(VOICEMAIL_FILE, "w") as f:
        json.dump([], f)

@voicemail_bp.route("/call/incoming", methods=["POST"])
def handle_incoming_call():
    resp = VoiceResponse()
    pst = pytz.timezone("America/Los_Angeles")
    now = datetime.now(pst)
    hour = now.hour
    weekday = now.weekday()

    if 10 <= hour < 18 and 1 <= weekday <= 5:
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
    recording_url = request.form.get("RecordingUrl")
    recording_sid = recording_url.split("/")[-1] if recording_url else ""
    from_number = request.form.get("From")
    transcription = request.form.get("TranscriptionText", "(no transcription)")

    utc = pytz.utc
    pst = pytz.timezone("America/Los_Angeles")
    timestamp_utc = datetime.utcnow().replace(tzinfo=utc)
    timestamp_pst = timestamp_utc.astimezone(pst).strftime('%B %d, %Y — %I:%M %p %Z')

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

    send_email_notification(voicemail_entry)
    sync_to_file_server(local_filename, recording_sid)

    return ("", 204)

def sync_to_file_server(local_path, sid):
    try:
        conn = Connection(uuid="", username=FILESHARE_USER, password=FILESHARE_PASS, server="192.168.1.100")
        conn.connect()
        session = Session(conn, FILESHARE_USER, FILESHARE_PASS)
        session.connect()
        tree = TreeConnect(session, FILESHARE_PATH)
        tree.connect()
        share_file = Open(tree, f"{sid}.mp3", access=FilePipePrinterAccessMask.GENERIC_WRITE,
                          options=CreateOptions.FILE_NON_DIRECTORY_FILE,
                          attributes=FileAttributes.ARCHIVE,
                          share=ShareAccess.FILE_SHARE_WRITE,
                          disposition=CreateDisposition.FILE_OVERWRITE_IF)
        share_file.create()
        with open(local_path, "rb") as f:
            share_file.write(f.read(), 0)
        share_file.close()
    except Exception as e:
        print(f"Failed to sync voicemail to file share: {e}", flush=True)

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

@voicemail_bp.route("/recording/<sid>.mp3", methods=["GET"])
def serve_recording(sid):
    local_path = f"{RECORDINGS_DIR}/{sid}.mp3"
    if os.path.exists(local_path):
        return send_file(local_path, mimetype="audio/mpeg", download_name=f"{sid}.mp3")
    else:
        return f"Recording not found: {sid}", 404

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

@voicemail_bp.route("/voicemails/download-all", methods=["GET"])
def download_all_voicemails():
    from zipfile import ZipFile
    zip_buffer = BytesIO()
    with ZipFile(zip_buffer, "w") as zipf:
        for file in os.listdir(RECORDINGS_DIR):
            if file.endswith(".mp3"):
                zipf.write(os.path.join(RECORDINGS_DIR, file), arcname=file)
    zip_buffer.seek(0)
    return send_file(zip_buffer, mimetype="application/zip", as_attachment=True, download_name="voicemails.zip")
