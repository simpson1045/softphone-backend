from io import BytesIO
from PIL import Image
from flask import Blueprint, request, jsonify, Response
from twilio.rest import Client
import os
from datetime import datetime
import requests
import re
import json
from flask_socketio import emit
from pytz import timezone
from flask_login import current_user
import psycopg2
from psycopg2.extras import RealDictCursor
from database import get_db_connection
from phone_utils import normalize_phone_number, get_contact_name

pacific = timezone("US/Pacific")
messaging_bp = Blueprint("messaging", __name__)

from dotenv import load_dotenv

load_dotenv()


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


def notify_novacore_ticket(phone_number, direction, comm_type, body="", staff_user_id=None):
    """
    Fire-and-forget notification to NovaCore about an SMS/call event.
    NovaCore will look up the customer, find their open ticket, and log it.

    Args:
        phone_number: The customer's phone number
        direction: "inbound" or "outbound"
        comm_type: "sms" or "call"
        body: Message text (empty for calls)
        staff_user_id: The staff user ID for outbound messages
    """
    try:
        api_key = os.getenv("EXTERNAL_SMS_API_KEY", "")
        if not api_key:
            print("[NOVACORE-NOTIFY] No API key configured, skipping", flush=True)
            return

        payload = {
            "phone_number": phone_number,
            "direction": direction,
            "comm_type": comm_type,
            "body": body or "",
        }
        if staff_user_id:
            payload["staff_user_id"] = staff_user_id

        resp = requests.post(
            "http://localhost:5000/api/ticket-comms/event",
            json=payload,
            headers={"Content-Type": "application/json", "X-API-Key": api_key},
            timeout=5,
        )
        result = resp.json() if resp.status_code == 200 else {}
        action = result.get("action", "unknown")
        print(f"[NOVACORE-NOTIFY] {direction} {comm_type} → ticket sync: {action}", flush=True)

    except Exception as e:
        # Fire-and-forget — never let this break the main flow
        print(f"[NOVACORE-NOTIFY] Error (non-blocking): {e}", flush=True)


REPAIRSHOPR_SMS_FORWARD_URL = os.getenv("REPAIRSHOPR_SMS_FORWARD_URL")
REPAIRSHOPR_TOKEN = os.getenv("REPAIRSHOPR_TOKEN")
REPAIRSHOPR_API_KEY = os.getenv("REPAIRSHOPR_API_KEY")
REPAIRSHOPR_BASE_URL = os.getenv("REPAIRSHOPR_BASE_URL")



# normalize_phone_number and get_contact_name imported from phone_utils


