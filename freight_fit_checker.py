# =============================================================
# freight_fit_checker.py  —  server-side module
#
# Deterministic geometry/weight cross-check between what the broker's
# notes say about the freight (pallet count/dims, dimensional-freight
# type — from broker_note_extractor) and what the ACTUAL matched truck
# can physically carry (interior dimensions, payload capacity — from
# the truck-matching result in parser_core.py). Pure arithmetic, no
# LLM call, no network I/O — free and instant on every matched load.
#
# Two severities:
#   issues   — hard problems: doesn't fit / overweight / freight
#              longer than the trailer. These should weigh heavily on
#              the decision engine.
#   warnings — soft concerns: fits only if rotated, near payload limit,
#              long enough that a permit MIGHT be needed (a rule-of-
#              thumb heuristic, NOT a substitute for verifying actual
#              state-by-state legal-length rules before dispatch).
#
# Returns "not enough data to check" (checked=False, fits=None) rather
# than guessing when the broker notes or truck record don't have the
# numbers needed — silence here is more honest than a false green
# light or a false alarm.
# =============================================================

import re
from typing import Any, Dict, List, Optional, Tuple

# Above this length (inches), flag that an oversize permit MIGHT be
# needed depending on state — 480in = 40ft, a common rule-of-thumb
# threshold before length permits start becoming relevant on typical
# combination/straight-truck configurations. This is intentionally a
# rough heuristic; actual permit requirements vary by state and by
# total vehicle length, not just cargo length.
_OVERSIZE_LENGTH_HEURISTIC_IN = 480.0

# A load within this fraction of the truck's payload cap gets a
# warning even though it technically still fits.
_PAYLOAD_WARNING_MARGIN = 0.9


def _parse_dims(dims_str: Optional[str]) -> Optional[Tuple[float, float, float]]:
    """
    Parse a free-text 'LxWxH' string (any casing/spacing around the
    x's) into a (length, width, height) tuple of floats, in inches.
    Returns None if the string is missing or doesn't parse cleanly —
    callers must treat that as "unknown," not "zero."
    """
    if not dims_str:
        return None
    parts = re.split(r"\s*[xX]\s*", dims_str.strip())
    if len(parts) < 3:
        return None
    try:
        nums = [float(re.sub(r"[^\d.]", "", p)) for p in parts[:3]]
    except ValueError:
        return None
    if any(n <= 0 for n in nums):
        return None
    return (nums[0], nums[1], nums[2])


def check_freight_fit(extraction: Optional[Dict[str, Any]],
                       truck_dimensions: Optional[str],
                       truck_max_payload_lbs: Optional[float],
                       load_weight_lbs: Optional[float]) -> Dict[str, Any]:
    """
    Args:
      extraction:            the dict returned by
                              broker_note_extractor.extract_broker_notes()
                              (or None if extraction wasn't available)
      truck_dimensions:      the matched truck's "LxWxH" string
      truck_max_payload_lbs: the matched truck's payload cap, in lbs
      load_weight_lbs:       the load's total weight, in lbs, from the
                              broker's posting (regex-parsed elsewhere)

    Returns:
      {
        "checked":  bool,           # False if nothing below could be
                                     # checked at all (missing data)
        "fits":     Optional[bool], # None if undetermined
        "issues":   [str, ...],     # hard problems
        "warnings": [str, ...],     # soft concerns
      }
    """
    issues: List[str] = []
    warnings: List[str] = []
    checked = False

    truck_dims = _parse_dims(truck_dimensions)
    ext = extraction or {}

    # ── Weight check ────────────────────────────────────────────────
    if load_weight_lbs and truck_max_payload_lbs:
        checked = True
        if load_weight_lbs > truck_max_payload_lbs:
            issues.append(
                f"Load weight ({load_weight_lbs:,.0f} lbs) exceeds truck's "
                f"payload capacity ({truck_max_payload_lbs:,.0f} lbs)"
            )
        elif load_weight_lbs > truck_max_payload_lbs * _PAYLOAD_WARNING_MARGIN:
            warnings.append(
                f"Load weight ({load_weight_lbs:,.0f} lbs) is close to truck's "
                f"payload capacity ({truck_max_payload_lbs:,.0f} lbs)"
            )

    # ── Pallet footprint / rotation check ────────────────────────────
    pallet_dims  = _parse_dims(ext.get("pallet_dimensions"))
    pallet_count = ext.get("pallet_count")

    fits_as_is = fits_rotated = False
    if pallet_dims and truck_dims:
        checked = True
        p_l, p_w, p_h = pallet_dims
        t_l, t_w, t_h = truck_dims

        if p_h > t_h:
            issues.append(
                f"Pallet height ({p_h:.0f}in) exceeds truck's interior height ({t_h:.0f}in)"
            )

        fits_as_is   = p_l <= t_l and p_w <= t_w
        fits_rotated = p_w <= t_l and p_l <= t_w

        if not fits_as_is and not fits_rotated:
            issues.append(
                f"Pallet footprint ({p_l:.0f}x{p_w:.0f}in) doesn't fit truck's "
                f"floor ({t_l:.0f}x{t_w:.0f}in) in either orientation"
            )
        elif not fits_as_is and fits_rotated:
            warnings.append("Pallets fit only if rotated 90° from how they're usually loaded")

        # Rough single-stacked capacity sanity check — a coarse estimate,
        # not a real loading-plan calculation (ignores stacking, spacing
        # between pallets, and load-bar/wall constraints).
        if pallet_count and (fits_as_is or fits_rotated):
            item_len = p_l if fits_as_is else p_w
            item_wid = p_w if fits_as_is else p_l
            per_row = max(1, int(t_w // item_wid))
            rows    = max(1, int(t_l // item_len))
            est_capacity = per_row * rows
            if est_capacity and pallet_count > est_capacity:
                warnings.append(
                    f"{pallet_count} pallets may not all fit single-stacked — "
                    f"estimated floor capacity ~{est_capacity} without doubling/stacking"
                )

    # ── Dimensional/oversized freight check ──────────────────────────
    dim_type = ext.get("dimensional_freight_type")
    if dim_type and dim_type != "none":
        checked = True
        # Use the longest known dimension as a rough stand-in for item
        # length — pallet_dimensions is the only numeric geometry we
        # have from the extraction; there's no separate "item length"
        # field, so this is intentionally an approximation.
        longest = max(pallet_dims) if pallet_dims else None

        if longest and truck_dims:
            t_l = truck_dims[0]
            if longest > t_l:
                issues.append(
                    f"{dim_type.replace('_', ' ').title()} freight (~{longest:.0f}in) "
                    f"exceeds truck's interior length ({t_l:.0f}in)"
                )
            if longest >= _OVERSIZE_LENGTH_HEURISTIC_IN:
                warnings.append(
                    f"{dim_type.replace('_', ' ').title()} freight is long enough "
                    f"(~{longest/12:.0f}ft) that oversize permits may be required "
                    f"depending on state — verify before dispatch"
                )
        else:
            warnings.append(
                f"Dimensional freight flagged ({dim_type}) but no dimensions given "
                f"to verify truck fit — confirm manually before bidding"
            )

    return {
        "checked":  checked,
        "fits":     (False if issues else (True if checked else None)),
        "issues":   issues,
        "warnings": warnings,
    }
