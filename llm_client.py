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
import re
import time
import threading
import requests

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_MODEL = "openai/gpt-oss-20b"
_session = requests.Session()

# Real bug, found live 2026-10-06/07, two layers deep:
#
# Layer 1 (fixed first, turned out insufficient on its own): throttle_
# seconds used to be a plain time.sleep() BEFORE each call — a
# per-call-SITE delay, not a per-PROJECT one. thread_learner's backfill
# runs in its own background thread per license (see poller.py's
# _maybe_run_thread_learning), so two licenses' passes overlapping (or
# a backfill running alongside a live reply classification) meant two
# threads each independently pacing THEIR OWN calls but firing
# concurrently — the aggregate rate against Groq's one shared quota was
# never actually bounded.
#
# Layer 2 (the real ceiling, found via a direct diagnostic curl to
# Groq's API — see x-ratelimit-* response headers): Groq's free tier
# for this model is NOT a request-count limit (997/1000 requests still
# available when this was checked) — it's 8000 TOKENS/minute. A
# backfill pass burns through a large batch of messages, each one a
# full prompt (up to ~4000 chars of email body) + completion + hidden
# reasoning tokens — a handful of those calls alone can exhaust an
# 8000-token/min budget, regardless of how well-spaced the *requests*
# are. Pacing on request count (even correctly, globally) was solving
# the wrong dimension — confirmed live: 429s kept recurring in tight
# bursts even after the Layer 1 fix shipped.
#
# Fixed by reading Groq's own x-ratelimit-remaining-tokens /
# x-ratelimit-reset-tokens response headers after every call (success
# or 429) and using THAT to pace the next one, instead of guessing.
# _next_allowed_at is the single shared "no Groq call before this
# monotonic time" gate, pushed forward by both the fixed per-call floor
# (throttle_seconds, a safety net for when headers are absent — a
# network error before any response, for instance) and the real
# token-budget signal, whichever is further out. A wait longer than
# _MAX_SLEEP_SECONDS fails the caller fast instead of blocking the
# thread for it — this is what actually ended the 2+ hour outage: a
# backfill with a large backlog now blows through every remaining item
# in milliseconds (each one failing fast) instead of synchronously
# sleeping out a real multi-minute token-budget reset once per message.
_rate_lock = threading.Lock()
_next_allowed_at = 0.0
_MAX_SLEEP_SECONDS = 5.0
_TOKEN_SAFETY_MARGIN = 1500  # don't fire the next call if fewer tokens than this remain in the current window

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")


def _parse_duration(s) -> float:
    """Groq sends reset windows as Go-style duration strings, e.g.
    "4m19.2s" or "577ms" — not seconds. Unrecognized/missing -> 0."""
    if not s:
        return 0.0
    total = 0.0
    for value, unit in _DURATION_RE.findall(s):
        total += float(value) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total


def _note_token_budget(headers):
    """Called after every real Groq response (success or 429). If the
    token window is critically low, pushes the shared gate out to
    Groq's own reported reset time — proactive, so the NEXT call never
    has to find out the hard way."""
    remaining, reset = headers.get("x-ratelimit-remaining-tokens"), headers.get("x-ratelimit-reset-tokens")
    if remaining is None or reset is None:
        return
    try:
        remaining = int(remaining)
    except ValueError:
        return
    if remaining >= _TOKEN_SAFETY_MARGIN:
        return
    wait_s = _parse_duration(reset)
    if wait_s <= 0:
        return
    global _next_allowed_at
    with _rate_lock:
        candidate = time.monotonic() + wait_s
        if candidate > _next_allowed_at:
            _next_allowed_at = candidate


def _wait_for_turn(min_interval: float) -> bool:
    """True if the caller may proceed (a short wait, if any, already
    happened). False if the wait would be long enough that the caller
    should fail fast instead of blocking its thread for it."""
    global _next_allowed_at
    with _rate_lock:
        now = time.monotonic()
        target = max(_next_allowed_at, now)
        wait = target - now
        if wait > _MAX_SLEEP_SECONDS:
            return False
        if wait > 0:
            time.sleep(wait)
        _next_allowed_at = time.monotonic() + min_interval
        return True


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

    Paced against Groq's real token-per-minute budget (read from its
    own response headers — see _note_token_budget), not a guessed
    request-count interval; throttle_seconds is just the floor used
    when no header data is available yet. A 429 retries once using
    Groq's own reported reset time when available (else its
    Retry-After header, else a flat 10s); a wait longer than
    _MAX_SLEEP_SECONDS skips the retry and fails fast instead of
    blocking this thread for it — see the module-level comment for why.
    """
    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set")

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
        if throttle_seconds and not _wait_for_turn(throttle_seconds):
            wait_remaining = _next_allowed_at - time.monotonic()
            raise RuntimeError(f"Groq rate limit cooldown — retrying in {wait_remaining:.0f}s")
        r = _session.post(_GROQ_URL, headers=headers, json=payload, timeout=timeout)
        last_status = r.status_code
        _note_token_budget(r.headers)
        if r.status_code == 429:
            if attempt == 0:
                reset_tokens = _parse_duration(r.headers.get("x-ratelimit-reset-tokens"))
                retry_after = r.headers.get("Retry-After")
                try:
                    retry_after = max(float(retry_after), 1.0) if retry_after else 0.0
                except ValueError:
                    retry_after = 0.0
                wait = max(reset_tokens, retry_after) or 10.0
                if wait > _MAX_SLEEP_SECONDS:
                    # Not a quick retry anymore — _note_token_budget
                    # already pushed the shared gate out; fail fast
                    # rather than block this thread for it.
                    print(f"[LLM] rate limited, {wait:.0f}s to recover — failing fast instead of blocking", flush=True)
                    raise RuntimeError(f"Groq rate limited — retrying in {wait:.0f}s")
                print(f"[LLM] rate limited, waiting {wait:.0f}s and retrying once...", flush=True)
                time.sleep(wait)
                continue
            print("[LLM] still rate limited after retry", flush=True)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"] or ""
    raise RuntimeError(f"Groq request failed after retry (status {last_status})")
