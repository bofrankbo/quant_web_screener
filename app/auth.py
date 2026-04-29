import base64
import hashlib
import hmac
import time
import secrets
from urllib.parse import urlencode

import requests
from fastapi import Request

from app.config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI
from app.db import get_user_by_id, upsert_google_user

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_SCOPE = "openid email profile"
OAUTH_STATE_TTL_SECONDS = 600


def auth_enabled() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def _state_signing_key() -> bytes:
    return GOOGLE_CLIENT_SECRET.encode("utf-8") or GOOGLE_CLIENT_ID.encode("utf-8")


def make_oauth_state() -> str:
    nonce = secrets.token_urlsafe(16)
    issued_at = str(int(time.time()))
    payload = f"{nonce}.{issued_at}".encode("utf-8")
    sig = hmac.new(_state_signing_key(), payload, hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
    return f"{nonce}.{issued_at}.{token}"


def verify_oauth_state(state: str, max_age: int = OAUTH_STATE_TTL_SECONDS) -> bool:
    try:
        nonce, issued_at_str, token = state.split(".", 2)
        issued_at = int(issued_at_str)
    except (ValueError, TypeError):
        return False

    if issued_at > int(time.time()) or int(time.time()) - issued_at > max_age:
        return False

    payload = f"{nonce}.{issued_at_str}".encode("utf-8")
    expected_sig = hmac.new(_state_signing_key(), payload, hashlib.sha256).digest()
    expected_token = base64.urlsafe_b64encode(expected_sig).decode("ascii").rstrip("=")
    return hmac.compare_digest(expected_token, token)


def build_google_login_url(request: Request) -> tuple[str, str, str]:
    state = make_oauth_state()
    redirect_uri = GOOGLE_REDIRECT_URI or str(request.url_for("auth_google_callback"))
    params = {
        "response_type": "code",
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": GOOGLE_SCOPE,
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
    return url, state, redirect_uri


def exchange_code_for_token(code: str, redirect_uri: str) -> dict:
    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_google_userinfo(access_token: str) -> dict:
    resp = requests.get(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def set_session_user(request: Request, user_row: dict) -> None:
    request.session["user"] = {
        "id": user_row["id"],
        "google_sub": user_row["google_sub"],
        "email": user_row.get("email"),
        "name": user_row.get("name"),
        "picture": user_row.get("picture"),
    }


def get_session_user(request: Request) -> dict | None:
    user = request.session.get("user")
    if not user:
        return None
    db_user = get_user_by_id(int(user["id"]))
    if db_user is None:
        request.session.pop("user", None)
        return None
    return db_user


def login_user_from_google(request: Request, google_profile: dict) -> dict:
    user_row = upsert_google_user(
        google_sub=google_profile["sub"],
        email=google_profile.get("email"),
        name=google_profile.get("name"),
        picture=google_profile.get("picture"),
    )
    set_session_user(request, user_row)
    return user_row
