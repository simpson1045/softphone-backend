import os
import json
import logging
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_from_directory, Response
import requests
from flask_cors import CORS
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.twiml.voice_response import VoiceResponse
from flask_socketio import SocketIO
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.serving import WSGIRequestHandler
from flask_login import current_user

# Import blueprints
from messaging import messaging_bp
# messages_routes removed — all routes already exist in messaging.py (was causing duplicate registration)
# from messages_routes import messages_api
from import_contacts import import_contacts_bp
from export_contacts import export_contacts_bp
from incoming import incoming_bp
from voicemails import voicemail_bp
from address_book import address_book_bp
from auth import auth_bp, init_login_manager
from database import get_db_connection
from call_recording import call_recording_bp
from twilio_security import validate_twilio_request
from tenant_context import current_tenant_id, tenant_by_phone

import sys

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "shared"))
from scanner_blocker import scanner_blocker_bp

FLASK_INSTANCE_ID = os.getpid()
print(f"🚀 Flask instance {FLASK_INSTANCE_ID} starting...")

load_dotenv()

# Database is now PostgreSQL - see database.py

# Flask app setup
react_build_dir = os.path.join(
    os.path.dirname(__file__), "..", "softphone-frontend", "dist"
)
app = Flask(__name__, static_folder=react_build_dir, static_url_path="/~build~")
app.secret_key = os.getenv("FLASK_SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("FLASK_SECRET_KEY environment variable is not set — add it to .env")
CORS(app, origins=["https://softphone.pc-reps.com", "http://localhost:5173", "http://localhost:5000"])

# Proxy fix for production - trust 1 proxy (Caddy)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Disable default Flask request logging to use custom logging with real IPs
log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)


# Custom request logging that uses the real IP from ProxyFix
@app.after_request
def log_request(response):
    # Suppress noisy routes
    suppress_paths = [
        "/voicemails/api",
        "/voicemails/unread-count",
        "/messages/threads",
        "/messages/thread/",
        "/messages/reactions/",
        "/messages/pinned/",
        "/api/calls",
        "/messages/mark-read",
        "/socket.io/",
        "/api/settings/dnd",  # ADD THIS LINE
    ]

    # Skip logging if path matches suppression list
    if not any(path in request.path for path in suppress_paths):
        real_ip = request.remote_addr
        timestamp = datetime.now().strftime("%d/%b/%Y %H:%M:%S")
        print(
            f'{real_ip} - - [{timestamp}] "{request.method} {request.full_path.rstrip("?")} HTTP/1.1" {response.status_code} -'
        )

    return response


# Initialize SocketIO
socketio = SocketIO(
    app,
    cors_allowed_origins=["https://softphone.pc-reps.com", "http://localhost:5173", "http://localhost:5000"],
    logger=False,
    engineio_logger=False,
    async_mode="threading",
    ping_timeout=60,
    ping_interval=25,
)

# Initialize Flask-Login
init_login_manager(app)


# Logging setup - suppress noisy routes
class RouteFilter(logging.Filter):
    def filter(self, record):
        suppress_paths = [
            "/voicemails/api",
            "/voicemails/unread-count",
            "/messages/threads",
            "/messages/thread/",
            "/messages/reactions/",
            "/messages/pinned/",
            "/api/calls",
            "/messages/mark-read",
            "/socket.io/",
        ]
        return not any(path in record.getMessage() for path in suppress_paths)


logging.getLogger("werkzeug").addFilter(RouteFilter())

# Register blueprints
app.register_blueprint(incoming_bp)
app.register_blueprint(voicemail_bp)
app.register_blueprint(address_book_bp)
app.register_blueprint(scanner_blocker_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(messaging_bp)
# app.register_blueprint(messages_api)  # Removed — duplicate of routes in messaging_bp
app.register_blueprint(import_contacts_bp)
app.register_blueprint(export_contacts_bp)
app.register_blueprint(call_recording_bp)


# ─── SEC-5: Global authentication enforcement ───
# Protects all API routes. SPA/static routes are unprotected (React handles its own auth).
# Twilio webhooks are whitelisted since they're called by Twilio servers (no browser session).

# API paths that REQUIRE authentication
PROTECTED_API_PREFIXES = (
    "/api/",              # All /api/* endpoints (calls, settings, greetings, analytics, etc.)
    "/voice/",            # Twilio token endpoint
    "/messages/",         # Messaging routes (threads, send, mark-read, search, etc.)
    "/address-book/",     # Contacts CRUD
    "/voicemails/",       # Voicemail list, mark-read, stats
    "/media-proxy",       # Media proxy
    "/contacts/",         # Contact search, flags
    "/flag-types",        # Flag management
    "/thread/",           # Thread redirect
    "/test-",             # Test endpoints
)

# Paths that look like API routes but must stay public (Twilio webhooks, auth)
PUBLIC_EXCEPTIONS = (
    "/api/auth/login",
    "/api/auth/check",
    "/api/external/send-sms",       # Has its own API key validation
    "/messages/incoming",           # Twilio SMS webhook
    "/messages/status",             # Twilio SMS status callback
    "/messages/test",               # SMS test endpoint
)

@app.before_request
def require_authentication():
    """Enforce login on API routes, let SPA/static/webhooks through"""
    path = request.path

    # Allow CORS preflight
    if request.method == "OPTIONS":
        return None

    # Check if this is a protected API path
    if any(path.startswith(prefix) for prefix in PROTECTED_API_PREFIXES):
        # Allow public exceptions (Twilio webhooks, login)
        if any(path.startswith(exc) for exc in PUBLIC_EXCEPTIONS):
            return None

        # Require authentication for everything else
        if not current_user.is_authenticated:
            return jsonify({"error": "Authentication required"}), 401

    # Non-API paths (SPA routes, static files, Twilio voice webhooks) pass through
    return None


@app.before_request
def resolve_tenant_from_webhook():
    """Set g.tenant_id from the inbound `To` number on Twilio webhooks.

    Twilio webhooks (POST /incoming, /dial-status, /messages/incoming, etc.)
    don't have a logged-in user, so without this hook every webhook would
    fall back to the pc_reps tenant via tenant_context.current_tenant_id().

    Reading `To` from request.values lets us route HaniTech inbound traffic
    (To = +17756185775) to the hanitech tenant and PC Reps inbound traffic
    (To = +17754602190) to pc_reps. If `To` doesn't match any tenant or
    isn't present, we leave g.tenant_id unset and the fallback chain in
    tenant_context handles it (current_user.tenant_id, then pc_reps).
    """
    from flask import g

    # Only Twilio POSTs include a "To" form field. Skip GETs and non-form requests fast.
    if request.method != "POST":
        return None

    to_number = request.values.get("To") or request.form.get("To")
    if not to_number:
        return None

    try:
        tenant = tenant_by_phone(to_number)
    except Exception as e:
        print(f"⚠️ tenant lookup failed for To={to_number}: {e}")
        return None

    if tenant:
        g.tenant_id = tenant["id"]
        # Quiet log so we can audit routing without flooding (skip if it's the
        # PC Reps default — that's the bulk of historical traffic and noisy).
        if tenant["slug"] != "pc_reps":
            print(f"🏢 Tenant routed: To={to_number} → {tenant['slug']}")

    return None


@app.route("/voice/token")
def voice_token():
    # Require authentication
    if not current_user.is_authenticated:
        return jsonify({"error": "Not authenticated"}), 401

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    api_key = os.getenv("TWILIO_API_KEY")
    api_secret = os.getenv("TWILIO_API_SECRET")
    app_sid = os.getenv("TWIML_APP_SID")

    if not all([account_sid, api_key, api_secret, app_sid]):
        return "Missing Twilio environment variables", 500

    # Use employee_id as unique Twilio identity
    identity = current_user.employee_id

    voice_grant = VoiceGrant(outgoing_application_sid=app_sid, incoming_allow=True)
    token = AccessToken(account_sid, api_key, api_secret, identity=identity)
    token.add_grant(voice_grant)

    print(f"🎫 Generated Twilio token for identity: {identity}")
    return jsonify({"token": str(token.to_jwt())})


@app.route("/call/flow", methods=["POST"])
@validate_twilio_request
def call_flow():
    response = VoiceResponse()
    to_number = request.values.get("To")
    caller_id = os.getenv("TWILIO_CALLER_ID", "+17754602190")

    if not to_number:
        return "Missing 'To' number", 400

    # Dial with recording enabled
    # The 'url' parameter plays TwiML to the callee when they answer (before connecting)
    dial = response.dial(
        caller_id=caller_id,
        record="record-from-answer-dual",
        recording_status_callback="https://softphone.pc-reps.com/recording/call-complete",
        recording_status_callback_method="POST",
        recording_status_callback_event="completed",
    )
    dial.number(to_number, url="https://softphone.pc-reps.com/outbound-notice")
    return Response(str(response), mimetype="application/xml")


@app.route("/outbound-notice", methods=["POST"])
@validate_twilio_request
def outbound_notice():
    """Plays recording notice to the callee when they answer an outbound call"""
    response = VoiceResponse()
    response.play("https://softphone.pc-reps.com/static/recording_notice.mp3")
    return Response(str(response), mimetype="application/xml")


@app.route("/api/call/transfer", methods=["POST"])
def transfer_call():
    """Transfer an active call to another agent.

    Accepts: { call_sid, target_identity, is_incoming }
    - call_sid: The CallSid from the browser's connection
    - target_identity: The employee_id of the target agent (Twilio client identity)
    - is_incoming: Whether this is an inbound call being transferred
    """
    from twilio.rest import Client as TwilioClient

    if not current_user.is_authenticated:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing request body"}), 400

    call_sid = data.get("call_sid")
    target_identity = data.get("target_identity")
    is_incoming = data.get("is_incoming", True)

    if not call_sid or not target_identity:
        return jsonify({"error": "call_sid and target_identity are required"}), 400

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    base_url = os.getenv("BASE_URL", "https://softphone.pc-reps.com")

    if not all([account_sid, auth_token]):
        return jsonify({"error": "Missing Twilio credentials"}), 500

    try:
        client = TwilioClient(account_sid, auth_token)
        transfer_url = f"{base_url}/call/transfer-twiml?target={target_identity}"

        if is_incoming:
            # INCOMING CALL: Browser has the child call SID.
            # Look up the parent call SID (the external caller's leg) via Twilio API.
            child_call = client.calls(call_sid).fetch()
            parent_call_sid = child_call.parent_call_sid

            if not parent_call_sid:
                return jsonify({"error": "Could not find parent call for transfer"}), 400

            # Update the parent call with new TwiML that dials the target agent.
            # This disconnects the current agent and rings the target agent.
            client.calls(parent_call_sid).update(url=transfer_url, method="POST")

            print(f"🔄 Transfer (incoming): child={call_sid} → parent={parent_call_sid} → target={target_identity}")

        else:
            # OUTBOUND CALL: Browser has the parent call SID.
            # Find the child call (the external party's leg).
            child_calls = client.calls.list(parent_call_sid=call_sid, status="in-progress", limit=1)

            if not child_calls:
                return jsonify({"error": "Could not find active child call for transfer"}), 400

            child_call_sid = child_calls[0].sid

            # Update the child call with new TwiML that dials the target agent.
            # The parent (browser) call ends naturally when its <Dial> completes.
            client.calls(child_call_sid).update(url=transfer_url, method="POST")

            print(f"🔄 Transfer (outbound): parent={call_sid} → child={child_call_sid} → target={target_identity}")

        return jsonify({"status": "transferred", "target": target_identity}), 200

    except Exception as e:
        print(f"❌ Transfer failed: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Transfer failed: {str(e)}"}), 500


