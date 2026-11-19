#!/usr/bin/env python3
"""
Connect an X (Twitter) account to Tweppy.

Run this on your own computer, open the printed address in a browser, and
approve the app on X. The resulting access and refresh tokens are saved to the
Tweppy database, after which the dashboard can post as that account.

Run it again to connect another account: sign in to X as that account in the
browser first (or switch accounts on X's approval screen). Connecting an
account that is already connected just renews its tokens.

Uses the OAuth 2.0 Authorization Code flow with PKCE (S256). The callback
address must exactly match REDIRECT_URI in .env and the Callback URI configured
for your app in the X developer portal.
"""
import base64
import hashlib
import html
import os
import secrets
from urllib.parse import urlencode, urlparse

import requests
from dotenv import load_dotenv
from flask import Flask, request, session

import rest

load_dotenv()

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://127.0.0.1:8080/callback")

AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
# media.write is needed to attach images and videos to posts.
SCOPES = ["tweet.read", "tweet.write", "users.read", "media.write", "offline.access"]

callback_path = urlparse(REDIRECT_URI).path or "/callback"

app = Flask(__name__)
app.secret_key = secrets.token_bytes(32)


def page(title, body):
    return (f"<!doctype html><meta charset='utf-8'><title>{html.escape(title)}</title>"
            "<body style='font-family:system-ui,sans-serif;max-width:640px;margin:4rem auto;line-height:1.5'>"
            f"<h1>{html.escape(title)}</h1>{body}</body>")


@app.route("/")
def start():
    if not CLIENT_ID or not CLIENT_SECRET:
        return page("Missing credentials",
                    "<p>Set CLIENT_ID and CLIENT_SECRET in your .env file, then restart this script.</p>"), 500

    # PKCE: a random secret verifier, sent to X only as its SHA-256 hash until the code exchange.
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    session["pkce_verifier"] = verifier
    session["oauth_state"] = state

    url = AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    connected = [f"@{a['username']}" for a in rest.list_accounts(fresh=True) if a["username"]]
    already = (f"<p>Already connected: {html.escape(', '.join(connected))}. To add another account, make sure "
               "X is signed in to that account in this browser.</p>") if connected else ""
    return page("Connect an X account",
                "<p>Sign in to X with the account you want Tweppy to post as, then approve the app.</p>"
                + already +
                f"<p><a href='{html.escape(url, quote=True)}'>Authorize with X</a></p>")


@app.route(callback_path)
def callback():
    if request.args.get("error"):
        return page("Authorization cancelled", f"<p>X said: {html.escape(request.args['error'])}</p>"), 400

    expected_state = session.pop("oauth_state", None)
    verifier = session.pop("pkce_verifier", None)
    if not expected_state or not verifier:
        return page("Session expired",
                    "<p>Start again from <a href='/'>the start page</a>. Open it on the same host name "
                    "as your callback address (for example 127.0.0.1, not localhost).</p>"), 400
    if not secrets.compare_digest(request.args.get("state", ""), expected_state):
        return page("State mismatch", "<p>Please <a href='/'>start again</a>.</p>"), 400

    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    response = requests.post(
        TOKEN_URL,
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "code": request.args.get("code", ""),
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
            "client_id": CLIENT_ID,
        },
        timeout=30,
    )
    if response.status_code != 200:
        return page("Could not get tokens", f"<pre>{html.escape(response.text)}</pre>"), 502

    tokens = response.json()
    try:
        account, is_new = rest.connect_account(tokens["access_token"], tokens["refresh_token"])
    except Exception as e:
        return page("Could not identify the account",
                    "<p>X approved the app but would not say which account this is, so nothing was saved. "
                    f"Check that your X API credits are not used up, then try again.</p><pre>{html.escape(str(e))}</pre>"), 502

    if account["username"]:
        verb = "Connected" if is_new else "Renewed the connection for"
        who = f"<p>{verb} <strong>@{html.escape(account['username'])}</strong>.</p>"
    else:
        who = "<p>Tokens saved, but the account lookup failed. Use Re-check X account in the dashboard's Settings.</p>"
    total = len(rest.list_accounts(fresh=True))
    return page("Connected", who + f"<p>{total} account(s) connected. To add another, sign in to X as that "
                "account and <a href='/'>start again</a>. Otherwise you can close this window and stop the script.</p>")


if __name__ == "__main__":
    parsed = urlparse(REDIRECT_URI)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 8080
    print(f"Open http://{host}:{port}/ in your browser to connect an X account.")
    # Bind to loopback only: this helper never needs to be reachable from other machines.
    app.run(host="127.0.0.1", port=port, debug=False)
