# =============================================================
# decision_engine.py  —  server-side module
#
# Deterministic Accept / Bid / Negotiate / Reject scoring for a
# matched load, built entirely from signals already computed
# elsewhere: broker track record (bid_history.broker_summary),
# deadhead-vs-radius margin, Google Maps verification confidence
# (parser_core's maps_verification), and rate guidance availability
# (bid_history.get_bid_recommendation). No LLM call, no network I/O —
# pure scoring over data that's already in memory by the time this
# runs, so it's free and instant to call on every parsed load.
#
# Deliberately conservative on one specific point: a broker with ZERO
# resolved bid history can never score better than "Negotiate" here,
# no matter how good the other signals look — "Accept"/"Bid" imply a
# level of confidence that simply doesn't exist without any track
# record with that broker yet. See _SEVERITY / the override at the
# bottom of get_load_decision().
# =============================================================

from typing import Any, Dict, List, Optional

import bid_history
import fleet_store

# Decisions ordered from most to least favorable — used only for the
# "never better than Negotiate for an unknown broker" override below.
_SEVERITY = {"Accept": 0, "Bid": 1, "Negotiate": 2, "Reject": 3}
_RANK_TO_DECISION = {v: k for k, v in _SEVERITY.items()}

# A broker needs at least this many resolved (won/lost) bids before
# its win rate is trusted as a real signal rather than noise.
_MIN_RESOLVED_FOR_WIN_RATE = 5


def _score_broker(broker_email: str) -> tuple:
    """Returns (score_delta, reasons, broker_known)."""
    if not broker_email:
        return -1, ["No broker email on this load — can't check track record"], False

    stats = bid_history.broker_summary(broker_email)
    if not stats or stats["total_bids"] == 0:
        return -1, ["No history with this broker yet — proceed cautiously"], False

    resolved = stats["won"] + stats["lost"]
    win_rate = stats.get("win_rate")

    if win_rate is None or resolved < _MIN_RESOLVED_FOR_WIN_RATE:
        return 0, [f"Limited history with this broker ({resolved} resolved "
                    f"bid{'s' if resolved != 1 else ''} so far)"], True

    pct = round(win_rate * 100)
    if win_rate >= 0.5:
        return 2, [f"Strong win rate with this broker ({pct}% of {resolved} resolved bids)"], True
    if win_rate <= 0.15:
        return -3, [f"Historically low win rate with this broker ({pct}% of {resolved} resolved bids)"], True
    return 0, [f"Mixed history with this broker ({pct}% of {resolved} resolved bids)"], True


def _score_radius_margin(deadhead_miles: Optional[float],
                          max_radius_miles: Optional[float]) -> tuple:
    """Returns (score_delta, reasons)."""
    if not deadhead_miles or not max_radius_miles:
        return 0, []
    pct = deadhead_miles / max_radius_miles
    if pct <= 0.5:
        return 1, [f"Deadhead well within range ({deadhead_miles:.0f}mi of {max_radius_miles:.0f}mi cap)"]
    if pct >= 0.9:
        return -1, [f"Deadhead near radius cap ({deadhead_miles:.0f}mi of {max_radius_miles:.0f}mi cap)"]
    return 0, []


def _score_maps_verification(maps_verification: Optional[Dict[str, Any]]) -> tuple:
    """Returns (score_delta, reasons)."""
    if not maps_verification:
        return 0, []
    if maps_verification.get("flagged") or maps_verification.get("confidence") == "low":
        diff_pct = maps_verification.get("diff_pct")
        detail = f" ({diff_pct*100:.0f}% off GraphHopper)" if diff_pct else ""
        return -1, [f"Mileage verification flagged a discrepancy{detail}"]
    if maps_verification.get("confidence") in ("high", "medium"):
        return 1, []
    return 0, []


def _score_rate_guidance(bid_recommendation: Optional[Dict[str, Any]]) -> tuple:
    """Returns (score_delta, reasons)."""
    if bid_recommendation:
        return 1, [f"Rate guidance available (${bid_recommendation['suggested_amount']:,.0f} "
                    f"suggested, n={bid_recommendation['sample_size']})"]
    return 0, ["No rate history for this lane/broker/vehicle yet — pricing blind"]