@app.route("/call/transfer-twiml", methods=["POST", "GET"])
def transfer_twiml():
    """Generate TwiML to connect a call to the target agent.
    Called by Twilio when a call is being transferred."""
    target = request.values.get("target")
    caller_id = os.getenv("TWILIO_CALLER_ID", "+17754602190")

    if not target:
        response = VoiceResponse()
        response.say("Transfer failed. No target specified.")
        response.hangup()
        return Response(str(response), mimetype="application/xml")

    response = VoiceResponse()
    response.say("Please hold while we transfer your call.", voice="Polly.Joanna")

    dial = response.dial(
        caller_id=caller_id,
        timeout=30,
        action=f"/call/transfer-status?target={target}",
        method="POST",
    )
    dial.client(target)

    return Response(str(response), mimetype="application/xml")


@app.route("/call/transfer-status", methods=["POST"])
def transfer_status():
    """Handle the result of a transfer dial attempt.
    If the target agent didn't answer, send to voicemail."""
    dial_status = request.values.get("DialCallStatus", "")
    target = request.values.get("target", "unknown")

    print(f"📞 Transfer dial status: {dial_status} (target: {target})")

    response = VoiceResponse()

    if dial_status in ("completed", "answered"):
        # Transfer was successful — call ends naturally when either party hangs up
        response.hangup()
    else:
        # Target didn't answer — let the caller know
        response.say("The person you are being transferred to is not available. Please leave a message after the beep.", voice="Polly.Joanna")
        response.play("https://softphone.pc-reps.com/beep.mp3")
        response.record(
            max_length=60,
            timeout=0,
            transcribe=True,
            transcribe_callback="https://softphone.pc-reps.com/voicemail/save",
            recording_status_callback="https://softphone.pc-reps.com/recording/complete",
            play_beep=False,
        )

    return Response(str(response), mimetype="application/xml")


