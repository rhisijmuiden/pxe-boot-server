"""Users, password hashing and the login session."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request

from . import db

ROLES = ("admin", "operator")  # operator: everything except users, mail, network and updates


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 15, 8, 1
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        test = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                              maxmem=64 * 1024 * 1024, dklen=len(bytes.fromhex(dk)))
        return hmac.compare_digest(test, bytes.fromhex(dk))
    except (ValueError, TypeError):
        return False


def user_count() -> int:
    return db.q1("SELECT COUNT(*) AS n FROM users")["n"]


def create_user(username: str, password: str, *, name: str = "", email: str = "", role: str = "admin",
                lang: str = "") -> int:
    username = username.strip().lower()
    if not username or len(password) < 8 or role not in ROLES:
        raise ValueError("invalid")
    return db.run("INSERT INTO users(username,name,email,password_hash,role,lang,created) VALUES(?,?,?,?,?,?,?)",
                  username, name.strip(), email.strip(), hash_password(password), role, lang, time.time())


def authenticate(username: str, password: str):
    row = db.q1("SELECT * FROM users WHERE username=? OR (email<>'' AND lower(email)=?)",
                username.strip().lower(), username.strip().lower())
    if row and verify_password(password, row["password_hash"]):
        db.run("UPDATE users SET last_login=? WHERE id=?", time.time(), row["id"])
        return row
    # constant-ish time when the user does not exist
    verify_password(password, "scrypt$32768$8$1$00$00")
    return None


def current_user(request: Request):
    uid = request.session.get("uid")
    if not uid:
        return None
    return db.q1("SELECT * FROM users WHERE id=?", uid)


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


def check_csrf(request: Request, token: str | None) -> None:
    if not token or not hmac.compare_digest(token, request.session.get("csrf", "")):
        raise HTTPException(status_code=403, detail="csrf")


class LoginRequired(Exception):
    pass


class OnboardingRequired(Exception):
    """An administrator signed in with the generated password and has not finished the first-run wizard."""


def require_user(request: Request):
    user = current_user(request)
    if user is None:
        raise LoginRequired()
    if (user["role"] == "admin" and request.method == "GET" and request.url.path != "/welcome"
            and not request.url.path.startswith("/api/") and db.get_setting("onboarding")):
        raise OnboardingRequired()
    return user


def require_admin(request: Request):
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


def create_reset_token(user_id: int) -> str:
    tok = secrets.token_urlsafe(32)
    db.run("DELETE FROM password_resets WHERE expires < ?", time.time())
    db.run("INSERT INTO password_resets(token,user_id,expires) VALUES(?,?,?)", tok, user_id, time.time() + 3600)
    return tok


def use_reset_token(tok: str, new_password: str) -> bool:
    row = db.q1("SELECT * FROM password_resets WHERE token=? AND expires > ?", tok, time.time())
    if not row or len(new_password) < 8:
        return False
    db.run("UPDATE users SET password_hash=? WHERE id=?", hash_password(new_password), row["user_id"])
    db.run("DELETE FROM password_resets WHERE token=?", tok)
    return True
