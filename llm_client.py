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
import requests

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_MODEL = "openai/gpt-oss-20b"
_session = requests.Session()


def _get_api_key() -> str:
    return os.environ.get("GROQ_API_KEY", "").strip()


def has_api_key() -> bool:
    return bool(_get_api_key())


def chat(system_prompt: str, user_content: str, *, json_mode: bool = False,
         max_tokens: int = 300, temperature: float = 0.0, timeout: int = 20,
         throttle_seconds: float = 1.0) -> str:
    """
    Returns the model's raw text response. Raises RuntimeError/
    requests exceptions on any failure — callers handle fail-soft
    themselves.

    Retries once on 429 (rate limited) with a longer wait, same
    pattern the old Gemini calls used. throttle_seconds is a small
    pre-call sleep (default 1s, well under Groq's free-tier per-minute
    allowance for this model) — not strictly required at this
    project's volume, kept mainly so a bulk backfill run doesn't burst
    requests in a tight loop.
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

    if throttle_seconds:
        time.sleep(throttle_seconds)

    last_status = None
    for attempt in range(2):
        r = _session.post(_GROQ_URL, headers=headers, json=payload, timeout=timeout)
        if r.status_code == 429 and attempt == 0:
            print("[LLM] rate limited, waiting 10s and retrying once...", flush=True)
            time.sleep(10)
            continue
        last_status = r.status_code
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"] or ""
    raise RuntimeError(f"Groq request failed after retry (status {last_status})")
