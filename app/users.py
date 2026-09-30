"""CarFinder user accounts.

Accounts are stored in ``data/users.json`` (gitignored, never published).
Passwords are salted and hashed with PBKDF2-SHA256 from the Python standard
library; the plain password is never stored.

Roles:
    admin  - manages household settings (dealer groups, colours) and users
    user   - manages their own car searches, postcode and password
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parent.parent
USERS_PATH = APP_ROOT / "data" / "users.json"

PBKDF2_ITERATIONS = 240_000
MIN_PASSWORD_LENGTH = 6
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,30}$")
ROLES = ("admin", "user")


def normalise_username(value: str | None) -> str:
    return (value or "").strip().lower()


def _hash_password(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations).hex()


def _read(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    users = data.get("users") if isinstance(data, dict) else None
    return [u for u in users or [] if isinstance(u, dict) and u.get("username")]


def _write(users: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"users": users}, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def public_user(user: dict[str, Any]) -> dict[str, str]:
    """User details that are safe to keep in the session / show in the UI."""
    return {
        "username": str(user["username"]),
        "display_name": str(user.get("display_name") or user["username"]),
        "role": str(user.get("role") or "user"),
    }


def list_users(path: Path | None = None) -> list[dict[str, str]]:
    return [public_user(u) for u in _read(path or USERS_PATH)]


def has_users(path: Path | None = None) -> bool:
    return bool(_read(path or USERS_PATH))


def get_user(username: str, path: Path | None = None) -> dict[str, str] | None:
    wanted = normalise_username(username)
    for user in _read(path or USERS_PATH):
        if user["username"] == wanted:
            return public_user(user)
    return None


def validate_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")


def create_user(
    username: str,
    password: str,
    display_name: str = "",
    role: str = "user",
    path: Path | None = None,
) -> dict[str, str]:
    path = path or USERS_PATH
    name = normalise_username(username)
    if not USERNAME_PATTERN.match(name):
        raise ValueError("Username must be 2-31 characters: letters, numbers, dot, dash or underscore.")
    if role not in ROLES:
        raise ValueError("Unknown role.")
    validate_password(password)
    users = _read(path)
    if any(u["username"] == name for u in users):
        raise ValueError(f"User '{name}' already exists.")
    salt = secrets.token_bytes(16)
    user = {
        "username": name,
        "display_name": (display_name or "").strip() or name.capitalize(),
        "role": role,
        "salt": salt.hex(),
        "iterations": PBKDF2_ITERATIONS,
        "password_hash": _hash_password(password, salt),
    }
    users.append(user)
    _write(users, path)
    return public_user(user)


def authenticate(username: str, password: str, path: Path | None = None) -> dict[str, str] | None:
    name = normalise_username(username)
    for user in _read(path or USERS_PATH):
        if user["username"] != name:
            continue
        try:
            salt = bytes.fromhex(str(user.get("salt") or ""))
            iterations = int(user.get("iterations") or PBKDF2_ITERATIONS)
        except (TypeError, ValueError):
            return None
        candidate = _hash_password(password or "", salt, iterations)
        if hmac.compare_digest(candidate, str(user.get("password_hash") or "")):
            return public_user(user)
        return None
    # Spend similar time for unknown users so names can't be probed by timing.
    _hash_password(password or "", b"carfinder-dummy-salt")
    return None


def set_password(username: str, password: str, path: Path | None = None) -> None:
    path = path or USERS_PATH
    validate_password(password)
    name = normalise_username(username)
    users = _read(path)
    for user in users:
        if user["username"] == name:
            salt = secrets.token_bytes(16)
            user.update(salt=salt.hex(), iterations=PBKDF2_ITERATIONS, password_hash=_hash_password(password, salt))
            _write(users, path)
            return
    raise ValueError(f"User '{name}' not found.")


def update_user(username: str, display_name: str | None = None, role: str | None = None, path: Path | None = None) -> None:
    path = path or USERS_PATH
    name = normalise_username(username)
    users = _read(path)
    target = next((u for u in users if u["username"] == name), None)
    if target is None:
        raise ValueError(f"User '{name}' not found.")
    if role is not None:
        if role not in ROLES:
            raise ValueError("Unknown role.")
        if target.get("role") == "admin" and role != "admin" and sum(1 for u in users if u.get("role") == "admin") <= 1:
            raise ValueError("CarFinder needs at least one admin.")
        target["role"] = role
    if display_name is not None and display_name.strip():
        target["display_name"] = display_name.strip()
    _write(users, path)


def delete_user(username: str, path: Path | None = None) -> None:
    path = path or USERS_PATH
    name = normalise_username(username)
    users = _read(path)
    target = next((u for u in users if u["username"] == name), None)
    if target is None:
        return
    if target.get("role") == "admin" and sum(1 for u in users if u.get("role") == "admin") <= 1:
        raise ValueError("CarFinder needs at least one admin.")
    _write([u for u in users if u["username"] != name], path)
