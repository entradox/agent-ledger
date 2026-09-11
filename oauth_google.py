#!/usr/bin/env python3
"""Google OAuth — plain REST calls, no SDK. Confirm the exact token/userinfo
endpoint shapes against Google's current OAuth2 docs at implementation time
(https://developers.google.com/identity/protocols/oauth2/web-server) —
these are the stable, long-standing endpoints but must be verified live,
not assumed from memory, per this project's no-guess-presented-as-proof
standard."""
import json
import os
import urllib.parse
import urllib.request

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"


def google_auth_url(state: str) -> str:
    client_id = os.environ["GOOGLE_OAUTH_CLIENT_ID"]
    redirect_uri = os.environ.get(
        "GOOGLE_OAUTH_REDIRECT_URI",
        "https://agent-ledger-production-0ff8.up.railway.app/auth/google/callback")
    params = {
        "client_id": client_id, "redirect_uri": redirect_uri,
        "response_type": "code", "scope": "openid email",
        "state": state,
    }
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str) -> dict:
    client_id = os.environ["GOOGLE_OAUTH_CLIENT_ID"]
    client_secret = os.environ["GOOGLE_OAUTH_CLIENT_SECRET"]
    redirect_uri = os.environ.get(
        "GOOGLE_OAUTH_REDIRECT_URI",
        "https://agent-ledger-production-0ff8.up.railway.app/auth/google/callback")
    body = urllib.parse.urlencode({
        "code": code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_ENDPOINT, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        token_data = json.loads(resp.read())
    userinfo_req = urllib.request.Request(
        USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {token_data['access_token']}"})
    with urllib.request.urlopen(userinfo_req, timeout=10) as resp:
        userinfo = json.loads(resp.read())
    return {"google_sub": userinfo["sub"], "email": userinfo.get("email", "")}
