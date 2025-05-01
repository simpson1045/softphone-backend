# test.py
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse
from voicemails import voicemail_bp

app = Flask(__name__)
app.register_blueprint(voicemail_bp)

# Token route
@app.route("/token", methods=["GET"])
def generate_token():
    identity = request.args.get("identity", "softphone_user")

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    api_key = os.getenv("TWILIO_API_KEY")
    api_secret = os.getenv("TWILIO_API_SECRET")
    app_sid = os.getenv("TWILIO_APP_SID")

    token = AccessToken(account_sid, api_key, api_secret, identity=identity)

    voice_grant = VoiceGrant(outgoing_application_sid=app_sid)
    token.add_grant(voice_grant)

    return jsonify({
        "identity": identity,
        "token": token.to_jwt()
    })
#@app.route("/call/incoming", methods=["POST"])
#def incoming_call():
 #   print("📞 /call/incoming was hit!", flush=True)
#
   # resp = VoiceResponse()
  #  resp.say("This is your test server. Twilio is connected.", voice="alice")
 #   resp.hangup()
#
  #  print("🔊 TwiML returned to Twilio:", flush=True)
 #   print(str(resp), flush=True)
#
   # return Response(str(resp), mimetype="text/xml")

@app.route("/", methods=["GET"])
def health_check():
    return "✅ Flask test server is live", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