def _score_freight_fit(freight_fit: Optional[Dict[str, Any]]) -> tuple:
    """
    Returns (score_delta, reasons). A hard fit issue (freight literally
    doesn't fit / overweight) is weighted heavily enough on its own to
    push the decision toward Reject — this is a physical constraint,
    not a soft preference like the other signals.
    """
    if not freight_fit or not freight_fit.get("checked"):
        return 0, []
    if freight_fit.get("issues"):
        return -4, [f"Fit check: {issue}" for issue in freight_fit["issues"]]
    if freight_fit.get("warnings"):
        return -1, [f"Fit check: {warning}" for warning in freight_fit["warnings"]]
    return 0, []


def get_load_decision(broker_email: str = "",
                       deadhead_miles: Optional[float] = None,
                       max_radius_miles: Optional[float] = None,
                       maps_verification: Optional[Dict[str, Any]] = None,
                       bid_recommendation: Optional[Dict[str, Any]] = None,
                       freight_fit: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Returns:
      {
        "decision":     "Accept" | "Bid" | "Negotiate" | "Reject",
        "confidence":   0.0-1.0,   # how much data backs this decision,
                                   # NOT how good the load is
        "score":        int,      # raw signal score, for debugging/tuning
        "reasons":      [str, ...],
        "broker_known": bool,
      }

    Thresholds (score -> decision), before the unknown-broker override:
        score <= -3          -> Reject
        -3 <  score <= -1    -> Negotiate
        -1 <  score <  3     -> Bid
        score >= 3           -> Accept
    """
    # ── Blacklist override ──────────────────────────────────────────
    # A dispatcher-blacklisted broker is an absolute business rule, not
    # a signal to weigh against others — short-circuits everything else
    # below with maximum confidence, before any scoring happens.
    if broker_email and fleet_store.is_broker_blacklisted(broker_email):
        return {
            "decision":     "Reject",
            "confidence":   0.95,
            "score":        -999,
            "reasons":      [f"Broker {broker_email} is blacklisted"],
            "broker_known": True,
        }

    score = 0
    reasons: List[str] = []

    broker_delta, broker_reasons, broker_known = _score_broker(broker_email)
    score += broker_delta
    reasons += broker_reasons

    radius_delta, radius_reasons = _score_radius_margin(deadhead_miles, max_radius_miles)
    score += radius_delta
    reasons += radius_reasons

    maps_delta, maps_reasons = _score_maps_verification(maps_verification)
    score += maps_delta
    reasons += maps_reasons

    rate_delta, rate_reasons = _score_rate_guidance(bid_recommendation)
    score += rate_delta
    reasons += rate_reasons

    fit_delta, fit_reasons = _score_freight_fit(freight_fit)
    score += fit_delta
    reasons += fit_reasons

    if score <= -3:
        decision = "Reject"
    elif score <= -1:
        decision = "Negotiate"
    elif score < 3:
        decision = "Bid"
    else:
        decision = "Accept"

    # ── Unknown-broker override ─────────────────────────────────────
    # Never let a fresh broker with zero track record come out as
    # "Accept" or "Bid" purely on the strength of unrelated signals
    # (good radius margin, clean Maps verification, etc.) — those
    # don't substitute for actually knowing how this broker behaves.
    if not broker_known:
        current_rank = _SEVERITY[decision]
        negotiate_rank = _SEVERITY["Negotiate"]
        decision = _RANK_TO_DECISION[max(current_rank, negotiate_rank)]

    # Confidence reflects how much real data backs this call, separate
    # from whether the load itself looks good.
    confidence = 0.2  # baseline — we always have deadhead/radius at minimum
    if broker_known:
        confidence += 0.3
    if bid_recommendation:
        confidence += 0.3
    if maps_verification and maps_verification.get("confidence") in ("high", "medium"):
        confidence += 0.2
    confidence = round(min(confidence, 0.95), 2)

    return {
        "decision":     decision,
        "confidence":   confidence,
        "score":        score,
        "reasons":      reasons,
        "broker_known": broker_known,
    }