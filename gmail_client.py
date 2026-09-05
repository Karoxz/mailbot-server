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
from typing import Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
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


def get_thread_label_names(service, thread_id: str, label_map: dict) -> list:
    """Every non-system label name anywhere in this thread — ported from
    the desktop's _get_thread_info()/_get_thread_label_names(), simplified
    to just the names (the desktop also returns the thread's subject, used
    there only for its own Telegram "labeled thread" notification, which
    poller.py doesn't send — see roadmap on the deliberately-simplified
    notification scope)."""
    try:
        thread = service.users().threads().get(
            userId="me", id=thread_id, format="metadata",
            metadataHeaders=["Subject"],
        ).execute()
        found = set()
        for msg in thread.get("messages", []):
            for lid in msg.get("labelIds", []):
                if lid not in _SYSTEM_LABEL_IDS and not lid.startswith("CATEGORY_"):
                    found.add(lid)
        return [label_map.get(lid, lid) for lid in found]
    except Exception:
        return []


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
