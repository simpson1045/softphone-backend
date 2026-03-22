from flask import Blueprint, request, Response
from twilio.twiml.voice_response import VoiceResponse
from datetime import datetime, timedelta, timezone as tz_utc
from pytz import timezone
import psycopg2
from psycopg2.extras import RealDictCursor
import os
import threading
import time
import re
from messaging import log_message
from database import get_db_connection
from phone_utils import normalize_phone_number, get_contact_name

incoming_bp = Blueprint("incoming", __name__)
pacific = timezone("US/Pacific")


def get_novacore_connection():
    """Get a connection to the NovaCore PostgreSQL database"""
    password = os.getenv("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError("POSTGRES_PASSWORD environment variable is not set")
    return psycopg2.connect(
        host="localhost",
        port=5432,
        database="novacore",
        user="postgres",
        password=password,
        cursor_factory=RealDictCursor,
    )



# normalize_phone_number and get_contact_name imported from phone_utils


def should_suppress_auto_sms(phone_number):
    """Check if auto-SMS should be suppressed for this contact (reads from sms_preferences table)"""
    try:
        normalized_phone = normalize_phone_number(phone_number)
        if not normalized_phone:
            return False

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT suppress_auto_sms, opted_out_sms FROM sms_preferences WHERE phone_number = ?",
            (normalized_phone,),
        )
        row = cur.fetchone()
        conn.close()
        if row:
            return bool(row["suppress_auto_sms"]) or bool(row["opted_out_sms"])
        return False
    except Exception as e:
        print(f"❌ Error checking auto-SMS suppression for {phone_number}: {e}")
        return False


def is_in_auto_sms_cooldown(phone_number):
    """Check if phone number is in auto-SMS cooldown period (24 hours)"""
    try:
        normalized_phone = normalize_phone_number(phone_number)
        if not normalized_phone:
            return True

        # Check if we sent an auto-SMS in the last 24 hours
        cooldown_hours = 24
        cutoff_time = (datetime.now() - timedelta(hours=cooldown_hours)).isoformat()

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT timestamp FROM messages 
            WHERE phone_number = ? 
            AND direction = 'outbound' 
            AND is_auto_sms = 1
            AND timestamp > ?
            ORDER BY timestamp DESC LIMIT 1
        """,
            (normalized_phone, cutoff_time),
        )

        row = cur.fetchone()
        conn.close()

        if row:
            print(
                f"⏰ Auto-SMS cooldown active for {normalized_phone} (last sent: {row['timestamp']})"
            )
            return True

        return False
    except Exception as e:
        print(f"❌ Error checking cooldown for {phone_number}: {e}")
        return True


def has_recent_stop_message(phone_number):
    """Check if contact recently sent STOP message (for additional protection)"""
    try:
        normalized_phone = normalize_phone_number(phone_number)
        if not normalized_phone:
            return False

        # Calculate week_ago timestamp
        week_ago = (datetime.now() - timedelta(days=7)).isoformat()

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT body FROM messages 
            WHERE phone_number = ? 
            AND direction = 'inbound' 
            AND timestamp > ?
            ORDER BY timestamp DESC
        """,
            (normalized_phone, week_ago),
        )

        recent_messages = cur.fetchall()
        conn.close()

        for msg in recent_messages:
            if msg["body"] and msg["body"].strip().upper() in [
                "STOP",
                "UNSUBSCRIBE",
                "QUIT",
                "END",
                "CANCEL",
                "OPTOUT",
            ]:
                return True

        return False
    except Exception as e:
        print(f"❌ Error checking for STOP messages: {e}")
        return False


