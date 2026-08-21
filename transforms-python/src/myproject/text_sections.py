"""
Deterministic note-reading helpers (NO LLM).

extract_excerpt(note_text)
   Mimic the clinician's reading strategy for nonsensitive/sensitive notes:
   find the EARLIEST occurrence of any recognized section header ("Assessment and
   Plan", "Impression", "Plan", ...) and keep from there to the END of the note.
   Tier 1 = Assessment/Plan/Impression anchors; Tier 2 = history/treatment
   fallback anchors; Tier 3 = no header found -> keep only the first few sentences
   (reason-for-visit) for a cheap relevance triage downstream.

Imaging rows do not pass through this module: Stage 2 reads the whole
impression_text, or the whole note_text when no impression is present.

Matching is case-insensitive and anchored to a "header-ish" boundary (start of
text, a newline, a run of 2+ spaces used as an EHR section separator, or sentence
punctuation) to avoid mid-sentence false positives.
"""
import re
from typing import List, Optional, Tuple

# Tier 1 -- Assessment / Plan / Impression anchors (incl. the & and A/P variants
# we found in the corpus). Order does NOT matter: the EARLIEST match in the text wins.
TIER1_ANCHORS: List[str] = [
    "assessment and plan",
    "assessment & plan",
    "assessment/plan",
    "a&p:",
    "a/p:",
    "assessment:",
    "plan of action",
    "plan of care",
    "plans:",
    "plan:",
    "impression and recommendations",
    "recommendations:",
    "recommendation:",
    "imaging studies:",
    "diagnostic studies:",
    "impression:",
]

# Tier 2 -- history fallback anchors (used only when NO Tier-1 / treatment-history anchor).
TIER2_ANCHORS: List[str] = [
    "interim history:",
    "surgical history:",
]

# ---- Treatment-history family: cumulative lists that repeat across a patient's notes
# (each visit repeats the prior list and appends). Handled with the two-part excerpt and
# de-duplicated to one canonical copy per patient (Option B). ----
TREATMENT_HISTORY_ANCHORS: List[str] = [
    "cancer treatment history:",
    "prior cancer treatment:",
    "current cancer treatment:",
]

# "Treatment History:" is broad (also appears as prose, e.g. "surgical treatment history").
# Treat it as a treatment-history FAMILY header ONLY when it is immediately followed by a
# numbered/dated list (the cumulative signature). This captures the GI-onc template's
# cumulative list while excluding prose narratives.
_GUARDED_TH_NEEDLE = "treatment history"
_NUMLIST_RE = re.compile(r"\d{1,2}\.\s")
_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _find_guarded_treatment_history(hay: str) -> int:
    """Earliest boundary 'treatment history' that is a header (colon soon) followed by a
    numbered/dated list within ~160 chars; else -1. Excludes prose 'treatment history. She...'."""
    start = 0
    while True:
        j = hay.find(_GUARDED_TH_NEEDLE, start)
        if j < 0:
            return -1
        if _is_boundary(hay, j):
            after = j + len(_GUARDED_TH_NEEDLE)
            window = hay[after:after + 160]
            if ":" in window[:15] and (_NUMLIST_RE.search(window) or _YEAR_RE.search(window)):
                return j
        start = j + 1


def _treatment_history_start(text: str) -> Tuple[int, Optional[str]]:
    """Earliest treatment-history-family start: the strict anchors (cancer/prior/current
    cancer treatment history) OR a guarded 'treatment history:' + numbered list."""
    hay = text.lower()
    idx, anchor = _earliest(hay, TREATMENT_HISTORY_ANCHORS)
    g = _find_guarded_treatment_history(hay)
    if g >= 0 and (idx < 0 or g < idx):
        return g, "treatment history:"
    return idx, anchor