def handle_stop_start_messages(phone_number, message_body):
    """Handle STOP/START opt-out messages with auto-replies"""
    from twilio.rest import Client

    if not message_body:
        return False

    message_upper = message_body.strip().upper()
    normalized_phone = normalize_phone_number(phone_number)

    if not normalized_phone:
        return False

    # Handle STOP messages
    message_clean = message_upper.strip()
    if message_clean in ["STOP", "UNSUBSCRIBE", "QUIT", "END", "CANCEL", "OPTOUT"]:
        try:
            conn = get_db_connection()
            cur = conn.cursor()

            # First, try to find existing contact
            cur.execute(
                """
                SELECT id FROM contacts 
                WHERE phone_primary = %s OR phone_secondary = %s
            """,
                (normalized_phone, normalized_phone),
            )

            contact = cur.fetchone()

            if contact:
                # Update existing contact
                cur.execute(
                    """
                    UPDATE contacts 
                    SET opted_out_sms = 1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                """,
                    (contact["id"],),
                )
                print(
                    f"✅ Updated existing contact opt-out status for {normalized_phone}"
                )
            else:
                # Create new contact record for opt-out tracking
                cur.execute(
                    """
                    INSERT INTO contacts (phone_primary, name, opted_out_sms, created_at, updated_at)
                    VALUES (%s, NULL, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                    (normalized_phone,),
                )
                print(
                    f"✅ Created new contact record with opt-out for {normalized_phone}"
                )

            conn.commit()
            conn.close()

            # Send confirmation reply
            account_sid = os.getenv("TWILIO_ACCOUNT_SID")
            auth_token = os.getenv("TWILIO_AUTH_TOKEN")
            sms_from_number = os.getenv("TWILIO_SMS_NUMBER", "+17754602190")

            if account_sid and auth_token:
                client = Client(account_sid, auth_token)

                stop_reply = (
                    "✅ You've been removed from PC Reps auto-SMS for missed calls. "
                    "You can still text us anytime with questions! Reply START to re-enable auto-SMS."
                )

                try:
                    reply_message = client.messages.create(
                        body=stop_reply,
                        from_=sms_from_number,
                        to=normalized_phone,
                        status_callback="https://softphone.pc-reps.com/messages/status",
                    )

                    print(
                        f"📤 STOP confirmation sent to {normalized_phone}: {reply_message.sid}"
                    )

                    # Log the auto-reply
                    log_message(
                        "outbound", normalized_phone, stop_reply, [], reply_message.sid
                    )

                except Exception as e:
                    print(f"❌ Error sending STOP confirmation: {e}")

            return True

        except Exception as e:
            print(f"❌ Error handling STOP message from {phone_number}: {e}")
            return False

    # Handle START messages
    elif message_clean in ["START", "SUBSCRIBE", "OPTIN"]:
        try:
            conn = get_db_connection()
            cur = conn.cursor()

            # Find existing contact
            cur.execute(
                """
                SELECT id FROM contacts 
                WHERE phone_primary = %s OR phone_secondary = %s
            """,
                (normalized_phone, normalized_phone),
            )

            contact = cur.fetchone()

            if contact:
                # Update existing contact
                cur.execute(
                    """
                    UPDATE contacts 
                    SET opted_out_sms = 0, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                """,
                    (contact["id"],),
                )
                print(f"✅ Updated contact opt-in status for {normalized_phone}")
            else:
                # Create new contact record with opt-in
                cur.execute(
                    """
                    INSERT INTO contacts (phone_primary, name, opted_out_sms, created_at, updated_at)
                    VALUES (%s, NULL, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                    (normalized_phone,),
                )
                print(
                    f"✅ Created new contact record with opt-in for {normalized_phone}"
                )

            conn.commit()
            conn.close()

            # Send confirmation reply
            account_sid = os.getenv("TWILIO_ACCOUNT_SID")
            auth_token = os.getenv("TWILIO_AUTH_TOKEN")
            sms_from_number = os.getenv("TWILIO_SMS_NUMBER", "+17754602190")

            if account_sid and auth_token:
                client = Client(account_sid, auth_token)

                start_reply = (
                    "✅ You're now subscribed to PC Reps auto-SMS for missed calls. "
                    "We'll text you when you call and we can't answer. Reply STOP anytime to opt out."
                )

                try:
                    reply_message = client.messages.create(
                        body=start_reply,
                        from_=sms_from_number,
                        to=normalized_phone,
                        status_callback="https://softphone.pc-reps.com/messages/status",
                    )

                    print(
                        f"📤 START confirmation sent to {normalized_phone}: {reply_message.sid}"
                    )

                    # Log the auto-reply
                    log_message(
                        "outbound", normalized_phone, start_reply, [], reply_message.sid
                    )

                except Exception as e:
                    print(f"❌ Error sending START confirmation: {e}")

            return True

        except Exception as e:
            print(f"❌ Error handling START message from {phone_number}: {e}")
            return False

    return False


def send_status_auto_reply(phone_number):
    """Send automatic status reply for priority greeting types (sick, vacation, holiday)"""
    from twilio.rest import Client
    from datetime import timedelta

    try:
        normalized_phone = normalize_phone_number(phone_number)
        if not normalized_phone:
            return False

        # Get active greeting to check if we should send status reply
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM greetings WHERE is_active = 1 LIMIT 1")
        active_greeting = cur.fetchone()
        conn.close()

        if not active_greeting:
            return False

        # Only send for priority greeting types
        priority_types = ["sick", "vacation", "holiday"]
        if active_greeting["type"] not in priority_types:
            return False

        # Check if we already sent a status reply recently (24 hour cooldown for status replies)
        cutoff_time = (datetime.now() - timedelta(hours=24)).isoformat()

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT timestamp FROM messages 
            WHERE phone_number = %s 
            AND direction = 'outbound' 
            AND (is_auto_sms = 1 OR body ILIKE '%PC Reps%sick%' OR body ILIKE '%PC Reps%vacation%' OR body ILIKE '%PC Reps%holiday%')
            AND timestamp > %s
            ORDER BY timestamp DESC LIMIT 1
        """,
            (normalized_phone, cutoff_time),
        )

        recent_status = cur.fetchone()
        conn.close()

        if recent_status:
            print(
                f"⏰ Status auto-reply cooldown active for {normalized_phone} (24 hour cooldown)"
            )
            return False

        # Send the status message
        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        sms_from_number = os.getenv("TWILIO_SMS_NUMBER", "+17754602190")

        if not account_sid or not auth_token:
            return False

        client = Client(account_sid, auth_token)
        status_message = active_greeting["auto_sms_message"]

        if status_message:
            message = client.messages.create(
                body=status_message,
                from_=sms_from_number,
                to=normalized_phone,
                status_callback="https://softphone.pc-reps.com/messages/status",
            )

            print(
                f"🚨 Status auto-reply sent to {normalized_phone}: {active_greeting['type']} - {message.sid}"
            )

            # Log the status reply
            log_message(
                "outbound",
                normalized_phone,
                status_message,
                [],
                message.sid,
                is_auto_sms=True,
            )

            # Log analytics event
            try:
                from analytics import log_analytics_event

                log_analytics_event(
                    active_greeting["type"],
                    active_greeting["name"],
                    "status_reply_sent",
                    normalized_phone,
                )
            except Exception as e:
                print(f"Analytics logging failed: {e}")

            return True

    except Exception as e:
        print(f"⌚ Error sending status auto-reply: {e}")
        return False


def send_closed_day_text_reply(phone_number):
    """Send automatic reply for texts received on closed days (Sunday & Monday)"""
    from twilio.rest import Client
    from datetime import timedelta
    from pytz import timezone

    try:
        # Import the business hours check from incoming.py
        from incoming import is_open_now

        # Only send on closed days
        if is_open_now():
            return False

        # Check if it's Sunday (6) or Monday (0) specifically
        pacific = timezone("US/Pacific")
        now = datetime.now(pacific)
        if now.weekday() not in [6, 0]:  # 6 = Sunday, 0 = Monday
            return False

        normalized_phone = normalize_phone_number(phone_number)
        if not normalized_phone:
            return False

        # Check if we already sent a closed-day text reply recently (24 hour cooldown)
        cutoff_time = (datetime.now() - timedelta(hours=24)).isoformat()

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT timestamp FROM messages 
            WHERE phone_number = %s 
            AND direction = 'outbound' 
            AND is_auto_sms = 1
            AND timestamp > %s
            ORDER BY timestamp DESC LIMIT 1
        """,
            (normalized_phone, cutoff_time),
        )

        recent_auto_reply = cur.fetchone()
        conn.close()

        if recent_auto_reply:
            print(
                f"⏰ Closed-day text auto-reply cooldown active for {normalized_phone} (24 hour cooldown)"
            )
            return False

        # Check if user has opted out
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT opted_out_sms, suppress_auto_sms FROM contacts 
            WHERE phone_primary = %s OR phone_secondary = %s
        """,
            (normalized_phone, normalized_phone),
        )

        contact = cur.fetchone()
        conn.close()

        if contact and (contact["opted_out_sms"] or contact["suppress_auto_sms"]):
            print(
                f"🚫 Closed-day text auto-reply suppressed for {normalized_phone} (opted out)"
            )
            return False

        # Send the closed-day message
        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        sms_from_number = os.getenv("TWILIO_SMS_NUMBER", "+17754602190")

        if not account_sid or not auth_token:
            return False

        client = Client(account_sid, auth_token)

        # Fetch closed message template from database
        default_closed = "Hi, this is PC Reps 👋 We're currently closed (open Tue, Thu, Sat 10–6). For the fastest response, text us your question and we'll get back to you when we open! Reply STOP to opt out."

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT setting_value FROM app_settings WHERE setting_key = 'auto_sms_closed_message'"
        )
        row = cur.fetchone()
        conn.close()
        closed_message = row["setting_value"] if row else default_closed

        message = client.messages.create(
            body=closed_message,
            from_=sms_from_number,
            to=normalized_phone,
            status_callback="https://softphone.pc-reps.com/messages/status",
        )

        print(
            f"📱 Closed-day text auto-reply sent to {normalized_phone}: {message.sid}"
        )
        print(f"📝 Day: {now.strftime('%A')}")

        # Log the auto-reply
        log_message(
            "outbound",
            normalized_phone,
            closed_message,
            [],
            message.sid,
            is_auto_sms=True,
        )

        # Log analytics event
        try:
            from analytics import log_analytics_event

            log_analytics_event(
                "closed_day_text",
                "Closed Day Text Auto-Reply",
                "text_auto_reply_sent",
                normalized_phone,
            )
        except Exception as e:
            print(f"Analytics logging failed: {e}")

        return True

    except Exception as e:
        print(f"❌ Error sending closed-day text auto-reply: {e}")
        return False


def log_message(
    direction,
    phone_number,
    body,
    media_urls=None,
    twilio_sid=None,
    is_auto_sms=False,
    user_id=None,
):
    """UPDATED: Log message to master database with reaction detection"""
    try:
        normalized_phone = normalize_phone_number(phone_number)
        if not normalized_phone:
            print(f"⚠️ Skipping message log - invalid phone: {phone_number}")
            return

        # Detect if this is a reaction message
        is_reaction = False
        reacted_to_message_id = None

        if direction == "inbound" and body:
            # Common reaction patterns
            reaction_patterns = [
                r'^Liked "(.+)"$',
                r'^Loved "(.+)"$',
                r'^Laughed at "(.+)"$',
                r'^Emphasized "(.+)"$',
                r'^Questioned "(.+)"$',
                r'^Disliked "(.+)"$',
            ]

            for pattern in reaction_patterns:
                match = re.match(pattern, body.strip())
                if match:
                    quoted_text = match.group(1)
                    is_reaction = True

                    # Find the original message this reacts to
                    conn = get_db_connection()
                    cur = conn.cursor()
                    cur.execute(
                        """
                        SELECT id FROM messages 
                        WHERE phone_number = %s 
                        AND direction = 'outbound' 
                        AND body LIKE %s 
                        ORDER BY timestamp DESC 
                        LIMIT 1
                    """,
                        (normalized_phone, f"%{quoted_text}%"),
                    )

                    result = cur.fetchone()
                    if result:
                        reacted_to_message_id = result["id"]
                    conn.close()
                    break

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO messages (direction, phone_number, body, media_urls, timestamp, read, twilio_sid, status, is_auto_sms, is_reaction, reacted_to_message_id, user_id)
            VALUES (%s, %s, %s, %s, %s, 0, %s, %s, %s, %s, %s, %s)
        """,
            (
                direction,
                normalized_phone,
                body,
                json.dumps(media_urls or []),
                datetime.now(pacific).isoformat(),
                twilio_sid,
                "sent" if direction == "outbound" else None,
                int(is_auto_sms),
                int(is_reaction),
                reacted_to_message_id,
                user_id,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Error logging message: {e}")


def forward_sms_to_repairshopr(request_form):
    try:
        payload = request_form.to_dict()
        url = f"{REPAIRSHOPR_SMS_FORWARD_URL}?token={REPAIRSHOPR_TOKEN}"
        print(f"📨 Forwarding SMS to RS → {url}")
        print(f"📦 Payload: {payload}")
        response = requests.post(url, data=payload, timeout=5)
        print(f"📬 Response Code: {response.status_code}")
        print(f"📬 Response Body: {response.text}")
        response.raise_for_status()
        print(f"✅ Forwarded SMS to RepairShopr (status {response.status_code})")
    except Exception as e:
        print(f"❌ Failed to forward SMS to RepairShopr: {e}")


def post_outbound_comment_to_ticket(phone_number, message_body):
    import re
    import subprocess
    import requests

    try:
        headers = {"Authorization": f"Bearer {REPAIRSHOPR_API_KEY}"}
        normalized_phone = re.sub(r"\D", "", phone_number)[-10:]
        search_url = f"{REPAIRSHOPR_BASE_URL}/customers?query={normalized_phone}"
        print(f"📡 Looking up customer with phone: {normalized_phone}")
        print(f"🔗 Full URL: {search_url}")
        resp = requests.get(search_url, headers=headers, timeout=5)
        if resp.status_code != 200:
            print(f"❌ Failed to lookup customer: {resp.text}")
            return
        data = resp.json()
        customers = data.get("customers", [])
        if not customers:
            print(f"ℹ️ No matching customer found. Response was: {data}")
            return

        customer_id = customers[0].get("id")
        ticket_resp = requests.get(
            f"{REPAIRSHOPR_BASE_URL}/tickets",
            headers=headers,
            params={"customer_id": customer_id},
            timeout=5,
        )
        print(f"📬 Ticket lookup response: {ticket_resp.status_code}")
        print(f"🧾 Tickets found: {len(ticket_resp.json().get('tickets', []))}")
        if ticket_resp.status_code != 200:
            print(f"❌ Failed to get tickets: {ticket_resp.text}")
            return
        ticket_data = ticket_resp.json()
        tickets = ticket_data.get("tickets", [])
        open_tickets = [
            t
            for t in tickets
            if t.get("status")
            in ["New", "In Progress", "Waiting on Customer", "Customer Reply"]
        ]
        if not open_tickets:
            print("ℹ️ No open tickets for this customer. Skipping comment.")
            return
        ticket_id = open_tickets[0].get("id")
        result = subprocess.run(
            [
                "powershell",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                "E:/PC Reps/softphone/send_comment_to_rs.ps1",
                "-TicketId",
                str(ticket_id),
                "-Message",
                message_body,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        print("✅ PowerShell script executed successfully.")
        print("STDOUT:", result.stdout)
        print("STDERR:", result.stderr)
    except subprocess.CalledProcessError as e:
        print("❌ PowerShell script failed:")
        print("Return Code:", e.returncode)
        print("STDOUT:", e.stdout)
        print("STDERR:", e.stderr)
    except Exception as e:
        import traceback

        print(f"❌ Error posting comment to RepairShopr: {e}")
        traceback.print_exc()


@messaging_bp.route("/messages/status", methods=["POST"])
def message_status_callback():
    try:
        message_sid = request.form.get("MessageSid")
        status = request.form.get("MessageStatus")  # sent, delivered, failed, etc.
        error_code = request.form.get("ErrorCode")
        error_message = request.form.get("ErrorMessage")

        print(
            f"📊 Received status callback: SID={message_sid}, Status={status}, Error={error_code}"
        )
        print(f"📋 Full callback data: {dict(request.form)}")

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE messages 
            SET status = %s, status_reason = %s
            WHERE twilio_sid = %s
        """,
            (
                status,
                error_message or f"Error {error_code}" if error_code else None,
                message_sid,
            ),
        )
        affected_rows = cur.rowcount
        conn.commit()
        conn.close()

        print(
            f"📊 Updated message {message_sid} status: {status} (affected {affected_rows} rows)"
        )

        # Handle delivery failures with better logging
        if status in ["undelivered", "failed"] and error_code:
            if error_code == "30006":
                print(
                    f"📵 LANDLINE DETECTED: Message {message_sid} failed - recipient is likely a landline or carrier doesn't support SMS"
                )
            elif error_code == "30007":
                print(
                    f"📵 CARRIER FILTERING: Message {message_sid} filtered by carrier - likely a landline"
                )
            elif error_code == "30008":
                print(
                    f"📵 UNKNOWN DESTINATION: Message {message_sid} failed - invalid or unreachable number"
                )
            else:
                print(
                    f"❌ MESSAGE DELIVERY FAILED: {message_sid} - Error {error_code}: {error_message}"
                )

        # Legacy handling for external outbound messages
        direction = request.form.get("Direction")
        phone_number = request.form.get("To")
        body = request.form.get("Body")
        if direction == "outbound-api" and body and status == "sent":
            print(f"📤 Logging external outbound message to {phone_number}: {body}")
            log_message("outbound", phone_number, body, [])

    except Exception as e:
        print(f"❌ Error in status callback: {e}")
        import traceback

        traceback.print_exc()
    return Response(status=204)


@messaging_bp.route("/messages/send", methods=["POST"])
def send_sms():
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    sms_from_number = os.getenv("TWILIO_SMS_NUMBER", "+17754602190")

    if not account_sid or not auth_token:
        return jsonify({"error": "Twilio credentials are missing"}), 500

    client = Client(account_sid, auth_token)

    to_number = request.form.get("to")
    message_body = request.form.get("message")

    # Get multiple files - they'll be named 'media[]' or just multiple 'media' fields
    media_files = request.files.getlist("media")

    try:
        # Process all media files first
        media_urls = []

        if media_files:
            print(f"📎 Processing {len(media_files)} media files")

            for i, media_file in enumerate(media_files):
                if media_file.filename:  # Make sure file has a name
                    print(f"📎 Processing file {i+1}: {media_file.filename}")

                    # Sanitize filename
                    import re

                    safe_filename = re.sub(r"[^\w\-_\.]", "_", media_file.filename)

                    # Add timestamp to avoid filename conflicts
                    import time

                    timestamp = str(int(time.time()))
                    name, ext = os.path.splitext(safe_filename)
                    safe_filename = f"{name}_{timestamp}_{i}{ext}"

                    save_path = f"static/uploads/{safe_filename}"
                    print(f"📁 Save path: {save_path}")

                    os.makedirs(os.path.dirname(save_path), exist_ok=True)
                    media_file.save(save_path)
                    print(f"💾 File {i+1} saved successfully")

                    # Add debugging here:
                    print(f"🔍 Original file type: {media_file.content_type}")
                    print(f"🔍 Original filename: {media_file.filename}")
                    print(f"🔍 Safe filename: {safe_filename}")
                    print(f"🔍 File exists after save: {os.path.exists(save_path)}")

                    import mimetypes

                    detected_type, _ = mimetypes.guess_type(save_path)
                    print(f"🔍 Detected MIME type: {detected_type}")

                    # Compress if needed
                    try:
                        file_size = os.path.getsize(save_path)
                        print(f"📦 File {i+1} size: {file_size} bytes")
                        if file_size > 5000000:
                            print(
                                f"⚠️ File {i+1} too large for Twilio. Attempting to compress..."
                            )
                            try:
                                with Image.open(save_path) as img:
                                    img_format = (
                                        img.format
                                        if img.format in ["JPEG", "PNG"]
                                        else "JPEG"
                                    )
                                    max_width = 1024
                                    if img.width > max_width:
                                        ratio = max_width / img.width
                                        new_size = (
                                            int(img.width * ratio),
                                            int(img.height * ratio),
                                        )
                                        img = img.resize(new_size, Image.LANCZOS)
                                    buffer = BytesIO()
                                    img.save(
                                        buffer,
                                        format=img_format,
                                        optimize=True,
                                        quality=85,
                                    )
                                    with open(save_path, "wb") as f:
                                        f.write(buffer.getvalue())
                                    print(
                                        f"✅ File {i+1} compression complete. New size:",
                                        os.path.getsize(save_path),
                                        "bytes",
                                    )
                            except Exception as e:
                                print(f"❌ File {i+1} compression failed:", e)
                    except Exception as e:
                        print(f"❌ File {i+1} size check failed:", e)

                    media_url = f"https://softphone.pc-reps.com/uploads/{safe_filename}"
                    media_urls.append(media_url)
                    print(f"🌐 File {i+1} Media URL: {media_url}")

        # Twilio limits: 10 media attachments OR ~4.5MB total size per message
        MAX_MEDIA_PER_MESSAGE = 10
        MAX_TOTAL_SIZE = 4500000  # 4.5MB safety margin

        # Calculate total size of all files
        total_size = 0
        for media_url in media_urls:
            filename = media_url.split("/")[-1]  # Get filename from URL
            file_path = f"static/uploads/{filename}"
            if os.path.exists(file_path):
                total_size += os.path.getsize(file_path)

        print(f"📏 Total attachment size: {total_size / (1024 * 1024):.2f} MB")

        if len(media_urls) > MAX_MEDIA_PER_MESSAGE or total_size > MAX_TOTAL_SIZE:
            # Split into multiple messages if too many files
            print(
                f"📨 Too many files ({len(media_urls)}), splitting into multiple messages"
            )

            # Split into chunks by both file count AND total size
            message_chunks = []
            current_chunk = []
            current_chunk_size = 0

            for i, media_url in enumerate(media_urls):
                filename = media_url.split("/")[-1]
                file_path = f"static/uploads/{filename}"
                file_size = (
                    os.path.getsize(file_path) if os.path.exists(file_path) else 0
                )

                # Check if adding this file would exceed limits
                if (
                    len(current_chunk) >= MAX_MEDIA_PER_MESSAGE
                    or (current_chunk_size + file_size) > MAX_TOTAL_SIZE
                ) and current_chunk:
                    # Start new chunk
                    message_chunks.append(current_chunk)
                    current_chunk = [media_url]
                    current_chunk_size = file_size
                else:
                    # Add to current chunk
                    current_chunk.append(media_url)
                    current_chunk_size += file_size

            # Don't forget the last chunk
            if current_chunk:
                message_chunks.append(current_chunk)

            print(
                f"📨 Splitting into {len(message_chunks)} messages by size/count limits"
            )

            # Send first message with text + first batch of media
            kwargs = {
                "body": message_body if message_body else "",
                "from_": sms_from_number,
                "to": to_number,
                "status_callback": "https://softphone.pc-reps.com/messages/status",
                "media_url": message_chunks[0],
            }

            print(f"📤 Sending message 1 with {len(message_chunks[0])} media files")
            message = client.messages.create(**kwargs)
            print(f"✅ Message 1 sent successfully: {message.sid}")

            # Send additional messages for remaining media
            for chunk_idx, chunk_media in enumerate(message_chunks[1:], 2):
                kwargs = {
                    "body": "",
                    "from_": sms_from_number,
                    "to": to_number,
                    "status_callback": "https://softphone.pc-reps.com/messages/status",
                    "media_url": chunk_media,
                }

                print(
                    f"📤 Sending message {chunk_idx} with {len(chunk_media)} media files"
                )
                additional_message = client.messages.create(**kwargs)
                print(
                    f"✅ Message {chunk_idx} sent successfully: {additional_message.sid}"
                )

                # Small delay between messages
                import time

                time.sleep(1)

            # Log all media in the first message
            log_message(
                "outbound",
                to_number,
                message_body,
                media_urls,
                message.sid,
                user_id=current_user.id if current_user.is_authenticated else None,
            )

        else:
            # Single message with all media
            kwargs = {
                "body": message_body,
                "from_": sms_from_number,
                "to": to_number,
                "status_callback": "https://softphone.pc-reps.com/messages/status",
            }

            if media_urls:
                kwargs["media_url"] = media_urls

            print(f"📤 Sending single message with {len(media_urls)} media files")
            message = client.messages.create(**kwargs)
            print(f"✅ Message sent successfully: {message.sid}")

            # Store the message with Twilio SID for status tracking
            log_message(
                "outbound",
                to_number,
                message_body,
                media_urls,
                message.sid,
                user_id=current_user.id if current_user.is_authenticated else None,
            )

        post_outbound_comment_to_ticket(to_number, message_body)

        # Notify NovaCore to log this outbound SMS on the customer's ticket
        notify_novacore_ticket(to_number, "outbound", "sms", body=message_body, staff_user_id=current_user.id if current_user.is_authenticated else None)

        return jsonify(
            {
                "status": "sent",
                "sid": message.sid,
                "to": message.to,
                "from": message.from_,
                "body": message.body,
                "media_count": len(media_urls),
            }
        )

    except Exception as e:
        print(f"❌ FULL ERROR DETAILS:")
        print(f"❌ Error type: {type(e).__name__}")
        print(f"❌ Error message: {str(e)}")
        import traceback

        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@messaging_bp.route("/api/external/send-sms", methods=["POST"])
def external_send_sms():
    """
    External API endpoint for sending SMS from other applications (e.g., NovaCore).
    Accepts JSON, validates API key, sends via Twilio, and logs to Softphone database.
    """
    # Validate API key
    expected_api_key = os.getenv("EXTERNAL_SMS_API_KEY")
    if not expected_api_key:
        print("❌ EXTERNAL_SMS_API_KEY not configured in environment")
        return jsonify({"success": False, "error": "API not configured"}), 500

    # Get API key from header or body
    provided_key = request.headers.get("X-API-Key") or request.json.get("api_key")
    if not provided_key or provided_key != expected_api_key:
        print(f"❌ Invalid API key attempt from {request.remote_addr}")
        return jsonify({"success": False, "error": "Invalid API key"}), 401

    # Get request data
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "No JSON data provided"}), 400

    to_number = data.get("to")
    message_body = data.get("message")
    source = data.get("source", "external")  # e.g., "novacore", "external"

    if not to_number or not message_body:
        return (
            jsonify({"success": False, "error": "Missing 'to' or 'message' field"}),
            400,
        )

    # Normalize phone number
    normalized_to = normalize_phone_number(to_number)
    if not normalized_to:
        return (
            jsonify({"success": False, "error": f"Invalid phone number: {to_number}"}),
            400,
        )

    # Get Twilio credentials
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    sms_from_number = os.getenv("TWILIO_SMS_NUMBER", "+17754602190")

    if not account_sid or not auth_token:
        return (
            jsonify({"success": False, "error": "Twilio credentials not configured"}),
            500,
        )

    try:
        # Send via Twilio
        client = Client(account_sid, auth_token)
        message = client.messages.create(
            body=message_body,
            from_=sms_from_number,
            to=normalized_to,
            status_callback="https://softphone.pc-reps.com/messages/status",
        )

        print(
            f"✅ External SMS sent: {message.sid} to {normalized_to} (source: {source})"
        )

        # Log to Softphone database so it appears in the UI
        log_message(
            direction="outbound",
            phone_number=normalized_to,
            body=message_body,
            media_urls=None,
            twilio_sid=message.sid,
            user_id=None,  # External API, no logged-in user
        )

        return jsonify(
            {"success": True, "sid": message.sid, "to": normalized_to, "source": source}
        )

    except Exception as e:
        print(f"❌ External SMS failed: {type(e).__name__}: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


@messaging_bp.route("/messages/incoming", methods=["POST"])
def receive_sms():
    print(f"🔥🔥🔥 SMS WEBHOOK HIT! Time: {datetime.utcnow()}")
    print(f"🔥 RECEIVE SMS FUNCTION CALLED!")
    print(f"🔥 Request form data: {dict(request.form)}")

    from_number = request.form.get("From")
    body = request.form.get("Body")
    num_media = int(request.form.get("NumMedia", "0"))
    media_urls = []
    for i in range(num_media):
        partial = request.form.get(f"MediaUrl{i}")
        if partial:
            media_urls.append(
                partial
                if partial.startswith("http")
                else f"https://api.twilio.com{partial}"
            )

    print(f"📥 New SMS from {from_number}: {body}")
    if media_urls:
        print(f"📎 Media: {media_urls}")

    log_message("inbound", from_number, body, media_urls)

    # Notify NovaCore to log this inbound SMS on the customer's ticket
    notify_novacore_ticket(from_number, "inbound", "sms", body=body or "")

    # NEW: Handle STOP/START messages with auto-replies
    if handle_stop_start_messages(from_number, body):
        print(f"🔄 Processed STOP/START message from {from_number}")

    # Send status auto-reply for priority greeting types (sick, vacation, holiday)
    status_reply_sent = send_status_auto_reply(from_number)

    # Only send closed-day auto-reply if we didn't already send a status reply
    if not status_reply_sent:
        send_closed_day_text_reply(from_number)

    # Emit real-time notification
    try:
        from flask import current_app

        socketio = current_app.extensions["socketio"]
        print(
            f"📡 Attempting to emit WebSocket event for {from_number} on instance {os.getpid()}"
        )
        print(
            f"🔍 Active SocketIO connections: {len(socketio.server.manager.rooms.get('/', {}).keys()) if hasattr(socketio.server, 'manager') else 'unknown'}"
        )

        socketio.emit(
            "new_message",
            {
                "phone_number": from_number,
                "message": body,
                "timestamp": datetime.utcnow().isoformat(),
            },
        )
        print(f"✅ WebSocket event emitted successfully")
    except Exception as e:
        print(f"❌ Error emitting WebSocket event: {e}")
        import traceback

        traceback.print_exc()

    # Forward to RepairShopr
    forward_sms_to_repairshopr(request.form)

    return "<Response></Response>", 200, {"Content-Type": "text/xml"}


@messaging_bp.route("/messages/delete-threads", methods=["POST"])
def delete_threads():
    try:
        data = request.get_json()
        phone_numbers = data.get("phone_numbers", [])

        if not phone_numbers:
            return jsonify({"error": "No phone numbers provided"}), 400

        conn = get_db_connection()
        cur = conn.cursor()
        # Normalize phone numbers before deletion
        normalized_numbers = []
        for pn in phone_numbers:
            normalized = normalize_phone_number(pn)
            if normalized:
                normalized_numbers.append(normalized)

        if normalized_numbers:
            for pn in normalized_numbers:
                cur.execute("DELETE FROM messages WHERE phone_number = %s", (pn,))
            conn.commit()
        conn.close()

        return jsonify({"status": "deleted", "count": len(normalized_numbers)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



# get_contact_name imported from phone_utils (see top of file)


@messaging_bp.route("/messages/thread/<phone_number>", methods=["GET"])
def get_thread(phone_number):
    try:
        # UPDATED: Normalize phone number for lookup
        normalized_phone = normalize_phone_number(phone_number)
        if not normalized_phone:
            return jsonify([])

        # First, get all users from NovaCore (PostgreSQL) to build a lookup map
        user_map = {}
        try:
            novacore_conn = get_novacore_connection()
            novacore_cursor = novacore_conn.cursor()
            novacore_cursor.execute(
                "SELECT id, first_name, last_name, user_color FROM users"
            )
            for user_row in novacore_cursor.fetchall():
                user_map[user_row["id"]] = {
                    "first_name": user_row["first_name"],
                    "last_name": user_row["last_name"],
                    "user_color": user_row["user_color"],
                }
            novacore_conn.close()
        except Exception as e:
            print(f"⚠️ Error fetching users from NovaCore: {e}")

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM messages
            WHERE phone_number = %s
            ORDER BY timestamp ASC
        """,
            (normalized_phone,),
        )
        rows = cur.fetchall()
        conn.close()

        messages = []
        for row in rows:
            msg_data = {
                "id": row["id"],
                "direction": row["direction"],
                "phone_number": row["phone_number"],
                "body": row["body"],
                "timestamp": row["timestamp"],
                "media_urls": json.loads(row["media_urls"] or "[]"),
                "status": row.get("status"),
                "status_reason": row.get("status_reason"),
                "twilio_sid": row.get("twilio_sid"),
                "pinned": row.get("pinned", False),
                "is_reaction": row.get("is_reaction", False),
                "reacted_to_message_id": row.get("reacted_to_message_id"),
                "user_id": row.get("user_id"),
            }

            # Add user info if available
            if msg_data["user_id"] and msg_data["user_id"] in user_map:
                user_info = user_map[msg_data["user_id"]]
                msg_data["user_first_name"] = user_info["first_name"]
                msg_data["user_last_name"] = user_info["last_name"]
                msg_data["user_color"] = user_info["user_color"]

            messages.append(msg_data)
        return jsonify(messages)
    except Exception as e:
        print("❌ Error fetching thread:", e)
        return jsonify([])


@messaging_bp.route("/messages/mark-read/<phone_number>", methods=["POST"])
def mark_thread_read(phone_number):
    try:
        normalized_phone = normalize_phone_number(phone_number)
        if not normalized_phone:
            return jsonify({"error": "Invalid phone number"}), 400

        conn = get_db_connection()
        cur = conn.cursor()
        # Only mark INBOUND messages as read - outbound messages shouldn't affect read status
        cur.execute(
            """
            UPDATE messages
            SET read = 1
            WHERE phone_number = %s AND direction = 'inbound' AND read = 0
        """,
            (normalized_phone,),
        )
        updated = cur.rowcount
        conn.commit()
        conn.close()
        if updated > 0:
            print(
                f"✅ Marked {updated} inbound messages as read for {normalized_phone}"
            )
        return jsonify({"status": "ok", "updated": updated})
    except Exception as e:
        print(f"❌ Error in mark-read: {e}")
        return jsonify({"error": str(e)}), 500


@messaging_bp.route("/messages/mark-unread/<phone_number>", methods=["POST"])
def mark_thread_unread(phone_number):
    try:
        normalized_phone = normalize_phone_number(phone_number)
        if not normalized_phone:
            return jsonify({"error": "Invalid phone number"}), 400

        conn = get_db_connection()
        cur = conn.cursor()
        # Only mark the most recent inbound message as unread (for visual indicator)
        cur.execute(
            """
            UPDATE messages
            SET read = 0
            WHERE phone_number = %s AND direction = 'inbound'
            AND id = (
                SELECT id FROM messages 
                WHERE phone_number = %s AND direction = 'inbound' 
                ORDER BY timestamp DESC 
                LIMIT 1
            )
        """,
            (normalized_phone, normalized_phone),
        )
        updated = cur.rowcount
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "updated": updated})
    except Exception as e:
        print(f"⚠️ Error in mark-unread: {e}")
        return jsonify({"error": str(e)}), 500


