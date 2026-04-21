"""Twilio webhook signature validation.

Public Twilio webhook routes (SMS, voice, status callbacks, voicemail,
recording completion) accept POST requests from Twilio. Without signature
validation, any caller who knows a webhook URL can forge requests: fake
inbound SMS, trigger outbound TwiML, poison voicemail records, fire
status callbacks.

Usage:
    from twilio_security import validate_twilio_request

    @app.route("/messages/incoming", methods=["POST"])
    @validate_twilio_request
    def incoming_sms():
        ...

Behavior:
- Enforcement is on by default. Set TWILIO_VALIDATE_WEBHOOKS=false in
  .env to log warnings but allow requests through (useful for debugging
  a URL-mismatch issue behind Caddy without taking the app offline).
- TWILIO_AUTH_TOKEN is read at request time, so rotating the token
  does not require a code change — only a .env update + restart.
- On failure in enforce mode: returns 403.
- Reconstructs the request URL from X-Forwarded-* headers so signatures
  signed against the public https URL still validate when Flask sees
  the request as http://localhost:10000/... behind Caddy.
"""

import logging
import os
from functools import wraps

from flask import abort, request
from twilio.request_validator import RequestValidator

log = logging.getLogger(__name__)


def _reconstruct_request_url():
    """Rebuild the URL as Twilio saw it, respecting reverse-proxy headers.

    Caddy/Cloudflare forward requests to the Flask app on an internal http
    URL, but Twilio signs the public https URL. RequestValidator must be
    given the URL Twilio saw, not what Flask sees.
    """
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host") or request.host
    path = request.full_path.rstrip("?")
    return f"{scheme}://{host}{path}"


def validate_twilio_request(f):
    """Decorator: reject requests whose X-Twilio-Signature header doesn't match."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        enforce = os.getenv("TWILIO_VALIDATE_WEBHOOKS", "true").lower() != "false"
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")

        if not auth_token:
            log.error("TWILIO_AUTH_TOKEN not set — cannot validate webhook signature")
            if enforce:
                abort(500)
            return f(*args, **kwargs)

        signature = request.headers.get("X-Twilio-Signature", "")
        url = _reconstruct_request_url()
        params = (
            request.form.to_dict()
            if request.method == "POST"
            else request.args.to_dict()
        )

        validator = RequestValidator(auth_token)
        if not validator.validate(url, params, signature):
            # Compute what we think the signature should have been, so the logs
            # show EXACTLY where we diverge from what Twilio sent.
            expected = validator.compute_signature(url, params)
            log.warning(
                "Twilio signature validation FAILED "
                f"url={url!r} remote={request.remote_addr} enforce={enforce} "
                f"received_sig={signature!r} computed_sig={expected!r} "
                f"token_prefix={auth_token[:4]}... params_keys={sorted(params.keys())}"
            )
            if enforce:
                abort(403)

        return f(*args, **kwargs)

    return wrapper
