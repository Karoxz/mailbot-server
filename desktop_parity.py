# =============================================================
# desktop_parity.py — server-side, added 2026-09-26
#
# Small, PURE helpers ported verbatim (behavior-for-behavior) from the
# desktop app (client/main copy.py) so the web/standalone engine
# (poller.py) makes exactly the same decisions the desktop does. Kept
# separate from poller.py so each one can be unit-tested in isolation
# and so the port is easy to diff against the desktop source. The
# desktop file is only ever READ to write these — never modified.
# =============================================================

import re

from parser_core import _US_STATES_SET, FREIGHT_MARKERS

# desktop: _QUOTE_BOUNDARY_PATTERNS / _strip_quoted_reply (main copy.py
# ~1330-1361). Returns only the NEW content of a reply, before the first
# quoted-history boundary, so a broker's reply that quotes the whole
# original posting can't re-match as a fresh load and re-notify.
_QUOTE_BOUNDARY_PATTERNS = [
    re.compile(r"^\s*>"),                                            # plain-text quoted line
    re.compile(r"^On .+ wrote:\s*$", re.IGNORECASE),                 # Gmail/Apple Mail/most clients
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE),  # Outlook
]


def strip_quoted_reply(text: str) -> str:
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if any(p.match(line) for p in _QUOTE_BOUNDARY_PATTERNS):
            return "\n".join(lines[:i]).strip()
    return (text or "").strip()


# desktop: _extract_state_codes_from_text (main copy.py ~1238)
def extract_state_codes_from_text(text: str) -> list:
    found, seen = [], set()
    for token in re.findall(r"\b([A-Z]{2})\b", (text or "").upper()):
        if token in _US_STATES_SET and token not in seen:
            seen.add(token)
            found.append(token)
    return found


# desktop: _process_email's reply/freight detection (main copy.py ~2297)
def is_reply_subject(subject: str) -> bool:
    return (subject or "").upper().strip().startswith(("RE:", "FW:", "FWD:"))


def is_freight_subject(subject: str) -> bool:
    upper = (subject or "").upper()
    return (not is_reply_subject(subject)) and any(m in upper for m in FREIGHT_MARKERS)


def labeled_thread_message(label_names: list, subject: str) -> str:
    """Exact text of desktop's _notify_labeled_thread Telegram ping."""
    states = extract_state_codes_from_text(subject)
    state_str = " · ".join(states) if states else "—"
    return "\n".join(["", f"📌 Label:  {', '.join(label_names)}", f"📍 States: {state_str}"])
