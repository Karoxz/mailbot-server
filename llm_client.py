# =============================================================
# llm_client.py — server-side module
#
# Thin wrapper around Groq's OpenAI-compatible chat completions API —
# the free-tier replacement for Gemini (2026-09-08). Gemini's free
# tier had a confirmed, recurring 500-requests/day cap that was
# blocking routine backfill/testing volume; Groq's free tier is far
# more generous for a model this size, and needs zero new server
# infrastructure — plain REST via `requests` (already a project
# dependency), no new package to install, unlike the old
# `google-genai` SDK this replaces.
#
# Used by thread_learner.py, reply_classifier.py, and
# broker_note_extractor.py — every LLM call in this project goes
# through here now, so there's exactly one place that knows the API
# key, the model, retry/throttle behavior, and the request/response
# shape.
#
# Model: openai/gpt-oss-20b — checked Groq's real current /models list
# at integration time (2026-09-08) rather than assuming a model name
# from memory; the previously-planned llama-3.1-8b-instant no longer
# exists on Groq's catalog. gpt-oss-20b is OpenAI's own open-weight
# model, a reasonable size/speed fit for the short, bounded extraction/
# classification tasks this project needs (one number, one of ~4
# labels, a handful of short structured fields) — not open-ended
# reasoning.
#
# IMPORTANT: this is a reasoning model — by default it spends part of
# its own output budget on a hidden chain-of-thought (returned
# separately as message.reasoning, not message.content) BEFORE the
# actual answer. Confirmed live: max_tokens=10 with no reasoning_effort
# set produced finish_reason="length" and EMPTY content — the whole
# budget went to reasoning tokens, no real answer at all. Fixed by
# always sending reasoning_effort="low", which keeps the hidden
# reasoning short (~5 tokens in testing) and lets the real answer
# actually get emitted within a normal token budget.
#
# Fails loud (raises) — every caller in this project already has its
# own fail-soft wrapper around its LLM call (return None / "no_signal"
# / etc. on exception), so this stays a thin, honest wrapper rather
# than swallowing errors a second time.
# =============================================================

import os
import time
import threading
import requests

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_MODEL = "openai/gpt-oss-20b"
_session = requests.Session()

# Real bug, found live 2026-10-06/07: throttle_seconds used to be a
# plain time.sleep() BEFORE each call — a per-call-SITE delay, not a
# per-PROJECT one. thread_learner's backfill runs in its own background
# thread per license (see poller.py's _maybe_run_thread_learning), so
# two licenses' passes overlapping (or a backfill running alongside a
# live reply classification) meant two threads each independently
# sleeping 1s between THEIR OWN calls but firing concurrently — the
# actual aggregate rate against Groq's single shared quota was never
# bounded by this at all. Confirmed live: ~2 hours of near-continuous
# 429s, severe enough that reply_classifier's classification (which
# bid_history/the web Live Feed both depend on) was failing right
# alongside thread_learner's, silently losing real broker-reply
# outcomes (reply_classifier fails soft by design). Fixed with a
# process-wide lock enforcing a minimum gap between ANY two Groq calls
# from ANY caller/thread, not just consecutive calls from the same one.
_rate_lock = threading.Lock()
_last_call_at = 0.0


def _wait_for_turn(min_interval: float):
    global _last_call_at
    with _rate_lock:
        now = time.monotonic()
        wait = _last_call_at + min_interval - now
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


# Circuit breaker, same bug/fix as above — thread_backfill.run_backfill
# has no idea a 429 even happened (thread_learner._extract_rate_from_text
# fails soft to None, by design, same contract every caller here uses),
# so a single backfill pass across a large bid-labeled thread history
# would grind through every remaining thread/message ONE AT A TIME once
# Groq's quota was exhausted — each one still paying the full throttle +
# request + 10s-retry-wait + request cycle for a call virtually
# guaranteed to fail, which is exactly what stretched this into a
# 2+ hour outage (and kept the quota pinned at its limit the whole
# time, since the retries themselves were most of the traffic). Once
# a 429 survives the in-call retry, every call for the next
# _COOLDOWN_SECONDS fails IMMEDIATELY (no HTTP call, no sleep) instead
# of repeating the same losing bet — lets Groq's window actually
# recover, and turns a large backlog's failure mode from "~15s per
# item for hours" into "near-instant for the rest of this cooldown".
_COOLDOWN_SECONDS = 45
_circuit_open_until = 0.0


def _get_api_key() -> str:
    return os.environ.get("GROQ_API_KEY", "").strip()


def has_api_key() -> bool:
    return bool(_get_api_key())


def chat(system_prompt: str, user_content: str, *, json_mode: bool = False,
         max_tokens: int = 300, temperature: float = 0.0, timeout: int = 20,
         throttle_seconds: float = 2.5) -> str:
    """
    Returns the model's raw text response. Raises RuntimeError/
    requests exceptions on any failure — callers handle fail-soft
    themselves.

    Retries once on 429 (rate limited), waiting whatever Groq's own
    Retry-After header says (falls back to 10s if it doesn't send one).
    throttle_seconds enforces a minimum gap before EVERY call, GLOBALLY
    across every thread/caller in the process (see _wait_for_turn) —
    bumped from 1.0 to 2.5s live 2026-10-07 after ~2 hours of
    near-continuous 429s showed 1s wasn't actually safe at this
    project's real concurrent volume (see the module-level comment on
    _rate_lock for the full story).
    """
    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set")

    global _circuit_open_until
    remaining = _circuit_open_until - time.monotonic()
    if remaining > 0:
        raise RuntimeError(f"Groq rate limit cooldown — retrying in {remaining:.0f}s")

    payload = {
        "model": _MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "reasoning_effort": "low",  # see module docstring — without this,
                                     # the hidden chain-of-thought can eat
                                     # the whole token budget and return
                                     # empty content, confirmed live.
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    last_status = None
    for attempt in range(2):
        if throttle_seconds:
            _wait_for_turn(throttle_seconds)
        r = _session.post(_GROQ_URL, headers=headers, json=payload, timeout=timeout)
        last_status = r.status_code
        if r.status_code == 429:
            if attempt == 0:
                retry_after = r.headers.get("Retry-After")
                try:
                    wait = max(float(retry_after), 1.0) if retry_after else 10.0
                except ValueError:
                    wait = 10.0
                print(f"[LLM] rate limited, waiting {wait:.0f}s and retrying once...", flush=True)
                time.sleep(wait)
                continue
            # Still 429 after the retry — open the circuit (see the
            # module-level comment) instead of letting raise_for_status()
            # below bubble a generic HTTPError that skips this entirely.
            _circuit_open_until = time.monotonic() + _COOLDOWN_SECONDS
            print(f"[LLM] still rate limited after retry — opening circuit for {_COOLDOWN_SECONDS}s", flush=True)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"] or ""
    raise RuntimeError(f"Groq request failed after retry (status {last_status})")
