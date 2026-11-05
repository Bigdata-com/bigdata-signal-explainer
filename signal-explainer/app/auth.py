"""Sign-in with the user's own Bigdata API key.

The web UI keeps the key encrypted (Fernet) in an HttpOnly, SameSite=Strict cookie that browser scripts cannot read.
API callers (the Claude skill, a ChatGPT action) send it as `Authorization: Bearer <key>` or `X-API-KEY`. The server
never stores the key: a user is identified by a short fingerprint of it, and Bigdata.com calls are made with it, so
usage lands on the user's own account.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Request, Response

from .config import get_settings

COOKIE = "se_key"
_s = get_settings()
_fernet = Fernet((_s.APP_SECRET or Fernet.generate_key().decode()).encode())
# Keys checked against Bigdata.com recently, so API calls don't re-validate on every request.
_verified: dict[str, float] = {}
VERIFY_TTL = 15 * 60


@dataclass(frozen=True)
class User:
    id: str
    api_key: str
    name: str | None = None

    @property
    def label(self) -> str:
        return self.name or f"Key …{self.id[-4:]}"


class LoginRequired(Exception):
    """Raised by web routes when there is no valid session; handled as a redirect to /login."""


def fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def set_key_cookie(response: Response, api_key: str) -> None:
    response.set_cookie(COOKIE, _fernet.encrypt(api_key.encode()).decode(), max_age=_s.SESSION_HOURS * 3600,
                        httponly=True, secure=_s.COOKIE_SECURE, samesite="strict", path="/")


def clear_key_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/")


def cookie_key(request: Request) -> str | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    try:
        return _fernet.decrypt(token.encode(), ttl=_s.SESSION_HOURS * 3600).decode()
    except (InvalidToken, ValueError):
        return None


def header_key(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return (request.headers.get("x-api-key") or "").strip() or None


def is_verified(api_key: str) -> bool:
    return _verified.get(fingerprint(api_key), 0) > time.time() - VERIFY_TTL


def mark_verified(api_key: str) -> None:
    _verified[fingerprint(api_key)] = time.time()
