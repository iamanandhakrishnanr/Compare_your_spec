"""OTP generation and email delivery for the SOS sign-up flow.

SMTP credentials are read from the .env file in the main project folder
(the parent of user_data_verification), e.g.:

    SENDER_EMAIL=your_address@gmail.com
    APP_PASSWORD=your app password
"""

import html
import os
import secrets
import smtplib
from email.message import EmailMessage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# .env lives in the main folder, one level above user_data_verification
ENV_PATH = os.path.normpath(os.path.join(BASE_DIR, "..", ".env"))


def _load_env(path):
    """Minimal .env parser (KEY=VALUE, optional quotes, # comments). No extra packages needed."""
    values = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                if key.startswith("export "):
                    key = key[len("export "):].strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                values[key] = val
    except OSError:
        pass
    return values


_env = _load_env(ENV_PATH)
# Real environment variables take priority over the .env file
SENDER_EMAIL = os.environ.get("SENDER_EMAIL") or _env.get("SENDER_EMAIL")
APP_PASSWORD = os.environ.get("APP_PASSWORD") or _env.get("APP_PASSWORD")

if not SENDER_EMAIL or not APP_PASSWORD:
    raise RuntimeError(
        f"SENDER_EMAIL / APP_PASSWORD not found. Add them to {ENV_PATH}"
    )

EXPIRY_MINUTES = 5  # keep in step with OTP_TTL in server.py
# mail.html lives in the sibling "screens" folder (../screens relative to this file)
TEMPLATE_PATH = os.path.normpath(os.path.join(BASE_DIR, "..", "screens", "mail.html"))


def _render_html(recipient_email, otp):
    """Fill mail.html with the OTP. Returns None if the template file is missing."""
    try:
        with open(TEMPLATE_PATH, encoding="utf-8") as f:
            page = f.read()
    except OSError:
        return None
    values = {
        "{{EMAIL}}": html.escape(recipient_email),
        "{{OTP}}": otp,
        "{{EXPIRY_MINUTES}}": str(EXPIRY_MINUTES),
        "{{EXPIRY_TIMECODE}}": f"{EXPIRY_MINUTES:02d}:00",
    }
    for i, digit in enumerate(otp, 1):
        values[f"{{{{D{i}}}}}"] = digit
    for key, val in values.items():
        page = page.replace(key, val)
    return page


def generate_otp():
    """Return a random 6-digit OTP as a string (always 6 digits, no leading-zero loss)."""
    return str(secrets.randbelow(900000) + 100000)


def send_otp(recipient_email, otp):
    """Email the given OTP to recipient_email. Raises on SMTP failure."""
    msg = EmailMessage()
    msg["Subject"] = "Your SOS verification code"
    msg["From"] = SENDER_EMAIL
    msg["To"] = recipient_email
    msg.set_content(f"Your OTP code is: {otp}\n\nThis code expires in {EXPIRY_MINUTES} minutes.")
    page = _render_html(recipient_email, otp)
    if page:  # HTML version for mail apps; plain text stays as the fallback
        msg.add_alternative(page, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(SENDER_EMAIL, APP_PASSWORD)
        smtp.send_message(msg)