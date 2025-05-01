# test.py
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse
from voicemails import voicemail_bp

app = Flask(__name__)

@app.route("/call/incoming", methods=["POST"])
def incoming_call():
    print("📞 /call/incoming was hit!", flush=True)

    resp = VoiceResponse()
    resp.say("This is your test server. Twilio is connected.", voice="alice")
    resp.hangup()

    print("🔊 TwiML returned to Twilio:", flush=True)
    print(str(resp), flush=True)

    return Response(str(resp), mimetype="text/xml")

@app.route("/", methods=["GET"])
def health_check():
    return "✅ Flask test server is live", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