@messaging_bp.route("/messages/search", methods=["GET"])
def search_messages():
    try:
        query = request.args.get("q", "").strip()
        if not query:
            return jsonify([])

        conn = get_db_connection()
        cur = conn.cursor()

        # Search through all messages
        search_term = f"%{query}%"
        cur.execute(
            """
            SELECT DISTINCT phone_number, 
                   COUNT(*) as match_count,
                   MAX(timestamp) as latest_match_timestamp
            FROM messages 
            WHERE body ILIKE %s
            GROUP BY phone_number
            ORDER BY latest_match_timestamp DESC
        """,
            (search_term,),
        )

        search_results = []
        for row in cur.fetchall():
            phone_number = row["phone_number"]
            match_count = row["match_count"]

            # Get contact name
            contact_name = get_contact_name(phone_number)

            # Get a sample of matching messages
            cur.execute(
                """
                SELECT body, timestamp, direction
                FROM messages 
                WHERE phone_number = %s AND body ILIKE %s
                ORDER BY timestamp DESC
                LIMIT 3
            """,
                (phone_number, search_term),
            )

            matching_messages = []
            for msg_row in cur.fetchall():
                matching_messages.append(
                    {
                        "body": msg_row["body"],
                        "timestamp": msg_row["timestamp"],
                        "direction": msg_row["direction"],
                    }
                )

            search_results.append(
                {
                    "phone_number": phone_number,
                    "contact_name": contact_name,
                    "match_count": match_count,
                    "latest_match_timestamp": row["latest_match_timestamp"],
                    "sample_messages": matching_messages,
                }
            )

        conn.close()
        return jsonify(search_results)

    except Exception as e:
        print("❌ Error in message search:", e)
        return jsonify([])


