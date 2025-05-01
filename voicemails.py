from flask import Blueprint, request, jsonify, Response, render_template_string, send_file
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client
import json
import os
from datetime import datetime
import requests
from io import BytesIO
from pytz import timezone, utc

voicemail_bp = Blueprint("voicemail", __name__)
VOICEMAIL_FILE = "voicemails.json"

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Ensure the voicemail file exists
if not os.path.exists(VOICEMAIL_FILE):
    with open(VOICEMAIL_FILE, "w") as f:
        json.dump([], f)

@voicemail_bp.route("/call/incoming", methods=["POST"])
def handle_incoming_call():
    print("📞 Incoming call handler hit!", flush=True)

    now = datetime.now()
    hour = now.hour
    weekday = now.weekday()  # Monday = 0, Sunday = 6

    resp = VoiceResponse()

    if 10 <= hour < 18 and 1 <= weekday <= 5:
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

    print("🔊 TwiML sent to Twilio:", flush=True)
    print(str(resp), flush=True)

    return Response(str(resp), status=200, mimetype="text/xml")

@voicemail_bp.route("/voicemail/save", methods=["POST"])
def save_voicemail():
    recording_url = request.form.get("RecordingUrl")
    recording_sid = recording_url.split("/")[-1] if recording_url else ""
    from_number = request.form.get("From")
    transcription = request.form.get("TranscriptionText", "(no transcription)")
    timestamp = datetime.utcnow().isoformat()

    voicemail_entry = {
        "from": from_number,
        "recording_sid": recording_sid,
        "transcription": transcription,
        "timestamp": timestamp
    }

    with open(VOICEMAIL_FILE, "r+") as f:
        data = json.load(f)
        data.append(voicemail_entry)
        f.seek(0)
        json.dump(data, f, indent=2)

    return ("", 204)

@voicemail_bp.route("/voicemails", methods=["GET"])
def list_voicemails():
    with open(VOICEMAIL_FILE, "r") as f:
        data = json.load(f)

    pacific = timezone('US/Pacific')
    for vm in data:
        utc_time = datetime.fromisoformat(vm["timestamp"])
        local_time = utc_time.replace(tzinfo=utc).astimezone(pacific)
        vm["date"] = local_time.strftime("%B %d, %Y")  # May 01, 2025
        vm["time"] = local_time.strftime("%I:%M %p %Z")  # 11:23 AM PDT

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
                margin-bottom: 40px;
                color: #00ffff;
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
        </style>
    </head>
    <body>
        <h1>📡 Incoming Voicemails</h1>
        {% for vm in voicemails %}
            <div class="voicemail">
                <div><span class="label">From:</span> <span class="value">{{ vm.from }}</span></div>
                <div><span class="label">Date:</span> <span class="value">{{ vm.date }}</span></div>
                <div><span class="label">Time:</span> <span class="value">{{ vm.time }}</span></div>
                <div><span class="label">Recording:</span><br><audio controls><source src="/recording/{{ vm.recording_sid }}.mp3" type="audio/mpeg"></audio></div>
                <div><span class="label">Transcription:</span> <span class="value">{{ vm.transcription }}</span></div>
            </div>
        {% endfor %}
    </body>
    </html>
    """
    return render_template_string(html, voicemails=data)

@voicemail_bp.route("/recording/<sid>.mp3", methods=["GET"])
def proxy_recording(sid):
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Recordings/{sid}.mp3"
    response = requests.get(url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))

    if response.status_code == 200:
        return send_file(BytesIO(response.content), mimetype="audio/mpeg", download_name=f"{sid}.mp3")
    else:
        return f"Failed to retrieve recording (status {response.status_code})", 500
