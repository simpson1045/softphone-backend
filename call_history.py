from flask import Blueprint, request, jsonify
import os
import json
from datetime import datetime

call_history_bp = Blueprint("call_history", __name__)

CALL_HISTORY_JSON_PATH = os.getenv("CALL_HISTORY_JSON_PATH", "call_log.json")

@call_history_bp.route("/call-history/api")
def call_history_api():
    if not os.path.exists(CALL_HISTORY_JSON_PATH):
        return jsonify([])
    with open(CALL_HISTORY_JSON_PATH, "r") as f:
        return jsonify(json.load(f))