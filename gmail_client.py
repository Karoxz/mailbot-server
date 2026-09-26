# =============================================================
# gmail_client.py — server-side module
#
# Everything that actually talks to the Gmail API using a per-license
# credential stored via gmail_store.py. This module owns
# authentication/refresh; poller.py (Phase C) owns the actual
# poll-and-process loop and imports build_service()/probe helpers from
# here.
#
# authenticate() below is gmail_store-backed instead of file-backed —
# ported from the desktop's authenticate_gmail() (main copy.py
# ~548-566), same refresh trigger (creds.expired and creds.refresh_token)
# and same "write the refreshed token back" behavior, just to SQLite
# instead of a local token.json file.
# =============================================================

import json
import os
import string
from random import SystemRandom
from typing import Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import gmail_store

# Same scopes the desktop's token was authorized with — an uploaded
# token.json must already carry at least these for anything useful to
# work, since a token's granted scopes are fixed at consent time and
# can't be widened by asking again server-side.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
]

# Gmail's fixed system label IDs — a label NOT in this set (and not a
# CATEGORY_* one) is a real custom label a dispatcher applied (Bid,
# Finished Loads, RC, etc.). Ported verbatim from the desktop's _SYSIDS
# (main copy.py) — used by poller.py's thread-label guard to recognize
# "this thread has already been handled, don't reprocess it."
_SYSTEM_LABEL_IDS = frozenset({
    "INBOX", "UNREAD", "SENT", "IMPORTANT", "STARRED", "TRASH", "SPAM", "DRAFT",
    "CATEGORY_FORUMS", "CATEGORY_UPDATES", "CATEGORY_PROMOTIONS",
    "CATEGORY_SOCIAL", "CATEGORY_PERSONAL",
})


class GmailAuthError(Exception):
    """Raised when a license has no usable Gmail credential — no token
    stored at all, or a stored token that failed to refresh (revoked,
    expired refresh_token, etc.)."""


def _credentials_from_json(token_json: str) -> Credentials:
    info = json.loads(token_json)
    return Credentials.from_authorized_user_info(info, SCOPES)


def get_credentials(license_key: str) -> Credentials:
    """Load the stored token for this license, refreshing (and
    persisting the refresh back) if needed. Raises GmailAuthError if
    there's no token stored, the stored JSON is unusable, or a refresh
    is needed but fails."""
    token_json = gmail_store.get_token(license_key)
    if not token_json:
        raise GmailAuthError("No Gmail token connected for this license.")

    try:
        creds = _credentials_from_json(token_json)
    except Exception as e:
        raise GmailAuthError(f"Stored token is unusable: {e}")

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                # Mirrors the desktop's fallback comment: a failed silent
                # refresh there falls through to a fresh interactive
                # consent flow, which has no server-side equivalent — a
                # failed refresh here is a hard stop, surfaced to the
                # caller (poller skips this license; Settings UI should
                # show "reconnect needed").
                gmail_store.save_token(license_key, token_json,
                                        status=f"refresh_failed: {e}")
                raise GmailAuthError(f"Token refresh failed — reconnect needed: {e}")
            # Refresh succeeded — persist the rotated token/expiry back,
            # exactly like the desktop rewrites token.json after a
            # silent refresh.
            gmail_store.save_token(license_key, creds.to_json(), status="connected")
        else:
            raise GmailAuthError("Stored token is invalid and has no refresh_token — reconnect needed.")

    return creds


def build_service(license_key: str):
    creds = get_credentials(license_key)
    return build("gmail", "v1", credentials=creds, cache_discovery=False,
                 static_discovery=False)


def get_label_map(service) -> dict:
    """{label_id: label_name} — ported verbatim from the desktop's
    get_label_map()."""
    resp = service.users().labels().list(userId="me").execute()
    return {lbl["id"]: lbl["name"] for lbl in resp.get("labels", [])}


def get_thread_info(service, thread_id: str, label_map: dict) -> tuple:
    """(label_names, subject) — every non-system label name anywhere in
    this thread plus the thread's first non-empty subject. Ported
    verbatim from the desktop's _get_thread_info() (2026-09-26 — the
    subject is what the labeled-thread Telegram ping extracts state
    codes from)."""
    try:
        thread = service.users().threads().get(
            userId="me", id=thread_id, format="metadata",
            metadataHeaders=["Subject"],
        ).execute()
        found = set()
        subject = ""
        for msg in thread.get("messages", []):
            for lid in msg.get("labelIds", []):
                if lid not in _SYSTEM_LABEL_IDS and not lid.startswith("CATEGORY_"):
                    found.add(lid)
            if not subject:
                for h in msg.get("payload", {}).get("headers", []):
                    if h.get("name", "").lower() == "subject":
                        v = h.get("value", "").strip()
                        if v:
                            subject = v
                            break
        return [label_map.get(lid, lid) for lid in found], subject
    except Exception:
        return [], ""


