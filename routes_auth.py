#!/usr/bin/env python3
"""Google OAuth login + session-based workspace dashboard. Session
resolution goes through identity.resolve_session() (Task 2) — this file
holds routing/cookie plumbing only, no credential logic of its own."""
import html
import secrets as _secrets_mod

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ledger_engine import DATA_DIR

router = APIRouter()


@router.get("/login")
def login():
    state = _secrets_mod.token_urlsafe(16)
    from oauth_google import google_auth_url
    resp = RedirectResponse(google_auth_url(state))
    resp.set_cookie("al_oauth_state", state, httponly=True, max_age=600)
    return resp

@router.get("/auth/google/callback")
def google_callback(code: str, state: str, request: Request):
    if request.cookies.get("al_oauth_state") != state:
        raise HTTPException(400, "invalid oauth state")
    from oauth_google import exchange_code
    import workspace_engine
    from session_auth import sign_session
    userinfo = exchange_code(code)
    # create_workspace is idempotent per google_sub (Task 1): a FIRST login
    # mints the workspace and returns its raw_key (shown once, below); a
    # RETURNING login gets the same workspace_id back and raw_key is None.
    # None is the correct answer there — only the key's hash is stored, so
    # there is nothing to re-show, and minting a fresh one would silently
    # break the key the user is already using (which is exactly what this
    # route used to do on every single login).
    workspace_id, raw_key = workspace_engine.create_workspace(
        owner_email=userinfo["email"], google_sub=userinfo["google_sub"])
    resp = RedirectResponse("/dashboard")
    resp.set_cookie("al_session", sign_session(workspace_id), httponly=True, max_age=2592000)
    # One-time key reveal: the raw key only exists right here, on a first
    # login. Carry it to the dashboard's first load via a short-lived cookie
    # the dashboard route reads-and-deletes, so a page refresh never shows
    # it twice.
    if raw_key:
        resp.set_cookie("al_key_reveal", raw_key, httponly=True, max_age=30)
    resp.delete_cookie("al_oauth_state")
    return resp

@router.get("/logout")
def logout():
    resp = RedirectResponse("/")
    resp.delete_cookie("al_session")
    return resp

@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request):
    import identity
    import workspace_engine
    workspace_id = identity.resolve_session(request.cookies.get("al_session", ""))
    if not workspace_id:
        return RedirectResponse("/login")
    ws = workspace_engine.get_workspace(workspace_id)
    reveal_key = request.cookies.get("al_key_reveal")
    key_html = (
        f'<p style="color:#d4af37"><b>Your workspace_key (save this now — shown once):</b><br>'
        f'<code>{html.escape(reveal_key)}</code></p>'
        if reveal_key else
        '<p>Your workspace_key was shown once at signup. Key recovery is not '
        'available yet in this version — logging in again will not reissue it.</p>'
    )
    agent_rows = "".join(
        f'<li><a href="/v1/report/{html.escape(d.name)}/html">{html.escape(d.name)}</a></li>'
        for d in (DATA_DIR / "agents").glob("*")
        if (d / "workspace_id.txt").exists()
        and (d / "workspace_id.txt").read_text().strip() == workspace_id
    ) or "<li>No agents claimed yet.</li>"
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>AgentLedger — Your Workspace</title></head><body style="font-family:sans-serif;padding:24px">
<h1>Your workspace</h1>
<p>Plan: <b>{html.escape(ws['plan'])}</b></p>
{key_html}
<h3>Your agents</h3><ul>{agent_rows}</ul>
<p><a href="/logout">Log out</a></p>
</body></html>"""
    resp = HTMLResponse(page)
    if reveal_key:
        resp.delete_cookie("al_key_reveal")
    return resp