@app.route("/api/calls")
def get_calls():
    """Get call history from database with pagination and filtering"""
    try:
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 100, type=int)
        direction = request.args.get("direction", None)

        # Cap per_page to prevent abuse
        per_page = min(per_page, 500)
        offset = (page - 1) * per_page

        conn = get_db_connection()
        cur = conn.cursor()

        # Build query with mandatory tenant filter + optional direction filter
        where_clause = "WHERE tenant_id = ?"
        params = [current_tenant_id()]
        if direction in ("inbound", "outbound"):
            where_clause += " AND direction = ?"
            params.append(direction)

        # Get total count for pagination info
        cur.execute(f"SELECT COUNT(*) as total FROM call_log {where_clause}", params)
        total = cur.fetchone()["total"]

        # Get paginated results
        cur.execute(
            f"SELECT * FROM call_log {where_clause} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params + [per_page, offset]
        )
        calls = [dict(row) for row in cur.fetchall()]
        conn.close()

        return jsonify({
            "calls": calls,
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": (total + per_page - 1) // per_page
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/calls/unseen-count")
def get_unseen_call_count():
    """Get count of unseen missed/inbound calls for notification badge"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) as count FROM call_log
            WHERE seen = false
            AND direction = 'inbound'
            AND status != 'completed'
            AND tenant_id = ?
        """, (current_tenant_id(),))
        result = cur.fetchone()
        conn.close()
        return jsonify({"count": result["count"] if result else 0})
    except Exception as e:
        # If 'seen' column doesn't exist yet, return 0 gracefully
        print(f"⚠️ Error getting unseen call count (column may not exist yet): {e}")
        return jsonify({"count": 0})


@app.route("/api/calls/mark-seen", methods=["POST"])
def mark_calls_seen():
    """Mark all unseen inbound calls as seen (when user visits call history)"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE call_log SET seen = true "
            "WHERE seen = false AND direction = 'inbound' AND tenant_id = ?",
            (current_tenant_id(),),
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"})
    except Exception as e:
        print(f"⚠️ Error marking calls as seen: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/calls/log", methods=["POST"])
def log_outbound_call():
    """Log outbound call to database"""
    print("🔥 /api/calls/log endpoint hit!")

    try:
        data = request.get_json()
        print(f"📥 Received data: {data}")

        phone_number = data.get("phone_number")
        status = data.get("status")
        call_sid = data.get("call_sid")

        print(f"📞 Parsed: phone={phone_number}, status={status}, sid={call_sid}")

        # Normalize phone number
        from phone_utils import normalize_phone_number

        normalized_phone = normalize_phone_number(phone_number)
        print(f"📱 Normalized phone: {normalized_phone}")

        if not normalized_phone:
            print("❌ Invalid phone number")
            return jsonify({"error": "Invalid phone number"}), 400

        # Get contact name from NovaCore
        from novacore_contacts import get_contact_name as nc_get_name
        contact_name = nc_get_name(normalized_phone)

        conn = get_db_connection()  # still needed for call_log insert below

        print(f"👤 Contact name: {contact_name}")
        print("✅ About to insert into database")

        # Log the call - update if exists, insert if new
        cur = conn.cursor()

        # Check if call already exists
        if call_sid:
            cur.execute(
                "SELECT id FROM call_log WHERE twilio_call_sid = ?", (call_sid,)
            )
            existing = cur.fetchone()

            if existing:
                # Update existing call — do NOT overwrite original timestamp
                cur.execute(
                    """
                        UPDATE call_log
                        SET status = ?
                        WHERE twilio_call_sid = ?
                    """,
                    (status, call_sid),
                )
                print(f"✅ Updated existing call record (SID: {call_sid})")
            else:
                # Insert new call — always store UTC timestamps
                cur.execute(
                    """
                        INSERT INTO call_log (
                            tenant_id, phone_number, direction, status, call_type,
                            caller_name, twilio_call_sid, timestamp, user_id
                        ) VALUES (?, ?, 'outbound', ?, 'voice', ?, ?, ?, ?)
                    """,
                    (
                        current_tenant_id(),
                        normalized_phone,
                        status,
                        contact_name,
                        call_sid,
                        datetime.now(timezone.utc).isoformat(),
                        current_user.id if current_user.is_authenticated else None,
                    ),
                )
                print(f"✅ Inserted new call record (SID: {call_sid})")
        else:
            # No call_sid, just insert (shouldn't happen but fallback)
            cur.execute(
                """
                INSERT INTO call_log (
                    tenant_id, phone_number, direction, status, call_type,
                    caller_name, twilio_call_sid, timestamp, user_id
                ) VALUES (?, ?, 'outbound', ?, 'voice', ?, ?, ?, ?)
            """,
                (
                    current_tenant_id(),
                    normalized_phone,
                    status,
                    contact_name,
                    "",
                    datetime.now(timezone.utc).isoformat(),
                    current_user.id if current_user.is_authenticated else None,
                ),
            )
            print("✅ Inserted new call record (no SID)")

        conn.commit()
        conn.close()

        # Notify NovaCore to log this outbound call on the customer's ticket
        try:
            from messaging import notify_novacore_ticket
            notify_novacore_ticket(normalized_phone, "outbound", "call", staff_user_id=current_user.id if current_user.is_authenticated else None)
        except Exception as e2:
            print(f"⚠️ Error notifying NovaCore of outbound call: {e2}")

        return jsonify({"status": "logged"})
    except Exception as e:
        print(f"❌ Error in log_outbound_call: {e}")
        import traceback

        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/voicemails")
def get_voicemails():
    return jsonify({"message": "Voicemail API not yet connected to database"}), 200


@app.route("/media-proxy")
def media_proxy():
    url = request.args.get("url")

    if not url:
        return "Missing URL", 400

    # SEC-3: Only allow proxying Twilio media URLs — prevents SSRF attacks
    from urllib.parse import urlparse
    parsed = urlparse(url)
    allowed_hosts = [
        "api.twilio.com",
        "media.twiliocdn.com",
        "s3-external-1.amazonaws.com",  # Twilio stores MMS media here
    ]
    if not parsed.hostname or not any(parsed.hostname.endswith(host) for host in allowed_hosts):
        print(f"🛑 Blocked proxy request to non-Twilio URL: {url}")
        return "Forbidden: only Twilio media URLs allowed", 403

    auth = (os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
    try:
        response = requests.get(url, auth=auth, timeout=10)
        response.raise_for_status()
        return Response(response.content, mimetype=response.headers.get("Content-Type", "application/octet-stream"))
    except Exception as e:
        print(f"❌ Proxy error for {url} — {e}")
        return "Error fetching media", 500


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    # SEC-6: Reject path traversal attempts (double-dot, backslash)
    if ".." in filename or "\\" in filename:
        return "Forbidden", 403
    return send_from_directory("static/uploads", filename)


# React app routing
# Suspicious file extensions and paths that bots probe for
BOT_PROBE_EXTENSIONS = (
    ".php",
    ".asp",
    ".aspx",
    ".jsp",
    ".cgi",
    ".pl",
    ".env",
    ".git",
    ".svn",
    ".bak",
    ".sql",
    ".db",
    ".log",
    ".ini",
    ".conf",
    ".yml",
    ".yaml",
    ".xml",
    ".json.bak",
)
BOT_PROBE_PATHS = (
    "wp-",
    "wordpress",
    "xmlrpc",
    "admin.php",
    "login.php",
    "shell",
    "eval-stdin",
    "phpinfo",
    "phpmyadmin",
    "adminer",
    "connector.sds",
    "geoserver",
    "/.env",
    "/.git",
    "/.svn",
)

ERROR_PAGES_DIR = os.path.join(os.path.dirname(__file__), "error_pages")


def is_bot_probe(path):
    """Check if a request path looks like a bot/scanner probe"""
    path_lower = path.lower()
    if any(path_lower.endswith(ext) for ext in BOT_PROBE_EXTENSIONS):
        return True
    if any(probe in path_lower for probe in BOT_PROBE_PATHS):
        return True
    return False


ERROR_PAGES_DIR = os.path.join(os.path.dirname(__file__), "error_pages")


@app.route("/error_pages/<path:filename>")
def serve_error_assets(filename):
    return send_from_directory(ERROR_PAGES_DIR, filename)


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_react_app(path):
    # If it looks like a bot probe, serve the fun 404 page
    if path and is_bot_probe(path):
        ip = request.headers.get(
            "X-Real-IP", request.headers.get("X-Forwarded-For", request.remote_addr)
        )
        print(f"🤡 Bot probe blocked with 404 page: /{path} from IP {ip}")
        return send_from_directory(ERROR_PAGES_DIR, "404.html"), 404

    full_path = os.path.join(app.static_folder, path)
    if path != "" and os.path.exists(full_path):
        return send_from_directory(app.static_folder, path)
    return send_from_directory(app.static_folder, "index.html")


@app.errorhandler(404)
def not_found(e):
    return send_from_directory(ERROR_PAGES_DIR, "404.html"), 404


# Dictionary to track online users: {user_id: {'sid': socket_id, 'employee_id': 'PCR-001', 'name': 'Matt', 'last_heartbeat': timestamp}}
online_users = {}


@socketio.on("connect")
def handle_connect():
    print(f"🔌 Client connected: {request.sid}")
    print(
        f"🔢 Total connected clients: {len(socketio.server.manager.rooms.get('/', {}).keys()) if hasattr(socketio.server, 'manager') else 'unknown'}"
    )
    return True


@socketio.on("user_login")
def handle_user_login(data):
    """
    Called when a user logs in and establishes WebSocket connection.
    Frontend sends: {user_id, employee_id, name}
    """
    try:
        user_id = data.get("user_id")
        employee_id = data.get("employee_id")
        name = data.get("name")

        if not user_id:
            print("❌ user_login event missing user_id")
            return

        # Add/update user in online tracking
        online_users[user_id] = {
            "sid": request.sid,
            "employee_id": employee_id,
            "name": name,
            "last_heartbeat": datetime.now(),
        }

        # Update last_activity in database (users table is in NovaCore PostgreSQL)
        # This is handled by NovaCore, skip here

        print(f"✅ User {name} ({employee_id}) logged in - Socket ID: {request.sid}")
        print(f"👥 Online users: {len(online_users)}")

        # Broadcast updated online user list to all clients
        socketio.emit(
            "users_online_update",
            {
                "online_users": [
                    {
                        "user_id": uid,
                        "employee_id": info["employee_id"],
                        "name": info["name"],
                    }
                    for uid, info in online_users.items()
                ]
            },
        )

    except Exception as e:
        print(f"❌ Error in user_login: {e}")


@socketio.on("user_heartbeat")
def handle_user_heartbeat(data):
    """
    Periodic heartbeat from frontend to confirm user is still online.
    Frontend sends: {user_id}
    """
    try:
        user_id = data.get("user_id")

        if user_id and user_id in online_users:
            online_users[user_id]["last_heartbeat"] = datetime.now()

            # Update last_activity in database (users table is in NovaCore PostgreSQL)
            # This is handled by NovaCore, skip here

    except Exception as e:
        print(f"❌ Error in user_heartbeat: {e}")


@socketio.on("disconnect")
def handle_disconnect():
    """Handle client disconnection - remove user from online tracking"""
    try:
        print(f"🔌 Client disconnected: {request.sid}")

        # Find and remove user by socket ID
        user_to_remove = None
        for user_id, info in online_users.items():
            if info["sid"] == request.sid:
                user_to_remove = user_id
                print(f"👋 User {info['name']} ({info['employee_id']}) went offline")
                break

        if user_to_remove:
            del online_users[user_to_remove]

            # Broadcast updated online user list to all clients
            socketio.emit(
                "users_online_update",
                {
                    "online_users": [
                        {
                            "user_id": uid,
                            "employee_id": info["employee_id"],
                            "name": info["name"],
                        }
                        for uid, info in online_users.items()
                    ]
                },
            )

        print(
            f"🔢 Total connected clients: {len(socketio.server.manager.rooms.get('/', {}).keys()) if hasattr(socketio.server, 'manager') else 'unknown'}"
        )
        print(f"👥 Online users: {len(online_users)}")

    except Exception as e:
        print(f"❌ Error in disconnect handler: {e}")
        import traceback

        traceback.print_exc()


# Development/testing routes
@app.route("/test-websocket")
def test_websocket():
    socketio.emit(
        "new_message",
        {
            "phone_number": "+1234567890",
            "message": "TEST MESSAGE",
            "timestamp": datetime.utcnow().isoformat(),
        },
    )
    return "Test message sent to all clients"


@app.route("/test-real-sms")
def test_real_sms():
    print(f"🧪 Testing real SMS format")
    socketio.emit(
        "new_message",
        {
            "phone_number": "+16193163652",
            "message": "TEST REAL SMS FORMAT",
            "timestamp": datetime.utcnow().isoformat(),
        },
    )
    return "Real SMS format test sent"


# Settings and Greetings API Routes
@app.route("/api/greetings")
def get_greetings():
    """Get all greetings"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM greetings WHERE tenant_id = ? ORDER BY type, name",
            (current_tenant_id(),),
        )
        greetings = [dict(row) for row in cur.fetchall()]
        conn.close()
        return jsonify(greetings)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/greetings/set-active", methods=["POST"])