@messaging_bp.route("/contacts/search", methods=["GET"])
def search_contacts():
    try:
        query = request.args.get("q", "").strip()
        if not query:
            return jsonify([])

        conn = get_db_connection()
        cur = conn.cursor()

        # Build search query for master database structure
        search_conditions = []
        params = []

        # Search name
        search_conditions.append("name ILIKE %s")
        params.append(f"%{query}%")

        # Search primary and secondary phone numbers
        search_conditions.append(
            "REPLACE(REPLACE(phone_primary, '+1', ''), '+', '') LIKE %s"
        )
        params.append(f"%{query.replace('+1', '').replace('+', '')}%")

        search_conditions.append(
            "REPLACE(REPLACE(phone_secondary, '+1', ''), '+', '') LIKE %s"
        )
        params.append(f"%{query.replace('+1', '').replace('+', '')}%")

        # Search company if available
        search_conditions.append("company ILIKE %s")
        params.append(f"%{query}%")

        # Execute search
        search_query = f"""
            SELECT * FROM contacts 
            WHERE {' OR '.join(search_conditions)}
            ORDER BY name ASC
            LIMIT 20
        """

        cur.execute(search_query, params)
        contacts = []
        for row in cur.fetchall():
            contact = dict(row)
            contacts.append(contact)

        conn.close()
        return jsonify(contacts)

    except Exception as e:
        print(f"❌ Error searching contacts: {e}")
        return jsonify([])


