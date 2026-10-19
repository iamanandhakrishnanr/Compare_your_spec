"""Database helpers for the SOS sign-up / sign-in system.

Each user is stored as: email (PRIMARY KEY), user_name, password_hash,
created_at. Passwords are hashed with werkzeug's generate_password_hash
before they ever touch the database — nothing plaintext is stored.
"""

import os
import sqlite3

from werkzeug.security import check_password_hash, generate_password_hash

# always next to this file (the "storage" folder), whatever folder the server is started from
DATABASE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_data.db")


def get_connection():
    connection = sqlite3.connect(DATABASE_FILE)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    """Create the users table if it doesn't exist yet. Safe to call every run."""
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                user_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.commit()


def email_exists(email):
    with get_connection() as connection:
        row = connection.execute(
            "SELECT 1 FROM users WHERE email = ?", (email,)
        ).fetchone()
        return row is not None


def create_user(name, email, password):
    """Insert a new, OTP-verified user.

    Raises sqlite3.IntegrityError if the email is already taken (belt and
    braces on top of the email_exists() check the caller should already
    have done, since the primary key enforces uniqueness at the DB level).
    """
    password_hash = generate_password_hash(password)
    with get_connection() as connection:
        connection.execute(
            "INSERT INTO users (email, user_name, password_hash) VALUES (?, ?, ?)",
            (email, name, password_hash),
        )
        connection.commit()


def update_password(email, new_password):
    """Set a new password for an existing user. Returns True if a row was updated."""
    password_hash = generate_password_hash(new_password)
    with get_connection() as connection:
        cursor = connection.execute(
            "UPDATE users SET password_hash = ? WHERE email = ?",
            (password_hash, email),
        )
        connection.commit()
        return cursor.rowcount > 0


def verify_login(email, password):
    """Return (True, user_name) if the email/password pair is valid, else (False, None)."""
    with get_connection() as connection:
        row = connection.execute(
            "SELECT user_name, password_hash FROM users WHERE email = ?",
            (email,),
        ).fetchone()

    if row is None:
        return False, None
    if check_password_hash(row["password_hash"], password):
        return True, row["user_name"]
    return False, None