def set_active_greeting():
    """Set the active greeting"""
    try:
        data = request.get_json()
        greeting_id = data.get("greeting_id")

        if not greeting_id:
            return jsonify({"error": "Missing greeting_id"}), 400

        conn = get_db_connection()
        cur = conn.cursor()

        # Get greeting info for logging
        tid = current_tenant_id()
        cur.execute(
            "SELECT name, type FROM greetings WHERE id = ? AND tenant_id = ?",
            (greeting_id, tid),
        )
        greeting = cur.fetchone()

        # Set all greetings to inactive (within this tenant only)
        cur.execute("UPDATE greetings SET is_active = 0 WHERE tenant_id = ?", (tid,))
        # Set the selected greeting to active
        cur.execute(
            "UPDATE greetings SET is_active = 1 WHERE id = ? AND tenant_id = ?",
            (greeting_id, tid),
        )
        conn.commit()

        # Log the activation
        if greeting:
            log_analytics_event(greeting["type"], greeting["name"], "activated")

        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/greetings/<int:greeting_id>", methods=["PUT"])
def update_greeting(greeting_id):
    """Update a greeting's message"""
    try:
        data = request.get_json()
        auto_sms_message = data.get("auto_sms_message")

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE greetings
            SET auto_sms_message = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND tenant_id = ?
        """,
            (auto_sms_message, greeting_id, current_tenant_id()),
        )
        conn.commit()
        conn.close()

        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/greetings/<int:greeting_id>/audio", methods=["POST"])
def upload_greeting_audio(greeting_id):
    """Upload audio file for a greeting with backup of existing file"""
    try:
        if "audio" not in request.files:
            return jsonify({"error": "No audio file provided"}), 400

        audio_file = request.files["audio"]
        if audio_file.filename == "":
            return jsonify({"error": "No file selected"}), 400

        # Get greeting info
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT name, type FROM greetings WHERE id = ? AND tenant_id = ?",
            (greeting_id, current_tenant_id()),
        )
        greeting = cur.fetchone()

        if not greeting:
            return jsonify({"error": "Greeting not found"}), 404

        # Create the correct directory (where your current files are)
        upload_dir = os.path.dirname(os.path.abspath(__file__))

        # Generate the expected filename
        import re

        safe_type = re.sub(r"[^\w\-_]", "_", greeting["type"])
        expected_filename = f"new_{safe_type}.mp3"
        expected_filepath = os.path.join(upload_dir, expected_filename)

        # Create backup of existing file if it exists
        backup_created = False
        if os.path.exists(expected_filepath):
            from datetime import datetime

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_filename = f"new_{safe_type}_backup_{timestamp}.mp3"
            backup_filepath = os.path.join(upload_dir, backup_filename)

            try:
                import shutil

                shutil.copy2(expected_filepath, backup_filepath)
                backup_created = True
                print(f"📦 Created backup: {backup_filename}")
            except Exception as e:
                print(f"⚠️ Failed to create backup: {e}")

        # Save uploaded file temporarily, then convert to Twilio-compatible format
        import subprocess

        temp_input = os.path.join(upload_dir, f"temp_upload_{greeting_id}.tmp")
        audio_file.save(temp_input)

        # Convert to 8000Hz mono (Twilio telephone standard) using ffmpeg
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",  # Overwrite output
                    "-i",
                    temp_input,  # Input file
                    "-ar",
                    "8000",  # Sample rate: 8kHz (telephone standard)
                    "-ac",
                    "1",  # Mono audio
                    "-b:a",
                    "32k",  # Bitrate (good for voice)
                    expected_filepath,  # Output file
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                print(f"⚠️ ffmpeg error: {result.stderr}")
                # Fallback: save original if conversion fails
                import shutil

                shutil.move(temp_input, expected_filepath)
                print("⚠️ Saved original file (conversion failed)")
            else:
                print(f"✅ Converted to 8000Hz mono: {expected_filename}")
                os.remove(temp_input)  # Clean up temp file

        except subprocess.TimeoutExpired:
            print("⚠️ ffmpeg timeout - saving original")
            import shutil

            shutil.move(temp_input, expected_filepath)
        except FileNotFoundError:
            print("⚠️ ffmpeg not found - saving original")
            import shutil

            shutil.move(temp_input, expected_filepath)

        # Update database with the standard URL
        audio_url = f"https://softphone.pc-reps.com/{expected_filename}"

        cur.execute(
            """
            UPDATE greetings
            SET audio_url = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND tenant_id = ?
        """,
            (audio_url, greeting_id, current_tenant_id()),
        )
        conn.commit()
        conn.close()

        response_message = f"Audio saved as {expected_filename}"
        if backup_created:
            response_message += f" (previous version backed up)"

        return jsonify(
            {
                "status": "success",
                "audio_url": audio_url,
                "filename": expected_filename,
                "backup_created": backup_created,
                "message": response_message,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/greetings/<int:greeting_id>/audio-library", methods=["GET"])
def get_audio_library(greeting_id):
    """Get list of available audio files for a greeting type"""
    try:
        # Get greeting type
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT type FROM greetings WHERE id = ? AND tenant_id = ?",
            (greeting_id, current_tenant_id()),
        )
        greeting = cur.fetchone()
        conn.close()

        if not greeting:
            return jsonify({"error": "Greeting not found"}), 404

        greeting_type = greeting["type"]
        upload_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

        # Find all MP3 files matching this greeting type
        import glob

        pattern = os.path.join(upload_dir, f"new_{greeting_type}*.mp3")
        files = glob.glob(pattern)

        audio_files = []
        for filepath in files:
            filename = os.path.basename(filepath)
            stat = os.stat(filepath)
            audio_files.append(
                {
                    "filename": filename,
                    "url": f"https://softphone.pc-reps.com/{filename}",
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )

        # Sort by modified date, newest first
        audio_files.sort(key=lambda x: x["modified"], reverse=True)

        return jsonify({"files": audio_files, "greeting_type": greeting_type})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/greetings/<int:greeting_id>/record", methods=["POST"])
def save_recorded_audio(greeting_id):
    """Save recorded audio from browser (webm) and convert to MP3"""
    try:
        if "audio" not in request.files:
            return jsonify({"error": "No audio file provided"}), 400

        audio_file = request.files["audio"]

        # Get greeting info
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT name, type FROM greetings WHERE id = ? AND tenant_id = ?",
            (greeting_id, current_tenant_id()),
        )
        greeting = cur.fetchone()
        conn.close()

        if not greeting:
            return jsonify({"error": "Greeting not found"}), 404

        upload_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

        # Generate filenames
        import re

        safe_type = re.sub(r"[^\w\-_]", "_", greeting["type"])
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Save webm temporarily
        temp_webm = os.path.join(upload_dir, f"temp_{safe_type}_{timestamp}.webm")
        final_mp3 = os.path.join(upload_dir, f"new_{safe_type}_{timestamp}.mp3")

        audio_file.save(temp_webm)

        # Convert to MP3 using ffmpeg
        import subprocess

        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-i",
                    temp_webm,
                    "-acodec",
                    "libmp3lame",
                    "-ab",
                    "128k",
                    "-ar",
                    "44100",
                    "-y",
                    final_mp3,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                raise Exception(f"FFmpeg error: {result.stderr}")

        finally:
            # Clean up temp file
            if os.path.exists(temp_webm):
                os.remove(temp_webm)

        final_filename = os.path.basename(final_mp3)
        audio_url = f"https://softphone.pc-reps.com/static/{final_filename}"

        return jsonify(
            {
                "status": "success",
                "audio_url": audio_url,
                "filename": final_filename,
                "message": f"Recording saved as {final_filename}",
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/greetings/<int:greeting_id>/select-audio", methods=["POST"])
def select_greeting_audio(greeting_id):
    """Select an existing audio file for a greeting"""
    try:
        data = request.get_json()
        filename = data.get("filename")

        if not filename:
            return jsonify({"error": "No filename provided"}), 400

        # Verify file exists
        upload_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
        filepath = os.path.join(upload_dir, filename)

        if not os.path.exists(filepath):
            return jsonify({"error": "File not found"}), 404

        # Update database
        audio_url = f"https://softphone.pc-reps.com/static/{filename}"

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE greetings
            SET audio_url = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND tenant_id = ?
        """,
            (audio_url, greeting_id, current_tenant_id()),
        )
        conn.commit()
        conn.close()

        return jsonify(
            {
                "status": "success",
                "audio_url": audio_url,
                "message": f"Now using {filename}",
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings/vacation-dates", methods=["GET", "POST"])
def vacation_dates():
    """Get or set vacation dates"""
    try:
        if request.method == "GET":
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT setting_key, setting_value FROM app_settings
                WHERE setting_key IN ('vacation_start_date', 'vacation_end_date')
                  AND tenant_id = ?
            """,
                (current_tenant_id(),),
            )
            rows = cur.fetchall()
            conn.close()

            dates = {}
            for row in rows:
                if "start" in row["setting_key"]:
                    dates["start_date"] = row["setting_value"]
                elif "end" in row["setting_key"]:
                    dates["end_date"] = row["setting_value"]

            return jsonify(dates)

        elif request.method == "POST":
            data = request.get_json()
            start_date = data.get("start_date")
            end_date = data.get("end_date")

            conn = get_db_connection()
            cur = conn.cursor()

            # Update or insert vacation dates. Conflict target is the
            # composite (tenant_id, setting_key) constraint introduced
            # by migrate_tenants.py.
            tid = current_tenant_id()
            cur.execute(
                """
                INSERT INTO app_settings (tenant_id, setting_key, setting_value, updated_at)
                VALUES (?, 'vacation_start_date', ?, CURRENT_TIMESTAMP)
                ON CONFLICT (tenant_id, setting_key)
                  DO UPDATE SET setting_value = ?, updated_at = CURRENT_TIMESTAMP
            """,
                (tid, start_date, start_date),
            )

            cur.execute(
                """
                INSERT INTO app_settings (tenant_id, setting_key, setting_value, updated_at)
                VALUES (?, 'vacation_end_date', ?, CURRENT_TIMESTAMP)
                ON CONFLICT (tenant_id, setting_key)
                  DO UPDATE SET setting_value = ?, updated_at = CURRENT_TIMESTAMP
            """,
                (tid, end_date, end_date),
            )

            conn.commit()
            conn.close()

            return jsonify({"status": "success"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/static/greetings/<path:filename>")
def serve_greeting_audio(filename):
    return send_from_directory("static/greetings", filename)


@app.route("/static/<path:filename>")
def serve_static_audio(filename):
    return send_from_directory("static", filename)


@app.route("/recordings/calls/<filename>")
def serve_call_recording(filename):
    """Serve call recording files"""
    recordings_dir = os.path.join(os.path.dirname(__file__), "recordings", "calls")
    return send_from_directory(recordings_dir, filename)


@app.route("/api/settings/recording-notice", methods=["GET"])
def get_recording_notice():
    """Get info about the current recording notice audio"""
    try:
        upload_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
        filepath = os.path.join(upload_dir, "recording_notice.mp3")

        if os.path.exists(filepath):
            stat = os.stat(filepath)
            return jsonify(
                {
                    "exists": True,
                    "url": "https://softphone.pc-reps.com/static/recording_notice.mp3",
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )
        else:
            return jsonify({"exists": False, "url": None})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings/recording-notice", methods=["POST"])
def upload_recording_notice():
    """Upload recording notice audio file"""
    try:
        if "audio" not in request.files:
            return jsonify({"error": "No audio file provided"}), 400

        audio_file = request.files["audio"]
        if audio_file.filename == "":
            return jsonify({"error": "No file selected"}), 400

        upload_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
        filepath = os.path.join(upload_dir, "recording_notice.mp3")

        # Backup existing file if it exists
        backup_created = False
        if os.path.exists(filepath):
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(
                upload_dir, f"recording_notice_backup_{timestamp}.mp3"
            )
            try:
                import shutil

                shutil.copy2(filepath, backup_path)
                backup_created = True
                print(f"📦 Created backup: recording_notice_backup_{timestamp}.mp3")
            except Exception as e:
                print(f"⚠️ Failed to create backup: {e}")

        # Save uploaded file temporarily, then convert to Twilio-compatible format
        import subprocess

        temp_input = os.path.join(upload_dir, "temp_recording_notice.tmp")
        audio_file.save(temp_input)

        # Convert to 8000Hz mono (Twilio telephone standard) using ffmpeg
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    temp_input,
                    "-ar",
                    "8000",
                    "-ac",
                    "1",
                    "-b:a",
                    "32k",
                    filepath,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                print(f"⚠️ ffmpeg error: {result.stderr}")
                import shutil

                shutil.move(temp_input, filepath)
                print("⚠️ Saved original file (conversion failed)")
            else:
                print(f"✅ Converted recording notice to 8000Hz mono")
                os.remove(temp_input)

        except subprocess.TimeoutExpired:
            print("⚠️ ffmpeg timeout - saving original")
            import shutil

            shutil.move(temp_input, filepath)
        except FileNotFoundError:
            print("⚠️ ffmpeg not found - saving original")
            import shutil

            shutil.move(temp_input, filepath)

        return jsonify(
            {
                "status": "success",
                "url": "https://softphone.pc-reps.com/static/recording_notice.mp3",
                "backup_created": backup_created,
                "message": "Recording notice uploaded successfully",
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings/recording-notice/record", methods=["POST"])
def save_recorded_notice():
    """Save recorded audio from browser (webm) and convert to MP3 for recording notice"""
    try:
        if "audio" not in request.files:
            return jsonify({"error": "No audio file provided"}), 400

        audio_file = request.files["audio"]
        upload_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
        filepath = os.path.join(upload_dir, "recording_notice.mp3")

        # Backup existing file if it exists
        backup_created = False
        if os.path.exists(filepath):
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(
                upload_dir, f"recording_notice_backup_{timestamp}.mp3"
            )
            try:
                import shutil

                shutil.copy2(filepath, backup_path)
                backup_created = True
            except Exception as e:
                print(f"⚠️ Failed to create backup: {e}")

        # Save webm temporarily
        temp_webm = os.path.join(upload_dir, "temp_recording_notice.webm")
        audio_file.save(temp_webm)

        # Convert to MP3 using ffmpeg (8000Hz mono for Twilio)
        import subprocess

        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    temp_webm,
                    "-ar",
                    "8000",
                    "-ac",
                    "1",
                    "-b:a",
                    "32k",
                    filepath,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                raise Exception(f"FFmpeg error: {result.stderr}")

        finally:
            if os.path.exists(temp_webm):
                os.remove(temp_webm)

        return jsonify(
            {
                "status": "success",
                "url": "https://softphone.pc-reps.com/static/recording_notice.mp3",
                "backup_created": backup_created,
                "message": "Recording notice saved successfully",
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def check_vacation_auto_return():
    """Check if vacation period has ended and auto-return to auto mode.

    Runs in a daemon thread (no Flask request context). Iterates over
    every tenant individually since each tenant has its own greetings
    + app_settings rows post-Phase 1c migration.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT id, slug FROM tenants")
        all_tenants = cur.fetchall()

        for tenant in all_tenants:
            tid = tenant["id"]

            # Check if vacation greeting is currently active for this tenant
            cur.execute(
                "SELECT * FROM greetings WHERE is_active = 1 AND type = 'vacation' "
                "AND tenant_id = ? LIMIT 1",
                (tid,),
            )
            active_vacation = cur.fetchone()
            if not active_vacation:
                continue  # not in vacation mode for this tenant

            # Get vacation end date for this tenant
            cur.execute(
                "SELECT setting_value FROM app_settings "
                "WHERE setting_key = 'vacation_end_date' AND tenant_id = ?",
                (tid,),
            )
            end_date_row = cur.fetchone()
            if not end_date_row or not end_date_row["setting_value"]:
                continue  # no end date set for this tenant

            end_date = datetime.fromisoformat(end_date_row["setting_value"])
            now = datetime.now()

            if now.date() > end_date.date():
                # Vacation period has ended for this tenant — switch to auto mode
                cur.execute(
                    "UPDATE greetings SET is_active = 0 WHERE tenant_id = ?",
                    (tid,),
                )
                cur.execute(
                    "UPDATE greetings SET is_active = 1 WHERE type = 'auto' AND tenant_id = ?",
                    (tid,),
                )
                conn.commit()
                print(
                    f"🔄 Auto-return [{tenant['slug']}]: Vacation ended on {end_date.date()}, "
                    "switched back to Auto Mode"
                )

        conn.close()

    except Exception as e:
        print(f"⌚ Error in vacation auto-return check: {e}")
        return False


# Schedule vacation auto-return check (runs every hour)
import threading
import time


def vacation_check_loop():
    while True:
        try:
            check_vacation_auto_return()
            time.sleep(3600)  # Check every hour
        except Exception as e:
            print(f"⌚ Error in vacation check loop: {e}")
            time.sleep(3600)


# Start vacation check thread
vacation_thread = threading.Thread(target=vacation_check_loop, daemon=True)
vacation_thread.start()


@app.route("/api/greetings/preview")
def preview_greeting():
    """Preview what customers would experience without actually triggering it"""
    try:
        preview_type = request.args.get("type", "call")  # 'call' or 'text'

        # Get current active greeting
        conn = get_db_connection()
        cur = conn.cursor()
        tid = current_tenant_id()
        cur.execute(
            "SELECT * FROM greetings WHERE is_active = 1 AND tenant_id = ? LIMIT 1",
            (tid,),
        )
        active_greeting = cur.fetchone()

        if not active_greeting:
            return jsonify({"error": "No active greeting found"}), 404

        # Determine what would happen based on current settings
        from incoming import is_open_now

        preview_data = {
            "mode": active_greeting["name"],
            "type": active_greeting["type"],
        }

        if active_greeting["type"] == "auto":
            # Auto mode - depends on current time
            # Fetch templates from database
            default_open = "Hi, this is PC Reps 👋 For the fastest response, just reply to this text with your question. Hours: Tue, Thu, Sat 10–6. Walk-ins welcome. Reply STOP to opt out."
            default_closed = "Hi, this is PC Reps 👋 We're currently closed (open Tue, Thu, Sat 10–6). For the fastest response, text us your question and we'll get back to you when we open! Reply STOP to opt out."

            cur.execute(
                "SELECT setting_value FROM app_settings "
                "WHERE setting_key = 'auto_sms_open_message' AND tenant_id = ?",
                (tid,),
            )
            open_row = cur.fetchone()
            cur.execute(
                "SELECT setting_value FROM app_settings "
                "WHERE setting_key = 'auto_sms_closed_message' AND tenant_id = ?",
                (tid,),
            )
            closed_row = cur.fetchone()

            open_message = open_row["setting_value"] if open_row else default_open
            closed_message = (
                closed_row["setting_value"] if closed_row else default_closed
            )

            if is_open_now():
                # Get open greeting
                cur.execute(
                    "SELECT * FROM greetings WHERE type = 'open' AND tenant_id = ? LIMIT 1",
                    (tid,),
                )
                open_greeting = cur.fetchone()
                preview_data.update(
                    {
                        "status": "Currently OPEN (Auto Mode)",
                        "audio_url": (
                            open_greeting["audio_url"]
                            if open_greeting
                            else "new_open.mp3"
                        ),
                        "sms_message": open_message,
                    }
                )
            else:
                # Get closed greeting
                cur.execute(
                    "SELECT * FROM greetings WHERE type = 'closed' AND tenant_id = ? LIMIT 1",
                    (tid,),
                )
                closed_greeting = cur.fetchone()
                preview_data.update(
                    {
                        "status": "Currently CLOSED (Auto Mode)",
                        "audio_url": (
                            closed_greeting["audio_url"]
                            if closed_greeting
                            else "new_closed.mp3"
                        ),
                        "sms_message": closed_message,
                    }
                )
        else:
            # Manual mode - use specific greeting
            preview_data.update(
                {
                    "status": f"Manual Override: {active_greeting['name']}",
                    "audio_url": active_greeting["audio_url"] or "new_closed.mp3",
                    "sms_message": active_greeting["auto_sms_message"],
                }
            )

        # Add experience-specific details
        if preview_type == "call":
            priority_types = ["sick", "vacation", "holiday"]
            bypass_conversation = active_greeting["type"] in priority_types

            preview_data["call_flow"] = {
                "step1": f"Caller hears: {preview_data['audio_url']}",
                "step2": f"After 45 seconds: SMS sent {'(bypasses conversation check)' if bypass_conversation else '(respects conversation history)'}",
                "step3": "24-hour cooldown begins",
            }
        elif preview_type == "text":
            priority_types = ["sick", "vacation", "holiday"]
            if active_greeting["type"] in priority_types:
                preview_data["text_flow"] = {
                    "step1": "Customer texts arrive normally",
                    "step2": f"Immediate auto-reply: {preview_data['sms_message']}",
                    "step3": "24-hour cooldown for status replies",
                }
            else:
                preview_data["text_flow"] = {
                    "step1": "Customer texts arrive normally",
                    "step2": "No auto-reply (only priority statuses send text auto-replies)",
                    "step3": "Normal conversation flow",
                }

        conn.close()
        return jsonify(preview_data)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/analytics")
def get_analytics():
    """Get usage analytics for the specified time period"""
    try:
        days = int(request.args.get("days", 7))
        cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()
        print(f"📊 Analytics request: {days} days, cutoff: {cutoff_date}")

        conn = get_db_connection()
        cur = conn.cursor()

        # Check if tables exist (PostgreSQL version)
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
        tables = [row["table_name"] for row in cur.fetchall()]
        print(f"📊 Available tables: {tables}")

        tid = current_tenant_id()

        # Get call statistics
        try:
            cur.execute(
                """
                SELECT status, COUNT(*) as count
                FROM call_log
                WHERE timestamp > ? AND tenant_id = ?
                GROUP BY status
            """,
                (cutoff_date, tid),
            )
            calls_data = dict(cur.fetchall())
            total_calls = sum(calls_data.values())
            print(f"📊 Calls data: {calls_data}")
        except Exception as e:
            print(f"📊 Call stats error: {e}")
            calls_data = {}
            total_calls = 0

        # Get auto-SMS statistics
        try:
            cur.execute(
                """
                SELECT COUNT(*) as count
                FROM messages
                WHERE direction = 'outbound'
                AND is_auto_sms = 1
                AND timestamp > ?
                AND tenant_id = ?
            """,
                (cutoff_date, tid),
            )
            total_auto_sms = cur.fetchone()["count"]
            print(f"📊 Auto SMS count: {total_auto_sms}")
        except Exception as e:
            print(f"📊 Auto SMS stats error: {e}")
            total_auto_sms = 0

        # Get SMS breakdown by greeting type
        try:
            cur.execute(
                """
                SELECT 
                    CASE 
                        WHEN body LIKE '%sick%' THEN 'sick'
                        WHEN body LIKE '%vacation%' THEN 'vacation'
                        WHEN body LIKE '%holiday%' THEN 'holiday'
                        WHEN body LIKE '%closed%' THEN 'closed'
                        WHEN body LIKE '%open%' OR body LIKE '%Hours: Tue%' THEN 'open'
                        ELSE 'other'
                    END as type,
                    COUNT(*) as count
                FROM messages
                WHERE direction = 'outbound'
                AND is_auto_sms = 1
                AND timestamp > ?
                AND tenant_id = ?
                GROUP BY type
            """,
                (cutoff_date, tid),
            )
            sms_data = dict(cur.fetchall())
            print(f"📊 SMS breakdown: {sms_data}")
        except Exception as e:
            print(f"📊 SMS breakdown error: {e}")
            sms_data = {}

        # Get greeting activations from analytics table (if it exists)
        activation_data = {}
        total_activations = 0
        recent_activations = []

        if "greeting_analytics" in tables:
            try:
                cur.execute(
                    """
                    SELECT greeting_type, COUNT(*) as count
                    FROM greeting_analytics
                    WHERE event_type = 'activated'
                    AND timestamp > ?
                    AND tenant_id = ?
                    GROUP BY greeting_type
                """,
                    (cutoff_date, tid),
                )
                activation_data = dict(cur.fetchall())
                total_activations = sum(activation_data.values())

                cur.execute(
                    """
                    SELECT greeting_name, timestamp
                    FROM greeting_analytics
                    WHERE event_type = 'activated'
                    AND timestamp > ?
                    AND tenant_id = ?
                    ORDER BY timestamp DESC
                    LIMIT 5
                """,
                    (cutoff_date, tid),
                )
                recent_activations = [dict(row) for row in cur.fetchall()]
                print(
                    f"📊 Activations: {activation_data}, Recent: {recent_activations}"
                )
            except Exception as e:
                print(f"📊 Analytics table error: {e}")
        else:
            print("📊 greeting_analytics table doesn't exist")

        result = {
            "total_calls": total_calls,
            "calls_by_type": calls_data,
            "total_auto_sms": total_auto_sms,
            "sms_by_type": sms_data,
            "total_activations": total_activations,
            "activation_by_type": activation_data,
            "recent_activations": recent_activations,
        }
        print(f"📊 Returning: {result}")
        conn.close()
        return jsonify(result)

    except Exception as e:
        print(f"📊 Analytics error: {e}")
        import traceback

        traceback.print_exc()
        return jsonify({"error": str(e)}), 500



# log_analytics_event moved to analytics.py to avoid circular imports
from analytics import log_analytics_event


@app.route("/api/novacore-lookup/<phone_number>")
def novacore_lookup(phone_number):
    """Look up NovaCore customer/ticket URL for a phone number.

    Returns the URL to the most recent open ticket if the customer has one,
    otherwise the customer profile URL. Returns 404 if no customer is found.
    """
    try:
        from urllib.parse import quote

        from novacore_contacts import find_customer_by_phone, get_novacore_connection

        public_url = os.getenv(
            "NOVACORE_PUBLIC_URL", "https://novacore.pc-reps.com"
        ).rstrip("/")

        customer = find_customer_by_phone(phone_number)
        if not customer:
            url = f"{public_url}/customers/new?phone={quote(phone_number)}"
            print(f"🆕 No customer found — opening NovaCore new-customer page with phone pre-filled")
            return jsonify({"url": url, "type": "new_customer"})

        customer_id = customer.get("id")
        if not customer_id:
            return jsonify({"error": "NovaCore customer has no id"}), 500

        conn = get_novacore_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM tickets
                    WHERE customer_id = %s
                      AND status NOT IN ('Resolved', 'Closed', 'Invoiced')
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (customer_id,),
                )
                row = cur.fetchone()
        finally:
            conn.close()

        if row:
            ticket_id = row["id"]
            url = f"{public_url}/tickets/{ticket_id}"
            print(f"🎫 Opening NovaCore ticket {ticket_id}")
            return jsonify({"url": url, "type": "ticket", "ticket_id": ticket_id})

        url = f"{public_url}/customer/{customer_id}"
        print(f"👤 Opening NovaCore customer {customer_id}")
        return jsonify({"url": url, "type": "customer", "customer_id": customer_id})

    except Exception as e:
        print(f"❌ NovaCore lookup error: {e}")
        return jsonify({"error": "NovaCore lookup failed"}), 500


@app.route("/api/greetings/add-dnd", methods=["GET", "POST"])
def add_dnd_greeting():
    """Add Do Not Disturb greeting if it doesn't exist"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Check if DND greeting already exists for this tenant
        tid = current_tenant_id()
        cursor.execute(
            "SELECT id FROM greetings WHERE type = 'dnd' AND tenant_id = ? LIMIT 1",
            (tid,),
        )
        existing = cursor.fetchone()

        if existing:
            conn.close()
            return jsonify(
                {"message": "DND greeting already exists", "id": existing["id"]}
            )

        # Add DND greeting for this tenant
        cursor.execute(
            """
            INSERT INTO greetings (tenant_id, type, name, auto_sms_message, audio_url, is_active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            RETURNING id
        """,
            (
                tid,
                "dnd",
                "Do Not Disturb",
                "Smart DND mode - uses open/closed SMS based on hours",
                "https://softphone.pc-reps.com/new_dnd.mp3",
                0,
            ),
        )

        dnd_id = cursor.fetchone()["id"]
        conn.commit()
        conn.close()

        return jsonify({"message": "DND greeting added", "id": dnd_id})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


## REMOVED: /api/call-history was a duplicate of /api/calls
## All call history now served by GET /api/calls with pagination


@app.route("/api/settings/dnd", methods=["GET", "POST"])
def dnd_setting():
    """Get or set DND status"""
    try:
        if request.method == "GET":
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT setting_value FROM app_settings "
                "WHERE setting_key = 'dnd_enabled' AND tenant_id = ?",
                (current_tenant_id(),),
            )
            row = cur.fetchone()
            conn.close()

            dnd_enabled = bool(int(row["setting_value"])) if row else False
            return jsonify({"dnd_enabled": dnd_enabled})

        elif request.method == "POST":
            data = request.get_json()
            dnd_enabled = data.get("dnd_enabled", False)

            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO app_settings (tenant_id, setting_key, setting_value, updated_at)
                VALUES (?, 'dnd_enabled', ?, CURRENT_TIMESTAMP)
                ON CONFLICT (tenant_id, setting_key)
                  DO UPDATE SET setting_value = ?, updated_at = CURRENT_TIMESTAMP
            """,
                (current_tenant_id(), str(int(dnd_enabled)), str(int(dnd_enabled))),
            )
            conn.commit()
            conn.close()

            return jsonify({"status": "success", "dnd_enabled": dnd_enabled})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings/auto-sms-templates", methods=["GET", "POST"])
def auto_sms_templates():
    """Get or update auto-SMS message templates for open/closed hours"""
    try:
        if request.method == "GET":
            conn = get_db_connection()
            cur = conn.cursor()

            tid = current_tenant_id()

            # Get open hours template
            cur.execute(
                "SELECT setting_value FROM app_settings "
                "WHERE setting_key = 'auto_sms_open_message' AND tenant_id = ?",
                (tid,),
            )
            open_row = cur.fetchone()

            # Get closed hours template
            cur.execute(
                "SELECT setting_value FROM app_settings "
                "WHERE setting_key = 'auto_sms_closed_message' AND tenant_id = ?",
                (tid,),
            )
            closed_row = cur.fetchone()
            conn.close()

            # Default messages if not set
            default_open = "Hi, this is PC Reps 👋 For the fastest response, just reply to this text with your question. Hours: Tue, Thu, Sat 10–6. Walk-ins welcome. Reply STOP to opt out."
            default_closed = "Hi, this is PC Reps 👋 We're currently closed (open Tue, Thu, Sat 10–6). For the fastest response, text us your question and we'll get back to you when we open! Reply STOP to opt out."

            return jsonify(
                {
                    "open_message": (
                        open_row["setting_value"] if open_row else default_open
                    ),
                    "closed_message": (
                        closed_row["setting_value"] if closed_row else default_closed
                    ),
                }
            )

        elif request.method == "POST":
            data = request.get_json()
            open_message = data.get("open_message")
            closed_message = data.get("closed_message")

            conn = get_db_connection()
            cur = conn.cursor()

            tid = current_tenant_id()

            if open_message is not None:
                cur.execute(
                    """
                    INSERT INTO app_settings (tenant_id, setting_key, setting_value, updated_at)
                    VALUES (?, 'auto_sms_open_message', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT (tenant_id, setting_key)
                      DO UPDATE SET setting_value = ?, updated_at = CURRENT_TIMESTAMP
                """,
                    (tid, open_message, open_message),
                )

            if closed_message is not None:
                cur.execute(
                    """
                    INSERT INTO app_settings (tenant_id, setting_key, setting_value, updated_at)
                    VALUES (?, 'auto_sms_closed_message', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT (tenant_id, setting_key)
                      DO UPDATE SET setting_value = ?, updated_at = CURRENT_TIMESTAMP
                """,
                    (tid, closed_message, closed_message),
                )

            conn.commit()
            conn.close()

            return jsonify({"status": "success"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/templates", methods=["GET", "POST"])
def message_templates_collection():
    """Shared message templates: list all, or create a new one."""
    try:
        if request.method == "GET":
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT id, name, content, category FROM message_templates "
                "WHERE tenant_id = ? ORDER BY category, name",
                (current_tenant_id(),),
            )
            rows = cur.fetchall()
            conn.close()
            return jsonify([dict(r) for r in rows])

        data = request.get_json() or {}
        name = (data.get("name") or "").strip()
        content = (data.get("content") or "").strip()
        category = (data.get("category") or "General").strip() or "General"

        if not name or not content:
            return jsonify({"error": "name and content are required"}), 400

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO message_templates (tenant_id, name, content, category)
            VALUES (?, ?, ?, ?)
            RETURNING id, name, content, category
            """,
            (current_tenant_id(), name, content, category),
        )
        row = cur.fetchone()
        conn.commit()
        conn.close()
        return jsonify(dict(row)), 201

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/templates/<int:template_id>", methods=["PUT", "DELETE"])
def message_templates_item(template_id):
    """Shared message templates: update or delete a specific template."""
    try:
        if request.method == "DELETE":
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM message_templates WHERE id = ? AND tenant_id = ?",
                (template_id, current_tenant_id()),
            )
            deleted = cur.rowcount
            conn.commit()
            conn.close()
            if deleted == 0:
                return jsonify({"error": "not found"}), 404
            return jsonify({"status": "success"})

        data = request.get_json() or {}
        name = (data.get("name") or "").strip()
        content = (data.get("content") or "").strip()
        category = (data.get("category") or "General").strip() or "General"

        if not name or not content:
            return jsonify({"error": "name and content are required"}), 400

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE message_templates
               SET name = ?, content = ?, category = ?, updated_at = NOW()
             WHERE id = ? AND tenant_id = ?
         RETURNING id, name, content, category
            """,
            (name, content, category, template_id, current_tenant_id()),
        )
        row = cur.fetchone()
        conn.commit()
        conn.close()
        if not row:
            return jsonify({"error": "not found"}), 404
        return jsonify(dict(row))

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/greetings/remove-dnd", methods=["DELETE"])
def remove_old_dnd_greeting():
    """Remove the old DND greeting since we're using toggle now"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM greetings WHERE type = 'dnd' AND tenant_id = ?",
            (current_tenant_id(),),
        )
        deleted_count = cursor.rowcount
        conn.commit()
        conn.close()

        return jsonify(
            {
                "message": f"Removed {deleted_count} old DND greeting(s)",
                "deleted_count": deleted_count,
            }
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/contacts/lookup/<phone_number>")
def lookup_contact(phone_number):
    """Look up contact name by phone number"""
    try:
        from phone_utils import normalize_phone_number

        normalized_phone = normalize_phone_number(phone_number)
        if not normalized_phone:
            return jsonify({"error": "Invalid phone number"}), 400

        from novacore_contacts import find_customer_by_phone
        customer = find_customer_by_phone(normalized_phone)

        if customer:
            name = customer.get("name") or ""
            if customer.get("company"):
                name += f" ({customer['company']})"
            return jsonify({"name": name})
        else:
            return jsonify({"name": None})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings/debug", methods=["GET", "POST"])
def debug_setting():
    """Get or set debug mode for testing"""
    try:
        if request.method == "GET":
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT setting_value FROM app_settings "
                "WHERE setting_key = 'debug_mode_enabled' AND tenant_id = ?",
                (current_tenant_id(),),
            )
            row = cur.fetchone()
            conn.close()

            debug_enabled = bool(int(row["setting_value"])) if row else False
            return jsonify({"debug_enabled": debug_enabled})

        elif request.method == "POST":
            data = request.get_json()
            debug_enabled = data.get("enabled", False)

            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO app_settings (tenant_id, setting_key, setting_value, updated_at)
                VALUES (?, 'debug_mode_enabled', ?, CURRENT_TIMESTAMP)
                ON CONFLICT (tenant_id, setting_key)
                  DO UPDATE SET setting_value = ?, updated_at = CURRENT_TIMESTAMP
            """,
                (current_tenant_id(), str(int(debug_enabled)), str(int(debug_enabled))),
            )
            conn.commit()
            conn.close()

            return jsonify({"status": "success", "debug_enabled": debug_enabled})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings/default-operator", methods=["GET"])