@messaging_bp.route("/contacts/recent", methods=["GET"])
def get_recent_contacts():
    try:
        # Get recent contacts from message threads
        conn = get_db_connection()
        cur = conn.cursor()

        # Get recent phone numbers from threads
        cur.execute(
            """
            SELECT DISTINCT phone_number, MAX(timestamp) as last_contact
            FROM messages 
            GROUP BY phone_number 
            ORDER BY last_contact DESC
            LIMIT 10
        """
        )

        recent_numbers = [row["phone_number"] for row in cur.fetchall()]

        # Look up contact details for these numbers
        recent_contacts = []

        for phone_number in recent_numbers:
            # Search for this contact using normalized phone numbers
            cur.execute(
                """
                SELECT * FROM contacts 
                WHERE phone_primary = %s OR phone_secondary = %s
                LIMIT 1
            """,
                (phone_number, phone_number),
            )

            row = cur.fetchone()
            if row:
                contact = dict(row)
                recent_contacts.append(contact)

        conn.close()
        return jsonify(recent_contacts)

    except Exception as e:
        print(f"❌ Error getting recent contacts: {e}")
        return jsonify([])


@messaging_bp.route("/thread/<phone_number>")
def open_thread(phone_number):
    """Direct link to open a specific message thread in Softphone"""
    try:
        # Normalize the phone number
        normalized_phone = normalize_phone_number(phone_number)
        if not normalized_phone:
            return "Invalid phone number", 400

        # Simple redirect page
        redirect_html = f"""
        <!DOCTYPE html>
        <html>
        <head><title>Opening Thread</title></head>
        <body style="font-family: Arial; text-align: center; padding: 50px; background: #031f3a; color: white;">
            <h2>Opening Message Thread</h2>
            <p>{normalized_phone}</p>
            <script>
                setTimeout(function() {{
                    window.location.href = '/texting%sthread={normalized_phone}';
                }}, 2000);
            </script>
        </body>
        </html>
        """

        return redirect_html, 200, {"Content-Type": "text/html"}

    except Exception as e:
        print(f"❌ Error in thread route: {e}")
        return "Error", 500


