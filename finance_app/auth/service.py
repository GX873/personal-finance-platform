from __future__ import annotations

import hmac
import secrets
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_app.auth.models import User
from finance_app.config import get_settings
from finance_app.db import get_db

password_hasher = PasswordHasher()
_dummy_hash = password_hasher.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters.")
    return password_hasher.hash(password)


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == username))
    try:
        password_hasher.verify(user.password_hash if user else _dummy_hash, password)
    except (VerificationError, InvalidHashError):
        return None
    if user is None or not user.is_active:
        return None
    if password_hasher.check_needs_rehash(user.password_hash):
        user.password_hash = password_hasher.hash(password)
        db.commit()
    return user


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


async def require_csrf(request: Request) -> None:
    expected = request.session.get("csrf_token")
    supplied: object = request.headers.get("X-CSRF-Token")
    if supplied is None:
        supplied = (await request.form()).get("csrf_token")
    if (
        not isinstance(expected, str)
        or not isinstance(supplied, str)
        or not hmac.compare_digest(expected.encode(), supplied.encode())
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not isinstance(user_id, int):
        return None
    user = db.get(User, user_id)
    fingerprint = request.session.get("auth_fingerprint")
    if (
        user is None
        or not user.is_active
        or not isinstance(fingerprint, str)
        or not hmac.compare_digest(
            fingerprint.encode(), session_fingerprint(user).encode()
        )
    ):
        request.session.clear()
        return None
    return user


def session_fingerprint(user: User) -> str:
    # Bind sessions to the current password without putting its hash in the cookie.
    return hmac.new(
        get_settings().secret_key.encode(),
        user.password_hash.encode(),
        "sha256",
    ).hexdigest()


def require_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> User:
    user = current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user