def get_default_operator():
    """Get the current default operator user ID"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT setting_value FROM app_settings "
            "WHERE setting_key = 'default_operator_user_id' AND tenant_id = ?",
            (current_tenant_id(),),
        )
        row = cursor.fetchone()
        conn.close()

        if row:
            return jsonify({"user_id": int(row["setting_value"])})
        else:
            return jsonify({"user_id": None})
    except Exception as e:
        print(f"Error getting default operator: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings/default-operator", methods=["POST"])
def set_default_operator():
    """Set the default operator user ID"""
    try:
        data = request.get_json()
        user_id = data.get("user_id")

        if not user_id:
            return jsonify({"error": "Missing user_id"}), 400

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO app_settings (tenant_id, setting_key, setting_value, updated_at)
            VALUES (?, 'default_operator_user_id', ?, CURRENT_TIMESTAMP)
            ON CONFLICT (tenant_id, setting_key)
              DO UPDATE SET setting_value = ?, updated_at = CURRENT_TIMESTAMP
        """,
            (current_tenant_id(), str(user_id), str(user_id)),
        )
        conn.commit()
        conn.close()

        return jsonify({"status": "success", "user_id": user_id})
    except Exception as e:
        print(f"Error setting default operator: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings/business-hours", methods=["GET", "POST"])