@messaging_bp.route("/messages/threads", methods=["GET"])
def get_message_threads():
    """Get all message threads — single query replaces old N+1 pattern"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Single query using CTEs to get everything at once
        # Old code ran 5 queries PER thread (N+1 pattern)
        cur.execute("""
            WITH latest_messages AS (
                SELECT
                    phone_number,
                    body,
                    direction,
                    timestamp,
                    ROW_NUMBER() OVER (PARTITION BY phone_number ORDER BY timestamp DESC) as rn
                FROM messages
            ),
            unread_counts AS (
                SELECT
                    phone_number,
                    COUNT(*) as unread_count
                FROM messages
                WHERE direction = 'inbound' AND read = 0
                GROUP BY phone_number
            ),
            outbound_latest AS (
                SELECT
                    phone_number,
                    status,
                    status_reason,
                    ROW_NUMBER() OVER (PARTITION BY phone_number ORDER BY timestamp DESC) as rn
                FROM messages
                WHERE direction = 'outbound'
            ),
            contact_flags AS (
                SELECT DISTINCT ON (COALESCE(c.phone_primary, c.phone_secondary))
                    c.phone_primary,
                    c.phone_secondary,
                    NULLIF(c.name, 'Unknown Contact') as contact_name,
                    c.flag_type_id,
                    ft.id as flag_id,
                    ft.name as flag_name,
                    ft.color as flag_color
                FROM contacts c
                LEFT JOIN flag_types ft ON c.flag_type_id = ft.id
            )
            SELECT
                lm.phone_number,
                lm.body as latest_message,
                lm.direction as latest_direction,
                lm.timestamp as latest_timestamp,
                COALESCE(uc.unread_count, 0) as unread_count,
                ol.status as last_outbound_status,
                ol.status_reason as last_outbound_status_reason,
                cf.contact_name,
                cf.flag_type_id,
                cf.flag_id,
                cf.flag_name,
                cf.flag_color
            FROM latest_messages lm
            LEFT JOIN unread_counts uc ON lm.phone_number = uc.phone_number
            LEFT JOIN outbound_latest ol ON lm.phone_number = ol.phone_number AND ol.rn = 1
            LEFT JOIN contact_flags cf ON (cf.phone_primary = lm.phone_number OR cf.phone_secondary = lm.phone_number)
            WHERE lm.rn = 1
            ORDER BY lm.timestamp DESC
        """)

        rows = cur.fetchall()
        threads = []
        for row in rows:
            flag_type = None
            if row["flag_type_id"]:
                flag_type = {
                    "id": row["flag_id"],
                    "name": row["flag_name"],
                    "color": row["flag_color"],
                }

            threads.append({
                "phone_number": row["phone_number"],
                "latest_timestamp": row["latest_timestamp"],
                "latest_message": row["latest_message"],
                "latest_direction": row["latest_direction"],
                "unread_count": int(row["unread_count"]),
                "contact_name": row["contact_name"],
                "last_outbound_status": row["last_outbound_status"],
                "last_outbound_status_reason": row["last_outbound_status_reason"],
                "is_flagged": flag_type is not None,
                "flag_type": flag_type,
            })

        conn.close()
        return jsonify(threads)
    except Exception as e:
        print("❌ Error fetching threads:", e)
        import traceback
        traceback.print_exc()
        return jsonify([])


@messaging_bp.route("/contacts/toggle-flag/<phone_number>", methods=["POST"])
def toggle_contact_flag(phone_number):
    """Set or remove a flag on a contact. Pass flag_type_id to set, null/omit to remove."""
    try:
        normalized_phone = normalize_phone_number(phone_number)
        if not normalized_phone:
            return jsonify({"error": "Invalid phone number"}), 400

        data = request.get_json() or {}
        flag_type_id = data.get("flag_type_id")  # None = unflag, integer = set flag

        conn = get_db_connection()
        cur = conn.cursor()

        # Find existing contact
        cur.execute(
            """
            SELECT id, flag_type_id FROM contacts 
            WHERE phone_primary = %s OR phone_secondary = %s
            LIMIT 1
            """,
            (normalized_phone, normalized_phone),
        )

        contact = cur.fetchone()

        if contact:
            # Update existing contact's flag
            cur.execute(
                """
                UPDATE contacts 
                SET flag_type_id = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (flag_type_id, contact["id"]),
            )
        else:
            # Create new contact record with flag
            cur.execute(
                """
                INSERT INTO contacts (phone_primary, name, flag_type_id, created_at, updated_at)
                VALUES (%s, NULL, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (normalized_phone, flag_type_id),
            )

        # Get flag type details if flagged
        flag_type = None
        if flag_type_id:
            cur.execute(
                "SELECT id, name, color FROM flag_types WHERE id = %s", (flag_type_id,)
            )
            flag_type = cur.fetchone()
            if flag_type:
                flag_type = dict(flag_type)

        conn.commit()
        conn.close()

        return jsonify(
            {"status": "ok", "flag_type_id": flag_type_id, "flag_type": flag_type}
        )

    except Exception as e:
        print(f"⚠️ Error toggling flag: {e}")
        return jsonify({"error": str(e)}), 500


@messaging_bp.route("/contacts/get-flag/<phone_number>", methods=["GET"])
def get_contact_flag(phone_number):
    """Get flag info for a contact, including flag type details"""
    try:
        normalized_phone = normalize_phone_number(phone_number)
        if not normalized_phone:
            return jsonify({"flagged": False, "flag_type_id": None, "flag_type": None})

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT c.flag_type_id, ft.id, ft.name, ft.color
            FROM contacts c
            LEFT JOIN flag_types ft ON c.flag_type_id = ft.id
            WHERE c.phone_primary = %s OR c.phone_secondary = %s
            LIMIT 1
            """,
            (normalized_phone, normalized_phone),
        )

        result = cur.fetchone()
        conn.close()

        if result and result["flag_type_id"]:
            return jsonify(
                {
                    "flagged": True,
                    "flag_type_id": result["flag_type_id"],
                    "flag_type": {
                        "id": result["id"],
                        "name": result["name"],
                        "color": result["color"],
                    },
                }
            )
        else:
            return jsonify({"flagged": False, "flag_type_id": None, "flag_type": None})

    except Exception as e:
        print(f"⚠️ Error getting flag: {e}")
        return jsonify({"flagged": False, "flag_type_id": None, "flag_type": None})


