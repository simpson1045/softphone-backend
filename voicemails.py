from flask import Blueprint, request, jsonify, Response, render_template_string
from twilio.twiml.voice_response import VoiceResponse
import json
import os
from datetime import datetime

voicemail_bp = Blueprint("voicemail", __name__)
VOICEMAIL_FILE = "voicemails.json"

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
        # Open hours: Tuesday–Saturday, 10am–6pm
        resp.play("https://softphone-backend-host.onrender.com/open_greeting.mp3")
        resp.pause(length=1)
    else:
        # Closed hours or Sunday/Monday
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
    from_number = request.form.get("From")
    transcription = request.form.get("TranscriptionText", "(no transcription)")
    timestamp = datetime.utcnow().isoformat()

    voicemail_entry = {
        "from": from_number,
        "recording_url": recording_url,
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

    html = """
    <html>
    <head><title>Voicemails</title></head>
    <body>
    <h1>Voicemail Log</h1>
    {% for vm in voicemails %}
        <div style='margin-bottom:20px;'>
            <strong>From:</strong> {{ vm.from }}<br>
            <strong>Time:</strong> {{ vm.timestamp }}<br>
            <strong>Recording:</strong> <a href="{{ vm.recording_url }}">Listen</a><br>
            <strong>Transcription:</strong> {{ vm.transcription }}<br>
        </div>
    {% endfor %}
    </body>
    </html>
    """
    return render_template_string(html, voicemails=data)