def business_hours():
    """Get or set business hours"""
    try:
        if request.method == "GET":
            conn = get_db_connection()
            cur = conn.cursor()

            # Get all business hour settings
            cur.execute(
                """
                SELECT setting_key, setting_value FROM app_settings
                WHERE setting_key IN ('business_open_time', 'business_close_time', 'business_days')
                  AND tenant_id = ?
            """,
                (current_tenant_id(),),
            )
            rows = cur.fetchall()
            conn.close()

            settings = {
                "open_time": "10:00",  # Default: 10am
                "close_time": "18:00",  # Default: 6pm
                "days": "2,4,6",  # Default: Tue, Thu, Sat
            }

            for row in rows:
                if row["setting_key"] == "business_open_time":
                    settings["open_time"] = row["setting_value"]
                elif row["setting_key"] == "business_close_time":
                    settings["close_time"] = row["setting_value"]
                elif row["setting_key"] == "business_days":
                    settings["days"] = row["setting_value"]

            return jsonify(settings)

        elif request.method == "POST":
            data = request.get_json()

            conn = get_db_connection()
            cur = conn.cursor()

            tid = current_tenant_id()

            if "open_time" in data:
                cur.execute(
                    """
                    INSERT INTO app_settings (tenant_id, setting_key, setting_value, updated_at)
                    VALUES (?, 'business_open_time', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT (tenant_id, setting_key)
                      DO UPDATE SET setting_value = ?, updated_at = CURRENT_TIMESTAMP
                """,
                    (tid, data["open_time"], data["open_time"]),
                )

            if "close_time" in data:
                cur.execute(
                    """
                    INSERT INTO app_settings (tenant_id, setting_key, setting_value, updated_at)
                    VALUES (?, 'business_close_time', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT (tenant_id, setting_key)
                      DO UPDATE SET setting_value = ?, updated_at = CURRENT_TIMESTAMP
                """,
                    (tid, data["close_time"], data["close_time"]),
                )

            if "days" in data:
                cur.execute(
                    """
                    INSERT INTO app_settings (tenant_id, setting_key, setting_value, updated_at)
                    VALUES (?, 'business_days', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT (tenant_id, setting_key)
                      DO UPDATE SET setting_value = ?, updated_at = CURRENT_TIMESTAMP
                """,
                    (tid, data["days"], data["days"]),
                )

            conn.commit()
            conn.close()

            return jsonify({"status": "success"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def run_startup_migrations():
    """Run safe migrations on startup — adds missing columns/indexes if needed"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Add 'seen' column to call_log if it doesn't exist
        cur.execute("""
            ALTER TABLE call_log ADD COLUMN IF NOT EXISTS seen BOOLEAN DEFAULT false
        """)

        # Performance indexes for messages table (used by /messages/threads)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_phone_ts ON messages (phone_number, timestamp DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_unread ON messages (phone_number, direction, read) WHERE direction = 'inbound' AND read = 0")

        conn.commit()
        conn.close()
        print("✅ Startup migrations complete")
    except Exception as e:
        print(f"⚠️ Startup migration note: {e}")


if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG") == "1"
    print("=" * 60)
    print("🚀 PC Reps Softphone Server Starting...")
    print("=" * 60)
    print(f"🐘 Database: PostgreSQL (softphone)")
    print(f"🌐 Server URL: http://0.0.0.0:10000")
    print(f"🔌 WebSocket: Enabled")
    print(f"🐞 Debug mode: {'ON (DEV ONLY)' if debug_mode else 'OFF'}")
    print("=" * 60)
    run_startup_migrations()
    # allow_unsafe_werkzeug=True is required by flask-socketio 5.x when debug is
    # off, since the Werkzeug dev server is not a hardened production server.
    # Full migration to a proper WSGI server happens in SaaS Phase 2 (dockerize).
    socketio.run(
        app,
        host="0.0.0.0",
        port=10000,
        debug=debug_mode,
        allow_unsafe_werkzeug=True,
    )