# ===== FLAG TYPES CRUD =====


@messaging_bp.route("/flag-types", methods=["GET"])
def get_flag_types():
    """Get all available flag types"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, name, color, created_at, updated_at FROM flag_types ORDER BY id ASC"
        )
        flag_types = [dict(row) for row in cur.fetchall()]
        conn.close()
        return jsonify(flag_types)
    except Exception as e:
        print(f"❌ Error fetching flag types: {e}")
        return jsonify([])


@messaging_bp.route("/flag-types", methods=["POST"])
def create_flag_type():
    """Create a new flag type"""
    try:
        data = request.get_json()
        name = data.get("name", "").strip()
        color = data.get("color", "#FF0000").strip()

        if not name:
            return jsonify({"error": "Name is required"}), 400

        if not color.startswith("#") or len(color) != 7:
            return (
                jsonify({"error": "Color must be a valid hex color (e.g., #FF0000)"}),
                400,
            )

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO flag_types (name, color, created_at, updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            RETURNING id, name, color, created_at, updated_at
            """,
            (name, color),
        )
        new_flag_type = dict(cur.fetchone())
        conn.commit()
        conn.close()

        return jsonify(new_flag_type), 201

    except Exception as e:
        print(f"❌ Error creating flag type: {e}")
        return jsonify({"error": str(e)}), 500