def get_thread_label_names(service, thread_id: str, label_map: dict) -> list:
    names, _ = get_thread_info(service, thread_id, label_map)
    return names


def has_custom_labels(label_ids) -> bool:
    """Same check as parser_core._has_custom_labels() — kept here too
    (not imported from there) so gmail_client has no dependency on
    parser_core; poller.py is the only place that needs both."""
    return any(lid not in _SYSTEM_LABEL_IDS and not lid.startswith("CATEGORY_")
               for lid in label_ids)


def mark_as_read(service, msg_id: str):
    service.users().messages().modify(
        userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
    ).execute()


def validate_and_probe(token_json: str) -> dict:
    """Used only by the upload endpoint: confirm pasted token.json
    content actually works before anything gets saved. Returns
    {"ok": True, "email": ...} or {"ok": False, "error": ...} — never
    raises, so the endpoint can always turn this into a clean HTTP
    response."""
    try:
        creds = _credentials_from_json(token_json)
    except Exception as e:
        return {"ok": False, "error": f"Couldn't parse token.json content: {e}"}

    try:
        if not creds.valid and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif not creds.valid:
            return {"ok": False, "error": "Token is invalid and has no refresh_token."}
    except Exception as e:
        return {"ok": False, "error": f"Token refresh failed (likely revoked/expired): {e}"}

    try:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False,
                        static_discovery=False)
        profile = service.users().getProfile(userId="me").execute()
        email = profile.get("emailAddress", "")
    except HttpError as e:
        return {"ok": False, "error": f"Gmail API rejected this token: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"Couldn't verify this token against Gmail: {e}"}

    # creds.to_json() carries any rotation from the refresh above (if one
    # happened) — the caller saves this, not the original pasted text,
    # so what's stored is always the freshest version.
    return {"ok": True, "email": email, "token_json": creds.to_json()}


# ── Real "Sign in with Google" OAuth flow, added 2026-09-25 ────────────
# The existing token-upload path (validate_and_probe, above) requires a
# dispatcher to already have an authorized token.json sitting next to
# the desktop app and to paste its raw content — fine as a one-time
# bootstrap for the account this project was built against, but not
# something a real customer of this product should ever have to do.
# This is the actual self-serve replacement: a "Sign in with Google"
# button that redirects to Google's own consent screen and comes back
# with a token, same as any normal web app's Google login.
#
# Needs its OWN OAuth client — client/credentials.json is a "installed"
# (desktop) app client (redirect_uris: ["http://localhost"]), which
# Google only allows to complete via a local loopback server on the
# SAME machine running the flow. That's what the desktop app itself
# uses (authenticate_gmail()'s InstalledAppFlow.run_local_server()) and
# it cannot serve a remote browser redirecting back to OUR server. A
# "Web application" type OAuth client (its own client_id/client_secret,
# an explicit HTTPS redirect URI Google is told to allow in advance) is
# required for this — see GOOGLE_OAUTH_CLIENT_ID/SECRET below.
def _oauth_client_config() -> Optional[dict]:
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def oauth_configured() -> bool:
    return _oauth_client_config() is not None


_PKCE_CHARS = string.ascii_letters + string.digits + "-._~"


def generate_code_verifier() -> str:
    """RFC 7636 PKCE code_verifier — same length/alphabet
    google_auth_oauthlib's Flow generates internally (see build_oauth_flow's
    docstring for why this needs to be generated explicitly here rather
    than left to the library's own autogeneration)."""
    rnd = SystemRandom()
    return "".join(rnd.choice(_PKCE_CHARS) for _ in range(128))


def build_oauth_flow(redirect_uri: str, code_verifier: Optional[str] = None) -> Optional[Flow]:
    """code_verifier: pass the SAME value on both ends of the flow —
    /oauth/start generates one (generate_code_verifier()) and must reuse
    it when building /oauth/callback's Flow too. These are two
    independent HTTP requests (no server-side session, may even land on
    different uvicorn workers), so left to the library's own
    autogenerate_code_verifier default, each call gets its own random
    value and the callback's token exchange fails with Google's
    "(invalid_grant) Missing code verifier" — confirmed live 2026-09-25.
    Passing the identical verifier explicitly (carried through via the
    signed oauth `state` param, see map_token.make_oauth_state) fixes
    this while keeping the whole flow stateless."""
    config = _oauth_client_config()
    if not config:
        return None
    if code_verifier:
        return Flow.from_client_config(
            config, scopes=SCOPES, redirect_uri=redirect_uri,
            code_verifier=code_verifier, autogenerate_code_verifier=False,
        )
    return Flow.from_client_config(config, scopes=SCOPES, redirect_uri=redirect_uri)


def get_profile_email(creds: Credentials) -> str:
    """Used right after completing the OAuth callback — creds are
    freshly minted from the code exchange, not yet stored anywhere."""
    service = build("gmail", "v1", credentials=creds, cache_discovery=False,
                    static_discovery=False)
    return service.users().getProfile(userId="me").execute().get("emailAddress", "")