# A&P variants that mark where Part 2 resumes (any of these count).
_AP_RESUME_ANCHORS = [
    "assessment and plan", "assessment & plan", "assessment/plan", "a&p:", "a/p:",
    "assessment:", "plan of action", "plan of care", "plans:", "plan:",
    "impression and recommendations", "recommendations:", "recommendation:", "impression:",
]
# Headers that end the treatment-history block (Part 1). The EARLIEST of these after
# the CTH start ends Part 1, so the CURRENT MEDICATIONS list and ALLERGIES section are
# excluded. In these notes the order is CTH -> CURRENT MEDICATIONS: -> ALLERGIES: -> ...
# so "current medications" is usually the cut; the rest are fallbacks.
_MIDDLE_START_HEADERS = [
    "current medications", "medications:", "allergies",
    "family history", "social history",
    "physical examination", "physical exam", "review of systems", "habits",
]
_SKIP_MARKER = "\n\n...[section skipped]...\n\n"


def _earliest_anchor_from(hay: str, anchors: List[str], start: int) -> int:
    """Earliest boundary-anchored index of any of `anchors` at/after `start`, or -1."""
    best = -1
    for a in anchors:
        st = start
        found = -1
        while True:
            j = hay.find(a, st)
            if j < 0:
                break
            if _is_boundary(hay, j):
                found = j
                break
            st = j + 1
        if found >= 0 and (best < 0 or found < best):
            best = found
    return best


# Bare (colon-less) A&P section headers, e.g. "Assessment Yolanda ..." / "Plan 1. ...".
_BARE_AP_WORDS = ("assessment", "plan")


def _preceded_by_header_boundary(text: str, j: int) -> bool:
    """True if position j looks like the start of a section (start of text, after a
    sentence terminator, or after a >=2-space EHR section gap)."""
    if j == 0:
        return True
    k = j - 1
    spaces = 0
    while k >= 0 and text[k] in " \t":
        spaces += 1
        k -= 1
    if k < 0 or spaces >= 2:
        return True
    return text[k] in ".!?;:\n\r"


def _followed_by_header_content(text: str, after: int) -> bool:
    """True if the first non-space char after the header is an uppercase letter or a
    digit (e.g. 'Assessment Yolanda...' or 'Plan 1. ...')."""
    k = after
    n = len(text)
    while k < n and text[k] in " \t.:-)\r\n":
        k += 1
    return k < n and (text[k].isupper() or text[k].isdigit())


def _find_bare_ap_header(text: str, start: int) -> int:
    """Earliest bare 'Assessment'/'Plan' SECTION HEADER at/after `start`, or -1.
    Strict to avoid prose matches: capitalized, whole word, header-ish boundary before,
    and header content after."""
    hay = text.lower()
    n = len(text)
    best = -1
    for w in _BARE_AP_WORDS:
        st = start
        while True:
            j = hay.find(w, st)
            if j < 0:
                break
            after = j + len(w)
            nxt = text[after] if after < n else " "
            if (text[j].isupper() and not nxt.isalpha()
                    and _is_boundary(hay, j)
                    and _preceded_by_header_boundary(text, j)
                    and _followed_by_header_content(text, after)):
                best = j if best < 0 else min(best, j)
                break
            st = j + 1
    return best


def _cth_boundaries(text: str) -> Tuple[int, int, int]:
    """
    Return (s, e0, p) for a CANCER TREATMENT HISTORY note:
      s  = start index of the CTH anchor (-1 if not present)
      e0 = start of the earliest "middle" header after s (ends Part 1), or -1
      p  = start of the earliest A&P variant after the middle header (Part 2), or -1
    """
    hay = text.lower()
    s, th = _treatment_history_start(text)
    if s < 0:
        return -1, -1, -1
    th = th or "treatment history:"
    e0 = -1
    for h in _MIDDLE_START_HEADERS:
        idx = _find_anchor(hay, h)
        if idx > s and (e0 < 0 or idx < e0):
            e0 = idx
    search_from = e0 if e0 > s else s + len(th)
    p = _earliest_anchor_from(hay, _AP_RESUME_ANCHORS, search_from)
    if p < 0:
        # fallback: bare (colon-less) 'Assessment'/'Plan' section header
        p = _find_bare_ap_header(text, search_from)
    return s, e0, p