def log_auto_sms_attempt(
    phone_number, call_log_id, status, reason, message_id=None, cooldown_until=None
):
    """Log auto-SMS attempt in auto_sms_log table"""
    try:
        normalized_phone = normalize_phone_number(phone_number)

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO auto_sms_log (
                phone_number, call_log_id, message_id, sent_at, 
                cooldown_until, status, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
            (
                normalized_phone,
                call_log_id,
                message_id,
                datetime.now().isoformat(),
                cooldown_until.isoformat() if cooldown_until else None,
                status,
                reason,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ Error logging auto-SMS attempt: {e}")


def is_open_now(phone_number=None):
    """Check if business is currently open, with debug override for testing"""

    # Check if this is your debug number
    if phone_number:
        normalized_phone = normalize_phone_number(phone_number)
        if normalized_phone == "+16193163652":  # Your number
            # Check if debug mode is enabled
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute(
                    "SELECT setting_value FROM app_settings WHERE setting_key = 'debug_mode_enabled'"
                )
                row = cur.fetchone()
                conn.close()

                if row and bool(int(row["setting_value"])):
                    print(
                        f"🛠 DEBUG MODE: Overriding hours for {normalized_phone} - treating as OPEN"
                    )
                    return True  # Always treat your number as "open hours" when debug is on
            except Exception as e:
                print(f"Error checking debug mode: {e}")

    # Get business hours from database
    open_time = "10:00"  # Default
    close_time = "18:00"  # Default
    business_days = [2, 4, 6]  # Default: Tue, Thu, Sat

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT setting_key, setting_value FROM app_settings 
            WHERE setting_key IN ('business_open_time', 'business_close_time', 'business_days')
        """
        )
        rows = cur.fetchall()
        conn.close()

        for row in rows:
            if row["setting_key"] == "business_open_time":
                open_time = row["setting_value"]
            elif row["setting_key"] == "business_close_time":
                close_time = row["setting_value"]
            elif row["setting_key"] == "business_days":
                business_days = [int(d) for d in row["setting_value"].split(",")]
    except Exception as e:
        print(f"⚠️ Error reading business hours: {e}")

    now = datetime.now(pacific)

    # Parse times and compare
    open_hour, open_min = map(int, open_time.split(":"))
    close_hour, close_min = map(int, close_time.split(":"))

    current_minutes = now.hour * 60 + now.minute
    open_minutes = open_hour * 60 + open_min
    close_minutes = close_hour * 60 + close_min

    return (
        open_minutes <= current_minutes < close_minutes
        and now.isoweekday() in business_days
    )


def send_auto_sms(phone_number, call_log_id=None):
    """Send automatic SMS for missed call with different messages for open/closed hours"""
    from twilio.rest import Client

    try:
        normalized_phone = normalize_phone_number(phone_number)
        if not normalized_phone:
            print(f"❌ Invalid phone number for auto-SMS: {phone_number}")
            return False

        # Check suppression
        if should_suppress_auto_sms(normalized_phone):
            print(f"🚫 Auto-SMS suppressed for {normalized_phone}")
            return False

        # Check cooldown
        if is_in_auto_sms_cooldown(normalized_phone):
            print(f"⏰ Auto-SMS in cooldown for {normalized_phone}")
            return False

        # Check for recent STOP message
        if has_recent_stop_message(normalized_phone):
            print(f"🛑 Auto-SMS blocked - recent STOP message from {normalized_phone}")
            return False

        # NEW: Check for recent conversation activity (but allow certain greeting types through)
        active_greeting = get_active_greeting()
        bypass_conversation_check = False

        if active_greeting:
            # These greeting types should send SMS even with recent conversations
            priority_greeting_types = ["sick", "vacation", "holiday"]
            if active_greeting.get("type") in priority_greeting_types:
                bypass_conversation_check = True
                print(
                    f"🚨 Priority greeting ({active_greeting.get('type')}) - bypassing conversation check for {normalized_phone}"
                )

        if not bypass_conversation_check and has_recent_conversation(
            normalized_phone, hours=720
        ):  # 30 days = 720 hours
            print(
                f"💬 Auto-SMS skipped - recent conversation detected for {normalized_phone}"
            )
            return False

        # Send the SMS
        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        sms_from_number = os.getenv("TWILIO_SMS_NUMBER", "+17754602190")

        if not account_sid or not auth_token:
            print("❌ Missing Twilio credentials for auto-SMS")
            return False

        client = Client(account_sid, auth_token)

        # Get the auto-SMS message based on active greeting mode
        active_greeting = get_active_greeting()

        if active_greeting and active_greeting.get("type") == "auto":
            # AUTO MODE: Use open/closed logic with configurable templates
            default_open = "Hi, this is PC Reps 👋 For the fastest response, just reply to this text with your question. Hours: Tue, Thu, Sat 10–6. Walk-ins welcome. Reply STOP to opt out."
            default_closed = "Hi, this is PC Reps 👋 We're currently closed (open Tue, Thu, Sat 10–6). For the fastest response, text us your question and we'll get back to you when we open! Reply STOP to opt out."

            # Fetch templates from database
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT setting_value FROM app_settings WHERE setting_key = 'auto_sms_open_message'"
            )
            open_row = cur.fetchone()
            cur.execute(
                "SELECT setting_value FROM app_settings WHERE setting_key = 'auto_sms_closed_message'"
            )
            closed_row = cur.fetchone()
            conn.close()

            open_message = open_row["setting_value"] if open_row else default_open
            closed_message = (
                closed_row["setting_value"] if closed_row else default_closed
            )

            if is_open_now(normalized_phone):
                auto_message = open_message
            else:
                auto_message = closed_message
        elif active_greeting and active_greeting.get("auto_sms_message"):
            # MANUAL MODE: Use the specific greeting's message
            auto_message = active_greeting["auto_sms_message"]
        else:
            # Fallback if no greeting found
            auto_message = (
                "Hi, this is PC Reps 👋 We're currently unavailable. Please text us your question and we'll get back to you! "
                "Reply STOP to opt out."
            )

        try:
            message = client.messages.create(
                body=auto_message,
                from_=sms_from_number,
                to=normalized_phone,
                status_callback="https://softphone.pc-reps.com/messages/status",
            )

            print(f"✅ Auto-SMS sent to {normalized_phone}: {message.sid}")
            print(f"📱 Message: {auto_message}")

            # Log the auto-SMS to the database
            log_message(
                "outbound",
                normalized_phone,
                auto_message,
                [],
                message.sid,
                is_auto_sms=True,
            )

            # Log analytics event
            try:
                from analytics import log_analytics_event

                if active_greeting:
                    log_analytics_event(
                        active_greeting.get("type", "unknown"),
                        active_greeting.get("name", "Unknown"),
                        "auto_sms_sent",
                        normalized_phone,
                    )
            except Exception as e:
                print(f"Analytics logging failed: {e}")

            # NEW: Emit real-time notification for auto-SMS
            try:
                from flask import current_app
                from flask_socketio import SocketIO

                # Use the global socketio instance instead of current_app
                with current_app.app_context():
                    socketio = current_app.extensions.get("socketio")
                    if socketio:
                        socketio.emit(
                            "new_message",
                            {
                                "phone_number": normalized_phone,
                                "message": auto_message,
                                "timestamp": datetime.now(pacific).isoformat(),
                            },
                        )
                        print(
                            f"📡 Emitted WebSocket event for auto-SMS to {normalized_phone}"
                        )
            except Exception as e:
                print(f"❌ Error emitting WebSocket event for auto-SMS: {e}")

            return True

        except Exception as twilio_error:
            # Handle Twilio-specific errors (like landlines)
            error_str = str(twilio_error)
            if "21614" in error_str:
                print(f"📵 Cannot send SMS to landline: {normalized_phone}")
            elif "30006" in error_str:
                print(
                    f"📵 LANDLINE DETECTED: Cannot deliver SMS to {normalized_phone} - likely a landline or carrier that doesn't support SMS"
                )
            elif "30007" in error_str:
                print(
                    f"📵 CARRIER FILTERING: SMS to {normalized_phone} filtered by carrier - likely a landline"
                )
            elif "30008" in error_str:
                print(
                    f"📵 UNKNOWN DESTINATION: SMS to {normalized_phone} - invalid or unreachable number"
                )
            else:
                print(
                    f"❌ Twilio error sending auto-SMS to {normalized_phone}: {twilio_error}"
                )
            return False

    except Exception as e:
        print(f"❌ Error sending auto-SMS to {phone_number}: {e}")
        log_auto_sms_attempt(phone_number, call_log_id, "failed", str(e))
        return False


def delayed_auto_sms(phone_number, call_log_id, delay_seconds=45):
    """Send auto-SMS after a delay (UPDATED: 45 seconds for missed calls)"""

    def send_after_delay():
        print(
            f"⏰ Waiting {delay_seconds} seconds before sending auto-SMS to {phone_number}"
        )
        time.sleep(delay_seconds)
        send_auto_sms(phone_number, call_log_id)

    # Start background thread
    thread = threading.Thread(target=send_after_delay)
    thread.daemon = True
    thread.start()


def log_call_to_db(call_data, user_id=None):
    """Log call to master database with proper normalization"""
    try:
        normalized_phone = normalize_phone_number(call_data["from_number"])
        contact_name = get_contact_name(normalized_phone)

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO call_log (
                phone_number, direction, status, call_type, 
                caller_name, twilio_call_sid, timestamp, user_id
            ) VALUES (?, 'inbound', ?, 'voice', ?, ?, ?, ?)
            RETURNING id
        """,
            (
                normalized_phone,
                call_data["status"],
                contact_name,
                call_data.get("call_sid", ""),
                call_data["timestamp"],
                user_id,
            ),
        )

        result = cur.fetchone()
        call_log_id = result["id"] if result else None
        conn.commit()

        # Notify NovaCore to log this inbound call on the customer's ticket
        try:
            from messaging import notify_novacore_ticket
            notify_novacore_ticket(normalized_phone, "inbound", "call")
        except Exception as e:
            print(f"⚠️ Error notifying NovaCore of inbound call: {e}")

        # Update missed call notification count if this was a missed call
        if "missed" in call_data["status"].lower():
            try:
                from flask import current_app

                socketio = current_app.extensions.get("socketio")
                if socketio:
                    # Get current missed call count
                    cur.execute(
                        "SELECT COUNT(*) as cnt FROM call_log WHERE status LIKE '%missed%'"
                    )
                    row = cur.fetchone()
                    missed_count = row["cnt"] if row else 0

                    socketio.emit(
                        "call_notification_update", {"missed_count": missed_count}
                    )
                    print(f"📡 Emitted missed call notification: {missed_count}")
            except Exception as e:
                print(f"⚠️ Error emitting call notification: {e}")

        conn.close()
        return call_log_id
    except Exception as e:
        print(f"❌ Error logging call to DB: {e}")
        return None


def get_dnd_status():
    """Check if DND mode is enabled"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT setting_value FROM app_settings WHERE setting_key = 'dnd_enabled'"
        )
        row = cur.fetchone()
        conn.close()
        return bool(int(row["setting_value"])) if row else False
    except Exception as e:
        print(f"Error checking DND status: {e}")
        return False


def get_active_greeting():
    """Get the currently active greeting settings"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM greetings WHERE is_active = 1 LIMIT 1")
        row = cur.fetchone()
        if row:
            conn.close()
            return dict(row)
        else:
            # Fallback to closed greeting if no active greeting found
            cur.execute("SELECT * FROM greetings WHERE type = 'closed' LIMIT 1")
            row = cur.fetchone()
            conn.close()
            return dict(row) if row else None
    except Exception as e:
        print(f"⌚ Error getting active greeting: {e}")
        return None


