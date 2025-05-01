from flask import Blueprint, request, jsonify, Response
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
        play_beep=True
        # You can re-add transcribe=True and transcribe_callback if needed later
    )
    resp.hangup()

    print("🔊 TwiML sent to Twilio:", flush=True)
    print(str(resp), flush=True)

    return Response(str(resp), status=200, mimetype="text/xml")

@voicemail_bp.route("/voicemails", methods=["GET"])
def list_voicemails():
    with open(VOICEMAIL_FILE, "r") as f:
        data = json.load(f)
    return jsonify(data)
