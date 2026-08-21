"""
Deterministic EnLiST notation adjudicator + gold-exact formatting.

The LLM emits an ORDERED list of SACT episodes, each tagged with a machine-readable
`change_trigger`. This module walks the EnLiST guideline table over that ordered list
to assign X.Y labels + New/Modified/Same, using three INDEPENDENT counters (e/a/i).
It also deterministically:
  - routes to the iLoT track from the LLM's own change_trigger "investigational_only"
    (the agent judges whether EVERY agent was investigational AT THE TIME it was given),
  - derives the U-code from each date's earliest/latest window,
  - derives reason_for_switching (PD-dominance; eLoT->aLoT recurrence = PFS event).

Labels are emitted in the gold's COMPACT form (e1.0 / a2.1 / i1.0). A separate
formatting layer maps the compact enums to the gold's verbose strings
(1bii Advanced (metastatic), 2a Curative, 8a.i PD, 4c Surgery).

Ported from the proven enlist714 assign_enlist_notation.
"""
from datetime import datetime
from typing import Any, Dict, List


_U_DATE_FIELDS = ("start_date", "stop_date", "first_pd_date", "censor_date", "recent_pd_date")


def _pdate(x):
    if not x:
        return None
    try:
        return datetime.strptime(str(x)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _derive_u(dt, earliest, latest, llm_u):
    """EnLiST U-code from the plausibility window: U0 exact, U1 <=14d, U2 <=1mo,
    U3 <=3mo, U4 wider. Fills null U-codes and makes U reproducible across charts."""
    if not dt:
        return None
    e, l = _pdate(earliest), _pdate(latest)
    if e and l:
        span = abs((l - e).days)
        if span <= 1:
            return "U0"
        if span <= 14:
            return "U1"
        if span <= 31:
            return "U2"
        if span <= 93:
            return "U3"
        return "U4"
    if llm_u in ("U0", "U1", "U2", "U3", "U4"):
        return llm_u
    return "U2"


def _setting_track(clinical_setting, treatment_intent, is_investigational_only) -> str:
    """Map a row to a notation track: 'e' (curative), 'a' (advanced), 'i' (investigational).
    Precedence: iLoT > eLoT (CURATIVE intent, EnLiST Principle 4) > aLoT."""
    if is_investigational_only:
        return "i"
    if treatment_intent == "2a" or clinical_setting == "1a":
        return "e"
    return "a"


def _fmt_composite(counters: Dict[str, Dict[str, int]]) -> str:
    """EnLiST patient-level composite snapshot in the framework's reporting format:
    "[eLoT X.Y + aLoT X.Y] + iLoT X.Y" (a track never used reads 0.0). X = total New LoTs in that
    track to date; Y = Modified LoTs since the most recent New. Snapshotting the running counters
    after each episode yields the cumulative composite for that row (ESMO EnLiST Table 3 col 10)."""
    def cell(t: str) -> str:
        c = counters[t]
        return f"{c['X']}.{c['Y']}"
    return f"[eLoT {cell('e')} + aLoT {cell('a')}] + iLoT {cell('i')}"


def assign_enlist_notation(episodes: List[Dict[str, Any]], derive_pfs: bool = True) -> List[Dict[str, Any]]:
    """Assign compact X.Y labels + New/Modified/Same by walking the EnLiST guidelines
    over an ordered (by start_date) episode list. Writes enlist_lot_label, lot_change_type,
    _notation_note, _track on each episode; derives U-codes.

    derive_pfs: when True (default), also derive the PFS-linked fields (the eLoT->aLoT setting-
      change PFS event and the reason_for_switching PD-dominance). Set False for the LoT-only
      Stage 3a pass, where PFS is determined later by the separate PFS agent."""
    counters = {"e": {"X": 0, "Y": 0}, "a": {"X": 0, "Y": 0}, "i": {"X": 0, "Y": 0}}

    out: List[Dict[str, Any]] = []
    for ep in episodes:
        setting = ep.get("clinical_setting")
        trigger = ep.get("change_trigger")
        # iLoT is decided by the LLM, not a drug allowlist: the agent emits change_trigger
        # "investigational_only" when EVERY agent in the regimen was investigational/unapproved
        # AT THE TIME it was given (a hardcoded list is time-agnostic and brittle).
        inv_only = (trigger == "investigational_only")

        track = _setting_track(setting, ep.get("treatment_intent"), inv_only)
        c = counters[track]
        first_in_track = (c["X"] == 0)
        change_type = "Same"
        note = ""

        if trigger in ("first_line", "new_cpd") or first_in_track:
            c["X"] += 1
            c["Y"] = 0
            change_type = "New"
            note = f"New ({track}): trigger={trigger}"
        elif trigger == "setting_change_e_to_a":
            c["X"] += 1
            c["Y"] = 0
            change_type = "New"
            note = "New: setting change early->advanced"
        elif trigger == "investigational_only":
            c["X"] += 1
            c["Y"] = 0
            change_type = "New"
            note = "New: investigational-only regimen (iLoT track)"
        elif trigger in ("agents_replaced_no_cpd", "agents_added_no_cpd"):
            c["Y"] += 1
            change_type = "Modified"
            note = f"Modified: trigger={trigger}"
        elif trigger in ("same_after_cpd", "agents_dropped_no_cpd"):
            change_type = "Same"
            note = f"Same: trigger={trigger}"
        else:
            change_type = "Same"
            note = f"Same (fallback): unrecognized trigger={trigger!r}"

        ep_out = dict(ep)
        ep_out["enlist_lot_label"] = f"{track}{c['X']}.{c['Y']}"   # compact: e1.0 / a2.1 / i1.0
        # Running cumulative composite across ALL three tracks as of this episode (Table 3 col 10).
        ep_out["enlist_composite_label"] = _fmt_composite(counters)
        ep_out["lot_change_type"] = change_type
        ep_out["_notation_note"] = note
        ep_out["_track"] = track
        out.append(ep_out)

    # ---- deterministic reason_for_switching + U-codes ----
    n = len(out)
    for i, ep in enumerate(out):
        track = ep.get("_track")
        if derive_pfs:
            nxt = out[i + 1] if i + 1 < n else None
            nxt_track = nxt.get("_track") if nxt else None
            # eLoT immediately followed by aLoT is a PFS EVENT for the eLoT ONLY when the SAME
            # regimen continued into the advanced setting because curative intent was abandoned
            # (change_trigger 'setting_change_e_to_a' on the NEXT line). If the eLoT instead ended
            # in a curative resection / local therapy and a DIFFERENT advanced line started later,
            # it is a CENSOR (per the clinician reviewers: local therapy no PD; the
            # eLoT counter merely freezes). So do NOT blanket-force flag=1 on every e->a adjacency;
            # trust the agent's flag except in the genuine setting-change continuation.
            if track == "e" and nxt_track == "a" and nxt and nxt.get("change_trigger") == "setting_change_e_to_a":
                ep["pfs_censor_flag"] = 1
                if not ep.get("first_pd_date"):
                    ep["first_pd_date"] = ep.get("censor_date")

            try:
                pfs = int(ep.get("pfs_censor_flag") or 0)
            except (TypeError, ValueError):
                pfs = 0
            reason = ep.get("reason_for_switching")
            clinical_setting = ep.get("clinical_setting")
            is_last = (i == n - 1)
            if pfs == 1:
                new_reason = "8e" if reason == "8e" else "8a.i"
            elif not is_last and reason == "8b" and not (track == "e" and clinical_setting == "1a"):
                new_reason = "8a.i"
            else:
                new_reason = reason
            ep["reason_for_switching"] = new_reason

        for fld in _U_DATE_FIELDS:
            ep[fld + "_U"] = _derive_u(ep.get(fld), ep.get(fld + "_earliest"),
                                       ep.get(fld + "_latest"), ep.get(fld + "_U"))
    return out


# ---------------------------------------------------------------------------
# GOLD-EXACT FORMATTING  (compact enum -> the clinician gold standard's verbose strings)
# ---------------------------------------------------------------------------
SETTING_LABELS = {
    "1a": "1a Early",
    "1bi": "1bi Advanced (locally advanced)",
    "1bii": "1bii Advanced (metastatic)",
}
INTENT_LABELS = {"2a": "2a Curative", "2b": "2b Non-curative"}
REASON_LABELS = {
    "8a.i": "8a.i PD",
    "8a.ii": "8a.ii Lack of adequate response",
    "8b": "8b Completed planned",
    "8c": "8c Intolerability",
    "8d": "8d Patient/clinician choice",
    "8e": "8e Death",
    "8f": "8f Other",
}
MODALITY_LABELS = {"4c": "4c Surgery", "4d": "4d Radiotherapy", "4e": "4e Other"}

# pfs_censor_reason is a fixed dropdown. The agent sometimes emits free-text prose here; snap it
# to one of the 6 canonical enum values so it matches the gold (defensive; the prompt also asks
# for the enum directly). Order matters: check the most specific keywords first.
PFS_REASON_ENUM = [
    "Ongoing", "Lost to follow-up", "Switched no PD",
    "Local therapy no PD", "Setting change", "Other",
]


def fmt_pfs_reason(v):
    """Normalize a pfs_censor_reason (possibly free-text) to one of the 6 enum values.
    None stays None (a PFS event has no censor reason)."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    low = s.lower()
    # exact enum match first
    for lab in PFS_REASON_ENUM:
        if low == lab.lower():
            return lab
    # keyword mapping for prose
    if "lost" in low and "follow" in low:
        return "Lost to follow-up"
    if "local therapy" in low or "local-therapy" in low:
        return "Local therapy no PD"
    if "setting" in low and ("change" in low or "flip" in low or "early" in low):
        return "Setting change"
    if "switch" in low:
        return "Switched no PD"
    if "ongoing" in low or "still on" in low or "continues" in low or "continued" in low:
        return "Ongoing"
    return "Other"


def _fmt(value, table):
    """Map a compact code to its gold-verbose label; pass through if already verbose
    or unknown; None stays None."""
    if value is None:
        return None
    v = str(value).strip()
    if v in table:
        return table[v]
    # already-verbose or partial: match on the leading code token
    for code, label in table.items():
        if v == label or v.startswith(code + " "):
            return label
    return v


def fmt_clinical_setting(v):
    return _fmt(v, SETTING_LABELS)


def fmt_treatment_intent(v):
    return _fmt(v, INTENT_LABELS)


def fmt_reason(v):
    return _fmt(v, REASON_LABELS)


def fmt_local_modality(v):
    return _fmt(v, MODALITY_LABELS)