@messaging_bp.route("/flag-types/<int:flag_type_id>", methods=["PUT"])
def update_flag_type(flag_type_id):
    """Update an existing flag type"""
    try:
        data = request.get_json()
        name = data.get("name", "").strip()
        color = data.get("color", "").strip()

        if not name:
            return jsonify({"error": "Name is required"}), 400

        if not color.startswith("#") or len(color) != 7:
            return (
                jsonify({"error": "Color must be a valid hex color (e.g., #FF0000)"}),
                400,
            )

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE flag_types 
            SET name = %s, color = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            RETURNING id, name, color, created_at, updated_at
            """,
            (name, color, flag_type_id),
        )
        updated = cur.fetchone()

        if not updated:
            conn.close()
            return jsonify({"error": "Flag type not found"}), 404

        conn.commit()
        conn.close()

        return jsonify(dict(updated))

    except Exception as e:
        print(f"❌ Error updating flag type: {e}")
        return jsonify({"error": str(e)}), 500


@messaging_bp.route("/flag-types/<int:flag_type_id>", methods=["DELETE"])
def delete_flag_type(flag_type_id):
    """Delete a flag type and unflag all contacts using it"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # First, unflag all contacts using this flag type
        cur.execute(
            "UPDATE contacts SET flag_type_id = NULL WHERE flag_type_id = %s",
            (flag_type_id,),
        )
        unflagged_count = cur.rowcount

        # Then delete the flag type
        cur.execute("DELETE FROM flag_types WHERE id = %s", (flag_type_id,))

        if cur.rowcount == 0:
            conn.close()
            return jsonify({"error": "Flag type not found"}), 404

        conn.commit()
        conn.close()

        return jsonify({"status": "deleted", "unflagged_contacts": unflagged_count})

    except Exception as e:
        print(f"❌ Error deleting flag type: {e}")
        return jsonify({"error": str(e)}), 500


@messaging_bp.route("/messages/toggle-pin/<int:message_id>", methods=["POST"])
def toggle_message_pin(message_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Get current pin status
        cur.execute("SELECT pinned FROM messages WHERE id = %s", (message_id,))
        result = cur.fetchone()

        if not result:
            conn.close()
            return jsonify({"error": "Message not found"}), 404

        # Toggle pin status
        new_pin_status = 0 if result["pinned"] else 1
        cur.execute(
            "UPDATE messages SET pinned = %s WHERE id = %s",
            (new_pin_status, message_id),
        )
        conn.commit()
        conn.close()

        return jsonify({"status": "ok", "pinned": bool(new_pin_status)})

    except Exception as e:
        print(f"Error toggling pin: {e}")
        return jsonify({"error": str(e)}), 500


@messaging_bp.route("/messages/pinned/<phone_number>", methods=["GET"])
def get_pinned_messages(phone_number):
    try:
        normalized_phone = normalize_phone_number(phone_number)
        if not normalized_phone:
            return jsonify([])

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM messages
            WHERE phone_number = %s AND pinned = 1
            ORDER BY timestamp DESC
        """,
            (normalized_phone,),
        )

        pinned_messages = []
        for row in cur.fetchall():
            pinned_messages.append(
                {
                    "id": row["id"],
                    "direction": row["direction"],
                    "phone_number": row["phone_number"],
                    "body": row["body"],
                    "timestamp": row["timestamp"],
                    "media_urls": json.loads(row["media_urls"] or "[]"),
                    "pinned": row["pinned"],
                }
            )

        conn.close()
        return jsonify(pinned_messages)

    except Exception as e:
        print(f"Error getting pinned messages: {e}")
        return jsonify([])


@messaging_bp.route("/messages/delete-message/<int:message_id>", methods=["DELETE"])
def delete_message(message_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Check if message exists
        cur.execute("SELECT id FROM messages WHERE id = %s", (message_id,))
        if not cur.fetchone():
            conn.close()
            return jsonify({"error": "Message not found"}), 404

        # Delete the message
        cur.execute("DELETE FROM messages WHERE id = %s", (message_id,))
        conn.commit()
        conn.close()

        return jsonify({"status": "deleted"})

    except Exception as e:
        print(f"Error deleting message: {e}")
        return jsonify({"error": str(e)}), 500


@messaging_bp.route("/messages/reactions/<phone_number>", methods=["GET"])
def get_message_reactions(phone_number):
    try:
        normalized_phone = normalize_phone_number(phone_number)
        if not normalized_phone:
            return jsonify({})

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT reacted_to_message_id, body, timestamp
            FROM messages 
            WHERE phone_number = %s AND is_reaction = 1 AND reacted_to_message_id IS NOT NULL
            ORDER BY timestamp DESC
        """,
            (normalized_phone,),
        )

        reactions = {}
        for row in cur.fetchall():
            message_id = row["reacted_to_message_id"]
            reaction_text = row["body"]

            # Extract reaction type
            reaction_type = "liked"  # default
            if reaction_text.startswith("Loved"):
                reaction_type = "loved"
            elif reaction_text.startswith("Laughed"):
                reaction_type = "laughed"
            elif reaction_text.startswith("Emphasized"):
                reaction_type = "emphasized"
            elif reaction_text.startswith("Questioned"):
                reaction_type = "questioned"
            elif reaction_text.startswith("Disliked"):
                reaction_type = "disliked"

            reactions[message_id] = {
                "type": reaction_type,
                "timestamp": row["timestamp"],
            }

        conn.close()
        return jsonify(reactions)

    except Exception as e:
        print(f"⚠️ Error getting reactions: {e}")
        return jsonify({})


@messaging_bp.route("/messages/test", methods=["GET", "POST"])
def test_route():
    print(f"🧪 Test route hit! Method: {request.method}")
    return "Test route works!", 200
