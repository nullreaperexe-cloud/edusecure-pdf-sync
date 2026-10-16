"""Secretless, authenticated GitHub Actions -> Supabase Edge Function client.

GitHub Actions issues a short-lived RS256 OIDC JWT. The Supabase function
validates its signature against GitHub's JWKS plus repository, branch, audience,
and event claims before using its server-side service role. The public REST
publishable key is NEVER used to grant write access.
"""
from __future__ import annotations
import base64
import json
import os
import time
from typing import Any
from urllib.parse import urlsplit

import requests
from reliable_http import request as http_request

DEFAULT_URL = "https://xudhjwojgxblbsebkzso.supabase.co"
AUDIENCE = "8apdf-supabase-sync"
_TOKEN = ""
_EXPIRES = 0.0


def endpoint() -> str:
    origin = (os.environ.get("SUPABASE_URL") or DEFAULT_URL).strip().rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or parsed.hostname != "xudhjwojgxblbsebkzso.supabase.co" or parsed.path:
        raise RuntimeError("SUPABASE_URL must target the verified new 8aPDF project")
    return origin + "/functions/v1/edusecure-sync"


def token() -> str:
    global _TOKEN, _EXPIRES
    if _TOKEN and time.time() < _EXPIRES - 65:
        return _TOKEN
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "").strip()
    bearer = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "").strip()
    if not url or not bearer:
        raise RuntimeError("GitHub Actions OIDC unavailable: workflow needs permissions: id-token: write")
    response = http_request(
        "GET", url, params={"audience": AUDIENCE},
        headers={"Authorization": "Bearer " + bearer, "Accept": "application/json"},
        timeout=25,
    )
    try:
        response.raise_for_status()
        value = response.json().get("value", "")
        if not isinstance(value, str) or len(value.split(".")) != 3:
            raise RuntimeError("GitHub Actions did not return a valid OIDC token")
        # Unverified expiry is used *only* for local cache refresh; the function
        # verifies signature, issuer, claims and expiration before every action.
        segment = value.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        claims = json.loads(base64.urlsafe_b64decode(segment))
        exp = int(claims.get("exp", 0))
        if exp <= time.time()+15:
            raise RuntimeError("OIDC JWT expired before request")
        _TOKEN, _EXPIRES = value, float(exp)
        return _TOKEN
    finally:
        response.close()


def call(action: str, **payload: Any) -> dict[str, Any]:
    """Exactly one authenticated Edge request. Use idempotent write actions."""
    url = endpoint()
    response = http_request(
        "POST", url,
        headers={
            "Authorization": "Bearer " + token(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={"action": action, **payload},
        timeout=(8, 45),
    )
    try:
        status = response.status_code
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError("Supabase Edge returned non-JSON HTTP " + str(status))
        if not response.ok or not isinstance(data, dict) or data.get("ok") is not True:
            reason = str(data.get("error", "Unknown error")) if isinstance(data, dict) else "Invalid response"
            raise RuntimeError("Supabase sync "+action+" failed HTTP "+str(status)+": "+reason[:180])
        return data
    finally:
        response.close()


def health() -> None:
    call("health")
    print("Supabase 8aPDF backend authorized through GitHub Actions OIDC.")
