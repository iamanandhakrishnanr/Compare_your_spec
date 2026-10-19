"""Sign-up / sign-in / OTP routes for accounts.html, as a Flask Blueprint.

models/groq_bot.py registers it and is now the one server for the whole site:
Run:   pip install flask
       python models/groq_bot.py
Open:  http://127.0.0.1:5000   (not the .html files directly - the pages call /api/*)
"""

import os
import re
import secrets
import sqlite3
import sys
import time

from flask import Blueprint, current_app, jsonify, request, send_from_directory, session

# db_utils.py lives in the sibling "storage" folder (../storage relative to this file)
STORAGE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "storage"))
sys.path.insert(0, STORAGE_DIR)

from db_utils import create_user, email_exists, init_db, update_password, verify_login
from otp_utils import generate_otp, send_otp

OTP_TTL = 5 * 60        # seconds; matches the "expires in 5 minutes" line in the email
RESEND_COOLDOWN = 30    # seconds between codes for the same address
MAX_ATTEMPTS = 5        # wrong guesses before the code is thrown away

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")  # one plain address: no commas, <>, quotes

# accounts.html lives in the sibling "screens" folder (../screens relative to this file)
SCREENS_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "screens"))

auth = Blueprint("auth", __name__)
pending = {}  # email -> {"otp", "expires", "sent_at", "attempts", "signup", "reset", "verified"}
init_db()


def _email_from_request():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    return data, email


@auth.get("/accounts")
@auth.get("/accounts.html")
def accounts_page():
    return send_from_directory(SCREENS_DIR, "accounts.html")


@auth.post("/api/send-otp")
def api_send_otp():
    data, email = _email_from_request()
    if not EMAIL_RE.match(email):
        return jsonify(error="Enter a valid email address"), 400

    # sign-up sends name + password too; the forgot-password flow sends only the email
    signup = None
    if "name" in data or "password" in data:
        name = str(data.get("name", "")).strip()
        password = str(data.get("password", ""))
        if not name or not password:
            return jsonify(error="Enter your name and a password"), 400
        if email_exists(email):
            return jsonify(error="Email already registered, sign in instead"), 409
        signup = {"name": name, "password": password}

    # forgot-password flow: only send a code to an address that has an account
    reset = bool(data.get("reset"))
    if reset and not email_exists(email):
        return jsonify(error="No account with this email"), 404

    now = time.time()
    entry = pending.get(email)
    if entry and now - entry["sent_at"] < RESEND_COOLDOWN:
        wait = int(RESEND_COOLDOWN - (now - entry["sent_at"])) + 1
        return jsonify(error=f"Wait {wait}s before requesting another code"), 429

    otp = generate_otp()
    try:
        send_otp(email, otp)
    except Exception as exc:  # SMTP auth/network failure
        current_app.logger.error("send_otp failed: %s", exc)
        return jsonify(error="Could not send the email"), 502

    pending[email] = {"otp": otp, "expires": now + OTP_TTL, "sent_at": now, "attempts": 0, "signup": signup,
                     "reset": reset, "verified": False}
    return jsonify(ok=True)


@auth.post("/api/verify-otp")
def api_verify_otp():
    data, email = _email_from_request()
    code = str(data.get("code", "")).strip()
    entry = pending.get(email)

    if not entry or time.time() > entry["expires"]:
        pending.pop(email, None)
        return jsonify(error="Code expired, request a new one"), 400

    entry["attempts"] += 1
    if entry["attempts"] > MAX_ATTEMPTS:
        pending.pop(email, None)
        return jsonify(error="Too many attempts, request a new code"), 429

    if not secrets.compare_digest(entry["otp"], code):
        return jsonify(error="Wrong code"), 400

    if entry.get("reset"):  # forgot password: keep the verified entry so /api/reset-password can use it
        entry["verified"] = True
        entry["expires"] = time.time() + OTP_TTL
        return jsonify(ok=True)

    pending.pop(email, None)
    signup = entry.get("signup")
    if signup:  # OTP verified: now save the new user
        try:
            create_user(signup["name"], email, signup["password"])
        except sqlite3.IntegrityError:  # someone registered this email in the meantime
            return jsonify(error="Email already registered, sign in instead"), 409
        session.clear()  # a verified sign-up is signed in: this cookie is what unlocks main.html
        session["email"] = email
        session["name"] = signup["name"]
    return jsonify(ok=True)


@auth.post("/api/reset-password")
def api_reset_password():
    data, email = _email_from_request()
    password = str(data.get("password", ""))
    entry = pending.get(email)

    if not entry or not entry.get("verified") or time.time() > entry["expires"]:
        return jsonify(error="Verify your email first"), 400
    if not password:
        return jsonify(error="Enter a new password"), 400

    pending.pop(email, None)
    if not update_password(email, password):  # overwrites the old password hash
        return jsonify(error="No account with this email"), 404
    return jsonify(ok=True)


@auth.post("/api/sign-in")
def api_sign_in():
    data, email = _email_from_request()
    password = str(data.get("password", ""))
    if not EMAIL_RE.match(email) or not password:
        return jsonify(error="Enter your email and password"), 400

    ok, user_name = verify_login(email, password)
    if not ok:
        return jsonify(error="Wrong email or password"), 401

    session.clear()  # this cookie is what unlocks main.html
    session["email"] = email
    session["name"] = user_name
    return jsonify(ok=True)


if __name__ == "__main__":
    sys.exit("server.py is now part of the main app. Run models/groq_bot.py and open http://127.0.0.1:5000")