def get_default_operator_identity():
    """Get the employee_id of the default operator for call routing"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Get default operator user_id from settings
        cur.execute(
            "SELECT setting_value FROM app_settings WHERE setting_key = 'default_operator_user_id'"
        )
        setting_row = cur.fetchone()
        conn.close()

        if not setting_row:
            print("⚠️ No default operator set, using fallback")
            return "browser-user"

        user_id = int(setting_row["setting_value"])

        # Get the employee_id from NovaCore database (PostgreSQL)
        nova_conn = get_novacore_connection()
        nova_cursor = nova_conn.cursor()
        nova_cursor.execute("SELECT employee_id FROM users WHERE id = %s", (user_id,))
        user_row = nova_cursor.fetchone()
        nova_conn.close()

        if user_row and user_row["employee_id"]:
            print(f"📞 Routing to default operator: {user_row['employee_id']}")
            return user_row["employee_id"]
        else:
            print(f"⚠️ User {user_id} has no employee_id, using fallback")
            return "browser-user"

    except Exception as e:
        print(f"❌ Error getting default operator: {e}")
        return "browser-user"


@incoming_bp.route("/incoming", methods=["POST"])
def incoming():
    from_number = request.values.get("From")
    to_number = request.values.get("To")
    call_sid = request.values.get("CallSid")
    timestamp = datetime.now(tz_utc.utc).isoformat()
    response = VoiceResponse()

    print(f"📞 Incoming call detected at {timestamp}")
    print(f"👤 From: {from_number}")
    print(f"📲 To: {to_number}")

    # Check DND setting first
    dnd_enabled = False
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT setting_value FROM app_settings WHERE setting_key = 'dnd_enabled'"
        )
        row = cur.fetchone()
        conn.close()
        dnd_enabled = bool(int(row["setting_value"])) if row else False
    except Exception as e:
        print(f"Error checking DND status: {e}")

    # Get active greeting
    active_greeting = get_active_greeting()

    if dnd_enabled:
        print("🔇 DND MODE ENABLED - All calls go to voicemail")

        # Use the active greeting's audio and SMS, but force to voicemail
        if active_greeting and active_greeting.get("type") == "auto":
            # Auto mode with DND - use open/closed logic for audio
            if is_open_now(from_number):
                print("🔇 DND + Auto Mode (Open Hours)")
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("SELECT * FROM greetings WHERE type = 'open' LIMIT 1")
                open_greeting = cur.fetchone()
                conn.close()

                greeting_audio_url = (
                    open_greeting["audio_url"]
                    if open_greeting
                    else "https://softphone.pc-reps.com/new_open.mp3"
                )
                status = "missed_call_dnd_auto_open"
            else:
                print("🔇 DND + Auto Mode (Closed Hours)")
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("SELECT * FROM greetings WHERE type = 'closed' LIMIT 1")
                closed_greeting = cur.fetchone()
                conn.close()

                greeting_audio_url = (
                    closed_greeting["audio_url"]
                    if closed_greeting
                    else "https://softphone.pc-reps.com/new_closed.mp3"
                )
                status = "missed_call_dnd_auto_closed"
        else:
            # Manual mode with DND - use the specific greeting
            greeting_audio_url = (
                active_greeting.get(
                    "audio_url", "https://softphone.pc-reps.com/new_closed.mp3"
                )
                if active_greeting
                else "https://softphone.pc-reps.com/new_closed.mp3"
            )
            greeting_name = (
                active_greeting.get("name", "Unknown") if active_greeting else "Unknown"
            )
            status = (
                f"missed_call_dnd_{active_greeting.get('type', 'unknown')}"
                if active_greeting
                else "missed_call_dnd"
            )
            print(f"🔇 DND + {greeting_name}")

        # Play greeting and go to voicemail (no dial attempt)
        response.play(greeting_audio_url)
        response.play("https://softphone.pc-reps.com/beep.mp3")
        response.record(
            max_length=60,
            timeout=0,
            transcribe=True,
            transcribe_callback="https://softphone.pc-reps.com/voicemail/save",
            recording_status_callback="https://softphone.pc-reps.com/recording/complete",
            play_beep=False,
        )

        # Log the call
        call_log_id = log_call_to_db(
            {
                "call_sid": call_sid,
                "from_number": from_number or "Unknown",
                "to_number": to_number or "Unknown",
                "call_type": "inbound",
                "status": status,
                "timestamp": timestamp,
            }
        )

        # Schedule auto-SMS (will use active greeting's logic)
        if from_number and call_log_id:
            print(f"📅 Scheduling DND auto-SMS for {from_number}")
            delayed_auto_sms(from_number, call_log_id, delay_seconds=45)

        print(f"🔇 DND: Call sent to voicemail")
        return Response(str(response), mimetype="application/xml")

    # EXISTING LOGIC WHEN DND IS OFF - keep your current auto/manual mode logic exactly as is
    elif active_greeting and active_greeting.get("type") == "auto":
        # AUTO MODE: Use old open/closed logic
        print("🔄 AUTO MODE: Using automatic open/closed switching")

        if is_open_now(from_number):
            print("🟢 We are OPEN (Auto Mode) - Routing call to softphone")

            # Play recording notice before connecting
            response.play("https://softphone.pc-reps.com/static/recording_notice.mp3")

            # ROUTE CALL TO SOFTPHONE (with recording)
            dial = response.dial(
                timeout=25,
                action="/incoming/dial-status",
                method="POST",
                status_callback="/incoming/call-status",
                status_callback_event="initiated ringing completed",
                status_callback_method="POST",
                record="record-from-answer-dual",
                recording_status_callback="https://softphone.pc-reps.com/recording/call-complete",
                recording_status_callback_method="POST",
                recording_status_callback_event="completed",
            )
            operator_identity = get_default_operator_identity()
            dial.client(operator_identity)

            # Log as ringing, not missed
            call_log_id = log_call_to_db(
                {
                    "call_sid": call_sid,
                    "from_number": from_number or "Unknown",
                    "to_number": to_number or "Unknown",
                    "call_type": "inbound",
                    "status": "ringing",
                    "timestamp": timestamp,
                }
            )

            greeting_name = "AUTO MODE - OPEN (Call Routed)"

        else:
            print("🔴 We are CLOSED (Auto Mode) - Going to voicemail")
            # Use the "closed" greeting settings
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT * FROM greetings WHERE type = 'closed' LIMIT 1")
            closed_greeting = cur.fetchone()
            conn.close()

            greeting_audio_url = (
                closed_greeting["audio_url"]
                if closed_greeting
                else "https://softphone.pc-reps.com/new_closed.mp3"
            )

            # Play greeting and go to voicemail
            response.play(greeting_audio_url)
            response.play("https://softphone.pc-reps.com/beep.mp3")
            response.record(
                max_length=60,
                timeout=0,
                transcribe=True,
                transcribe_callback="/voicemail/save",
                recording_status_callback="/recording/complete",
                play_beep=False,
            )

            # Log as missed and schedule auto-SMS
            call_log_id = log_call_to_db(
                {
                    "call_sid": call_sid,
                    "from_number": from_number or "Unknown",
                    "to_number": to_number or "Unknown",
                    "call_type": "inbound",
                    "status": "missed_call_closed_hours",
                    "timestamp": timestamp,
                }
            )

            if from_number and call_log_id:
                print(
                    f"📅 Scheduling auto-SMS for {from_number} in 45 seconds (CLOSED)"
                )
                delayed_auto_sms(from_number, call_log_id, delay_seconds=45)

            greeting_name = "AUTO MODE - CLOSED"

    elif active_greeting and active_greeting.get("type") == "open":
        # OPEN OVERRIDE: Force calls to ring regardless of business hours
        print("🟢 OPEN OVERRIDE: Forcing calls to ring (ignoring business hours)")

        # Play recording notice before connecting
        response.play("https://softphone.pc-reps.com/static/recording_notice.mp3")

        dial = response.dial(
            timeout=25,
            action="/incoming/dial-status",
            method="POST",
            status_callback="/incoming/call-status",
            status_callback_event="initiated ringing completed",
            status_callback_method="POST",
            record="record-from-answer-dual",
            recording_status_callback="https://softphone.pc-reps.com/recording/call-complete",
            recording_status_callback_method="POST",
            recording_status_callback_event="completed",
        )
        operator_identity = get_default_operator_identity()
        dial.client(operator_identity)

        call_log_id = log_call_to_db(
            {
                "call_sid": call_sid,
                "from_number": from_number or "Unknown",
                "to_number": to_number or "Unknown",
                "call_type": "inbound",
                "status": "ringing",
                "timestamp": timestamp,
            }
        )

        greeting_name = "OPEN OVERRIDE (Call Routed)"

    else:
        # CLOSED/MANUAL MODE: Use the specifically selected greeting (goes to voicemail)
        greeting_audio_url = (
            "https://softphone.pc-reps.com/new_closed.mp3"  # Default fallback
        )
        greeting_name = "Unknown"
        status = "missed_call_manual_mode"

        if active_greeting:
            greeting_audio_url = active_greeting.get("audio_url", greeting_audio_url)
            greeting_name = active_greeting.get("name", "Custom Greeting")
            status = f"missed_call_{active_greeting.get('type', 'unknown')}"
            print(f"🔴 CLOSED/MANUAL MODE: Using {greeting_name} - Going to voicemail")
        else:
            print("⚠️ No active greeting found, using fallback")

        # Play greeting and go to voicemail for manual modes
        response.play(greeting_audio_url)
        response.play("https://softphone.pc-reps.com/beep.mp3")
        response.record(
            max_length=60,
            timeout=0,
            transcribe=True,
            transcribe_callback="/voicemail/save",
            recording_status_callback="/recording/complete",
            play_beep=False,
        )

        # Log the call and schedule auto-SMS
        call_log_id = log_call_to_db(
            {
                "call_sid": call_sid,
                "from_number": from_number or "Unknown",
                "to_number": to_number or "Unknown",
                "call_type": "inbound",
                "status": status,
                "timestamp": timestamp,
            }
        )

        if from_number and call_log_id:
            print(
                f"📅 Scheduling auto-SMS for {from_number} in 45 seconds ({greeting_name})"
            )
            delayed_auto_sms(from_number, call_log_id, delay_seconds=45)

    print(f"🎵 Call handling complete")
    return Response(str(response), mimetype="application/xml")


@incoming_bp.route("/incoming/dial-status", methods=["POST"])
def dial_status():
    print("📞 /dial-status callback triggered ✅")
    for key, value in request.form.items():
        print(f"📋 {key}: {value}")

    from_number = request.form.get("From", "")
    to_number = request.form.get("To", "")
    status = request.form.get("DialCallStatus", "no-answer")
    call_sid = request.form.get("CallSid", "")

    # Update call status in master database — do NOT overwrite original timestamp
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # If call was answered, get the operator's user_id
        user_id = None
        if status == "completed":
            cur.execute(
                "SELECT setting_value FROM app_settings WHERE setting_key = 'default_operator_user_id'"
            )
            setting_row = cur.fetchone()
            if setting_row:
                user_id = int(setting_row["setting_value"])

        cur.execute(
            """
            UPDATE call_log
            SET status = ?, user_id = COALESCE(?, user_id)
            WHERE twilio_call_sid = ?
        """,
            (status, user_id, call_sid),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Error updating call status: {e}")

    response = VoiceResponse()

    if status != "completed":
        print("📬 Call not answered — redirecting to voicemail")

        # Get active greeting to determine which voicemail greeting to play
        active_greeting = get_active_greeting()

        if active_greeting and active_greeting.get("type") == "auto":
            # Use open greeting for missed calls during open hours
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT * FROM greetings WHERE type = 'open' LIMIT 1")
            open_greeting = cur.fetchone()
            conn.close()

            greeting_url = (
                open_greeting["audio_url"]
                if open_greeting
                else "https://softphone.pc-reps.com/new_open.mp3"
            )
        else:
            # Use the active greeting for manual modes
            greeting_url = (
                active_greeting.get(
                    "audio_url", "https://softphone.pc-reps.com/new_closed.mp3"
                )
                if active_greeting
                else "https://softphone.pc-reps.com/new_closed.mp3"
            )

        # Play voicemail greeting and record
        response.play(greeting_url)
        response.play("https://softphone.pc-reps.com/beep.mp3")
        response.record(
            max_length=60,
            timeout=0,
            transcribe=True,
            transcribe_callback="https://softphone.pc-reps.com/voicemail/save",
            recording_status_callback="https://softphone.pc-reps.com/recording/complete",
            play_beep=False,
        )

        # Schedule auto-SMS for missed call
        if from_number:
            print(f"📅 Scheduling auto-SMS for missed call: {from_number}")
            # Get the call_log_id for SMS tracking
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute(
                    "SELECT id FROM call_log WHERE twilio_call_sid = ?", (call_sid,)
                )
                result = cur.fetchone()
                call_log_id = result["id"] if result else None
                conn.close()

                if call_log_id:
                    delayed_auto_sms(from_number, call_log_id, delay_seconds=45)
            except Exception as e:
                print(f"⚠️ Error getting call_log_id for SMS: {e}")
                # Send SMS anyway without call_log_id
                delayed_auto_sms(from_number, None, delay_seconds=45)
    else:
        print("✅ Call completed — no voicemail needed")

    return Response(str(response), mimetype="application/xml")


@incoming_bp.route("/incoming/call-status", methods=["POST"])
def call_status():
    call_sid = request.values.get("CallSid")
    from_number = request.values.get("From", "")
    to_number = request.values.get("To", "")
    event = request.values.get("CallStatus")  # initiated, ringing, completed
    timestamp = datetime.now(tz_utc.utc).isoformat()

    print(f"📡 Call Status Callback: {event} — {from_number} → {to_number}")

    if event == "completed":
        # Use master database with normalized phone numbers
        normalized_phone = normalize_phone_number(from_number)
        if normalized_phone:
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT 1 FROM call_log
                    WHERE phone_number = ? AND twilio_call_sid = ?
                """,
                    (normalized_phone, call_sid),
                )

                if not cur.fetchone():
                    contact_name = get_contact_name(normalized_phone)
                    cur.execute(
                        """
                        INSERT INTO call_log (
                            phone_number, direction, status, call_type,
                            caller_name, twilio_call_sid, timestamp
                        ) VALUES (?, 'inbound', 'completed', 'voice', ?, ?, ?)
                    """,
                        (normalized_phone, contact_name, call_sid, timestamp),
                    )
                    conn.commit()
                conn.close()
            except Exception as e:
                print(f"❌ Error in call status callback: {e}")

    return ("", 204)


def has_recent_conversation(phone_number, hours=720):
    """Check if there has been recent message activity (both directions) within specified hours (default: 30 days)"""
    try:
        normalized_phone = normalize_phone_number(phone_number)
        if not normalized_phone:
            return False

        # Calculate cutoff time
        cutoff_time = (datetime.now() - timedelta(hours=hours)).isoformat()

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) as cnt FROM messages 
            WHERE phone_number = ? 
            AND timestamp > ?
            AND is_auto_sms = 0
        """,
            (normalized_phone, cutoff_time),
        )

        row = cur.fetchone()
        message_count = row["cnt"] if row else 0
        conn.close()

        if message_count > 0:
            print(
                f"📱 Recent conversation detected for {normalized_phone} ({message_count} messages in last {hours//24} days)"
            )
            return True

        return False
    except Exception as e:
        print(f"❌ Error checking recent conversation: {e}")
        return False