def _cancer_tx_history_excerpt(text: str, hay: str, s: int) -> str:
    """
    Joined two-part excerpt for a CTH note (used by extract_excerpt):
      Part 1 (CTH -> middle header) + skip marker + Part 2 (A&P -> end).
    No A&P anywhere -> keep everything (s -> end). A&P but no middle header -> no skip.
    """
    _, e0, p = _cth_boundaries(text)
    if p < 0:
        return text[s:].strip()               # no A&P -> keep everything (#3)
    e = e0 if (0 <= e0 < p) else p             # no middle header -> no skip
    if s < e < p:
        return text[s:e].strip() + _SKIP_MARKER + text[p:].strip()
    return text[s:].strip()


def _is_boundary(hay: str, idx: int) -> bool:
    """
    True if the anchor at position `idx` starts a new word (word boundary).

    EHR notes are often flattened to single-spaced prose where a section header is
    preceded by a period+space (". Assessment and Plan:") or just a space, so we
    accept any non-alphanumeric preceding char. This still blocks mid-word matches
    (e.g. 'reassessment:' does not match the 'assessment:' anchor), and the colon in
    most anchors guards against mid-sentence false positives.
    """
    if idx == 0:
        return True
    return not hay[idx - 1].isalnum()


def _find_anchor(hay_lower: str, anchor: str) -> int:
    """Leftmost boundary-anchored index of `anchor` in the lowercased text, or -1."""
    start = 0
    while True:
        idx = hay_lower.find(anchor, start)
        if idx < 0:
            return -1
        if _is_boundary(hay_lower, idx):
            return idx
        start = idx + 1


def _earliest(hay_lower: str, anchors: List[str]) -> Tuple[int, Optional[str]]:
    """Return (index, anchor) of the earliest matching anchor, or (-1, None)."""
    best_idx = -1
    best_anchor: Optional[str] = None
    for anchor in anchors:
        idx = _find_anchor(hay_lower, anchor)
        if idx >= 0 and (best_idx < 0 or idx < best_idx):
            best_idx, best_anchor = idx, anchor
    return best_idx, best_anchor


def _first_sentences(text: str, n: int = 5) -> str:
    """Cheap sentence split -> first `n` sentences (reason-for-visit triage)."""
    parts: List[str] = []
    buf = []
    count = 0
    for ch in text:
        buf.append(ch)
        if ch in ".!?\n":
            seg = "".join(buf).strip()
            if seg:
                parts.append(seg)
                count += 1
                if count >= n:
                    break
            buf = []
    if count < n and buf:
        tail = "".join(buf).strip()
        if tail:
            parts.append(tail)
    return " ".join(parts).strip()


def extract_excerpt(note_text: Optional[str], first_sentences: int = 5) -> Tuple[Optional[str], str, Optional[str]]:
    """
    Return (relevant_excerpt, excerpt_tier, matched_header).

      tier "1"    -> earliest Tier-1 anchor .. end of note
      tier "2"    -> earliest Tier-2 anchor .. end of note
      tier "3"    -> no anchor: first `first_sentences` sentences (triage text)
      tier "skip" -> empty / null note_text
    """
    if not note_text or not str(note_text).strip():
        return None, "skip", None
    text = str(note_text)
    hay = text.lower()

    th_idx, th_anchor = _treatment_history_start(text)
    ap_idx, ap_anchor = _earliest(hay, TIER1_ANCHORS)

    # treatment-history-led note (CTH / prior / current cancer treatment) -> two-part
    # excerpt (history block + A&P, middle skipped), and the history block is de-duped.
    if th_idx >= 0 and (ap_idx < 0 or th_idx <= ap_idx):
        return _cancer_tx_history_excerpt(text, hay, th_idx), "1", th_anchor

    if ap_idx >= 0:
        return text[ap_idx:].strip(), "1", ap_anchor

    idx, anchor = _earliest(hay, TIER2_ANCHORS)
    if idx >= 0:
        return text[idx:].strip(), "2", anchor

    return _first_sentences(text, first_sentences), "3", None


