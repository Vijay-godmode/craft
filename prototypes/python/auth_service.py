"""Small, explicit account service for CareerCraft.

The web layer owns sessions and CSRF policy.  This module owns validation and
password hashing so account handling is not spread across unrelated routes.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3
from typing import Any

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class AccountValidationError(ValueError):
    """A user-facing account validation failure."""


@dataclass(frozen=True)
class CareerUser(UserMixin):
    """The minimal authenticated-user object required by Flask-Login."""

    id: int
    email: str
    display_name: str
    role: str = "user"
    is_active_flag: bool = True

    @property
    def is_active(self) -> bool:
        return self.is_active_flag

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "display_name": self.display_name,
            "role": self.role,
        }


def normalise_email(value: Any) -> str:
    return str(value or "").strip().casefold()[:254]


def normalise_display_name(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:80]


def validate_registration(email: Any, password: Any, display_name: Any) -> tuple[str, str, str]:
    clean_email = normalise_email(email)
    clean_name = normalise_display_name(display_name)
    raw_password = str(password or "")
    if not EMAIL_PATTERN.fullmatch(clean_email):
        raise AccountValidationError("Enter a valid email address.")
    if len(clean_name) < 2:
        raise AccountValidationError("Enter the name you want CareerCraft to use.")
    if len(raw_password) < 10:
        raise AccountValidationError("Use at least 10 characters for your password.")
    if not re.search(r"[A-Za-z]", raw_password) or not re.search(r"\d", raw_password):
        raise AccountValidationError("Use at least one letter and one number in your password.")
    return clean_email, raw_password, clean_name


def row_to_user(row: sqlite3.Row | None) -> CareerUser | None:
    if not row:
        return None
    return CareerUser(
        id=int(row["id"]),
        email=str(row["email"]),
        display_name=str(row["display_name"]),
        role=str(row["role"] or "user"),
        is_active_flag=bool(row["is_active"]),
    )


def load_user(connection: sqlite3.Connection, user_id: str | int) -> CareerUser | None:
    try:
        numeric_id = int(user_id)
    except (TypeError, ValueError):
        return None
    row = connection.execute(
        "SELECT id, email, display_name, role, is_active FROM users WHERE id = ?", (numeric_id,)
    ).fetchone()
    return row_to_user(row)


def create_user(
    connection: sqlite3.Connection,
    *,
    email: Any,
    password: Any,
    display_name: Any,
    created_at: str,
) -> CareerUser:
    clean_email, raw_password, clean_name = validate_registration(email, password, display_name)
    try:
        cursor = connection.execute(
            """
            INSERT INTO users (email, display_name, password_hash, role, is_active, created_at, updated_at)
            VALUES (?, ?, ?, 'user', 1, ?, ?)
            """,
            (clean_email, clean_name, generate_password_hash(raw_password), created_at, created_at),
        )
    except sqlite3.IntegrityError as exc:
        raise AccountValidationError("An account already exists for that email.") from exc
    return CareerUser(id=int(cursor.lastrowid), email=clean_email, display_name=clean_name)


def authenticate_user(connection: sqlite3.Connection, email: Any, password: Any) -> CareerUser | None:
    clean_email = normalise_email(email)
    row = connection.execute(
        "SELECT id, email, display_name, password_hash, role, is_active FROM users WHERE email = ?", (clean_email,)
    ).fetchone()
    if not row or not bool(row["is_active"]) or not check_password_hash(str(row["password_hash"]), str(password or "")):
        return None
    return row_to_user(row)
