"""
nw_align.py -- Needleman-Wunsch alignment + field accuracy for LoT episodes.

Ported from an earlier prompt-validation implementation (accuracy_nw_aligned.py), but
adapted for LONG-format episode tables (one row per episode) instead of the wide
(line_1_*, line_2_*...) format. Deterministic: regimen equivalence is decided by
Jaccard on canonical drug sets (no LLM adjudication).

Alignment respects temporal order (LoT episodes are inherently sequential).
Composite similarity = drug Jaccard (0.5) + start proximity 60d (0.3) + end 90d (0.2).
"""
import re
from datetime import datetime
from typing import Optional, List, Tuple, Dict

import numpy as np
import pandas as pd

GAP_PENALTY = -0.5
W_DRUG, W_START, W_END = 0.5, 0.3, 0.2
REGIMEN_LENIENT_JACCARD = 0.5   # >= this = same backbone (lenient match)

# ============================================================
# DRUG DICTIONARIES
# ============================================================
SYNONYMS = {
    "5fu": "5-fu", "5-fluorouracil": "5-fu", "fluorouracil": "5-fu", "5fu-": "5-fu",
    "5-fu/lv": "5-fu", "5fu/lv": "5-fu", "5-fu/leucovorin": "5-fu",
    "xeloda": "capecitabine", "cape": "capecitabine",
    "lv": "leucovorin", "folinic acid": "leucovorin",
    "avastin": "bevacizumab", "stivarga": "regorafenib", "erbitux": "cetuximab",
    "vectibix": "panitumumab", "keytruda": "pembrolizumab", "opdivo": "nivolumab",
    "yervoy": "ipilimumab", "cyramza": "ramucirumab", "zaltrap": "aflibercept",
    "tas-102": "trifluridine-tipiracil", "tas102": "trifluridine-tipiracil",
    "tas 102": "trifluridine-tipiracil", "lonsurf": "trifluridine-tipiracil",
    "trifluridine/tipiracil": "trifluridine-tipiracil",
    "trifluridine-tipiracil": "trifluridine-tipiracil", "trifluridine": "trifluridine-tipiracil",
    "medi4736": "durvalumab", "medi473": "durvalumab", "imfinzi": "durvalumab",
    "41bb": "pf-05082566", "pf-05082566": "pf-05082566",
    "zw25": "zanidatamab", "ds-8201a": "trastuzumab-deruxtecan", "ds8201a": "trastuzumab-deruxtecan",
    "ds-8201": "trastuzumab-deruxtecan", "enhertu": "trastuzumab-deruxtecan",
    "trastuzumab deruxtecan": "trastuzumab-deruxtecan", "trastuzumab-deruxtecan": "trastuzumab-deruxtecan",
    "agen2034": "balstilimab", "agen1181": "agen1181",
    "m7824": "bintrafusp-alfa", "bintrafusp alfa": "bintrafusp-alfa",
    "camptosar": "irinotecan", "irinotecan liposome": "irinotecan",
    "fruquintinib": "fruquintinib", "fruquintinib / placebo": "fruquintinib",
    "novo-ttf": "novo-ttf", "novo ttf": "novo-ttf", "ttfields": "novo-ttf",
    "ceralasertib": "ceralasertib", "azd6738": "ceralasertib",
    "ly3962673": "ly3962673", "kras g12d inhibitor ly3962673": "ly3962673",
    "kras g12d inhibitor": "ly3962673",
    "nab-paclitaxel": "nab-paclitaxel", "abraxane": "nab-paclitaxel",
    "gemcitabine": "gemcitabine", "gemzar": "gemcitabine",
    "mk-8353": "mk-8353", "mk8353": "mk-8353",
    "selumetinib": "selumetinib", "azd6244": "selumetinib",
    "cobimetinib": "cobimetinib", "cotellic": "cobimetinib",
    "lvgn6051": "lvgn6051",
}

REGIMEN_EXPANSIONS = {
    "folfox": {"5-fu", "leucovorin", "oxaliplatin"},
    "mfolfox6": {"5-fu", "leucovorin", "oxaliplatin"},
    "mfolfox-6": {"5-fu", "leucovorin", "oxaliplatin"},
    "modified folfox": {"5-fu", "leucovorin", "oxaliplatin"},
    "modified folfox 6": {"5-fu", "leucovorin", "oxaliplatin"},
    "folfox6": {"5-fu", "leucovorin", "oxaliplatin"},
    "folfiri": {"5-fu", "leucovorin", "irinotecan"},
    "mfolfiri": {"5-fu", "leucovorin", "irinotecan"},
    "modified folfiri": {"5-fu", "leucovorin", "irinotecan"},
    "folfoxiri": {"5-fu", "leucovorin", "oxaliplatin", "irinotecan"},
    "folfirinox": {"5-fu", "leucovorin", "oxaliplatin", "irinotecan"},
    "modified folfirinox": {"5-fu", "leucovorin", "oxaliplatin", "irinotecan"},
    "modified folfoxiri": {"5-fu", "leucovorin", "oxaliplatin", "irinotecan"},
    "capox": {"capecitabine", "oxaliplatin"}, "capeox": {"capecitabine", "oxaliplatin"},
    "xelox": {"capecitabine", "oxaliplatin"},
    "capiri": {"capecitabine", "irinotecan"}, "xeliri": {"capecitabine", "irinotecan"},
    "capeiri": {"capecitabine", "irinotecan"}, "irox": {"irinotecan", "oxaliplatin"},
}


# ============================================================
# REGIMEN NORMALIZATION
# ============================================================
def regimen_to_drug_set(regimen_str) -> Optional[frozenset]:
    """Decompose a regimen string into a canonical drug set (leucovorin stripped)."""
    if regimen_str is None or (isinstance(regimen_str, float) and pd.isna(regimen_str)):
        return None
    reg = str(regimen_str).strip().lower()
    if not reg:
        return None
    reg = re.sub(r"\([^)]*\)", "", reg)          # drop parentheticals
    for mod in ("adjuvant", "rechallenge", "monotherapy", "single agent", "modified", "maintenance", "neoadjuvant"):
        reg = re.sub(r"\b" + mod + r"\b", "", reg)
    # arrow notation -> take first stage only
    for arrow in ("\u2192", "->"):
        if arrow in reg:
            reg = reg.split(arrow)[0]
    reg = reg.replace(" + ", "+").replace(" / ", "+").replace("/", "+")
    reg = re.sub(r",\s*", "+", reg)
    reg = re.sub(r"\s+plus\s+", "+", reg)
    parts = [p.strip().strip("-").strip() for p in reg.split("+") if p.strip()]

    expanded = []
    for part in parts:
        if part in REGIMEN_EXPANSIONS or part in SYNONYMS:
            expanded.append(part)
        else:
            tokens = part.split()
            if len(tokens) > 1:
                joined = "".join(tokens)
                if joined in REGIMEN_EXPANSIONS or joined in SYNONYMS:
                    expanded.append(joined)
                elif len(tokens) == 2 and re.match(r"^\d+$", tokens[1]):
                    with_num = tokens[0] + tokens[1]
                    if with_num in REGIMEN_EXPANSIONS or with_num in SYNONYMS:
                        expanded.append(with_num)
                    elif tokens[0] in REGIMEN_EXPANSIONS or tokens[0] in SYNONYMS:
                        expanded.append(tokens[0])
                    else:
                        expanded.extend(tokens)
                else:
                    expanded.extend(tokens)
            else:
                expanded.append(part)

    drugs = set()
    for part in expanded:
        part = part.strip()
        if not part:
            continue
        if part in REGIMEN_EXPANSIONS:
            drugs.update(REGIMEN_EXPANSIONS[part])
        else:
            drugs.add(SYNONYMS.get(part, part))
    for junk in ("leucovorin", "placebo", "protocol", "trial", "inv-", ""):
        drugs.discard(junk)
    return frozenset(drugs) if drugs else None


def jaccard(a, b) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ============================================================
# DATE HELPERS
# ============================================================
def parse_date(val) -> Optional[datetime]:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, pd.Timestamp):
        dt = val.to_pydatetime()
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    if isinstance(val, datetime):
        return val.replace(tzinfo=None) if val.tzinfo else val
    s = str(val).strip()
    if not s or s.lower() in ("none", "null", "nat", "nan", "ongoing", ""):
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S", "%m/%d/%y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:10], fmt) if "H" not in fmt else datetime.strptime(s[:19], fmt)
        except (ValueError, IndexError):
            continue
    try:
        parsed = pd.to_datetime(s, errors="coerce")
        if pd.notna(parsed):
            dt = parsed.to_pydatetime()
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except Exception:
        pass
    return None


def date_proximity(a, b, max_days) -> float:
    if a is None or b is None:
        return 0.0
    diff = abs((a - b).days)
    return 0.0 if diff >= max_days else 1.0 - (diff / max_days)


def dates_match(a, b, tol) -> Optional[bool]:
    """None/None -> True; one None -> False; else within tolerance."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs((a - b).days) <= tol


# ============================================================
# FIELD NORMALIZERS
# ============================================================
def normalize_mrn(mrn) -> str:
    if mrn is None or (isinstance(mrn, float) and pd.isna(mrn)):
        return ""
    s = str(mrn).strip()
    try:
        return str(int(float(s)))
    except (ValueError, TypeError):
        return s.lstrip("0") or "0"


def norm_lot_label(label) -> Optional[str]:
    """'aLoT 1.0' / 'a1.0' / 'eLoT2.1' -> 'a1.0' / 'e2.1'."""
    if not label or pd.isna(label):
        return None
    s = str(label).strip().lower().replace(" ", "").replace("lot", "")
    return s or None


def norm_setting(s) -> Optional[str]:
    if not s or pd.isna(s):
        return None
    s = str(s).strip().lower()
    if s.startswith("1bii"):
        return "1bii"
    if s.startswith("1bi"):
        return "1bi"
    if s.startswith("1a"):
        return "1a"
    return s or None


def norm_intent(s) -> Optional[str]:
    if not s or pd.isna(s):
        return None
    s = str(s).strip().lower()
    if s.startswith("2a") or "curative" in s and "non" not in s:
        return "2a"
    if s.startswith("2b") or "non-curative" in s or "noncurative" in s:
        return "2b"
    return s or None


def norm_reason(s) -> Optional[str]:
    """Extract the 8x.y code prefix from reason_for_switching."""
    if s is None or pd.isna(s):
        return None
    s = str(s).strip().lower()
    if not s or s in ("none", "null"):
        return None
    m = re.match(r"(8[a-f](?:\.i{1,2})?)", s)
    return m.group(1) if m else s


def norm_flag(v) -> Optional[int]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        s = str(v).strip().lower()
        if s in ("1", "true", "yes"):
            return 1
        if s in ("0", "false", "no"):
            return 0
        return None


# ============================================================
# NEEDLEMAN-WUNSCH ALIGNMENT
# ============================================================
def _composite(a: Dict, b: Dict) -> float:
    drug_sim = jaccard(a["drugs"], b["drugs"]) if a["drugs"] and b["drugs"] else 0.0
    start_sim = date_proximity(a["start"], b["start"], 60)
    end_sim = date_proximity(a["stop"], b["stop"], 90)
    return W_DRUG * drug_sim + W_START * start_sim + W_END * end_sim


def _align_score(sim: float) -> float:
    if sim >= 0.5:
        return sim
    if sim >= 0.2:
        return sim * 0.5 - 0.1
    return -0.4


def needleman_wunsch(gs: List[Dict], llm: List[Dict],
                     gap: float = GAP_PENALTY) -> List[Tuple[Optional[int], Optional[int]]]:
    """Global alignment. Returns [(gs_idx|None, llm_idx|None), ...]."""
    n, m = len(gs), len(llm)
    if n == 0 and m == 0:
        return []
    if n == 0:
        return [(None, j) for j in range(m)]
    if m == 0:
        return [(i, None) for i in range(n)]

    S = np.zeros((n + 1, m + 1))
    for i in range(1, n + 1):
        S[i][0] = S[i - 1][0] + gap
    for j in range(1, m + 1):
        S[0][j] = S[0][j - 1] + gap
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            a = _align_score(_composite(gs[i - 1], llm[j - 1]))
            S[i][j] = max(S[i - 1][j - 1] + a, S[i - 1][j] + gap, S[i][j - 1] + gap)

    aln = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            a = _align_score(_composite(gs[i - 1], llm[j - 1]))
            cur = S[i][j]
            if abs(cur - (S[i - 1][j - 1] + a)) < 1e-9:
                aln.append((i - 1, j - 1)); i -= 1; j -= 1
            elif abs(cur - (S[i - 1][j] + gap)) < 1e-9:
                aln.append((i - 1, None)); i -= 1
            else:
                aln.append((None, j - 1)); j -= 1
        elif i > 0:
            aln.append((i - 1, None)); i -= 1
        else:
            aln.append((None, j - 1)); j -= 1
    aln.reverse()
    return aln


# ============================================================
# LONG-FORMAT LINE EXTRACTION
# ============================================================
def extract_lines(df: pd.DataFrame, mrn_norm: str) -> List[Dict]:
    """Extract episodes for one patient from a LONG-format table, sorted by start_date."""
    rows = df[df["mrn_norm"] == mrn_norm]
    if rows.empty:
        return []
    lines = []

    def _blank(v):
        return v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == ""

    for _, r in rows.iterrows():
        # Skip empty placeholder rows (a patient for whom the agent emitted zero episodes
        # is written as one all-null row). Dropping it lets that patient's gold lines count
        # as genuine misses instead of injecting a phantom 'agent_extra' line that hurts PPV.
        if _blank(r.get("systemic_anticancer_agents")) and _blank(r.get("enlist_lot_label")):
            continue
        lines.append({
            "regimen": r.get("systemic_anticancer_agents"),
            "drugs": regimen_to_drug_set(r.get("systemic_anticancer_agents")),
            "maintenance": r.get("maintenance_regimens"),
            "lot_label": r.get("enlist_lot_label"),
            "lot_change_type": r.get("lot_change_type"),
            "setting": r.get("clinical_setting"),
            "intent": r.get("treatment_intent"),
            "start": parse_date(r.get("start_date")),
            "stop": parse_date(r.get("stop_date")),
            "first_pd": parse_date(r.get("first_pd_date")),
            "censor_date": parse_date(r.get("censor_date")),
            "pfs_flag": norm_flag(r.get("pfs_censor_flag")),
            "pfs_reason": r.get("pfs_censor_reason"),
            "reason_switch": r.get("reason_for_switching"),
            "sact_cytotoxic": norm_flag(r.get("sact_cytotoxic")),
            "sact_targeted": norm_flag(r.get("sact_targeted")),
            "sact_immunotherapy": norm_flag(r.get("sact_immunotherapy")),
            "sact_investigational": norm_flag(r.get("sact_investigational")),
            "local_modality": r.get("local_therapy_1_modality"),
            "local_date": parse_date(r.get("local_therapy_1_date")),
        })
    # sort by start date (None last)
    lines.sort(key=lambda x: (x["start"] is None, x["start"] or datetime.max))
    return lines


# ============================================================
# FIELD COMPARISON
# ============================================================
# (field_name, kind, tolerance) -- kind drives how we compare
FIELD_SPECS = [
    ("regimen", "regimen", None),
    ("maintenance", "regimen_nullable", None),
    ("lot_label", "norm_lot", None),
    ("lot_change_type", "istr", None),
    ("setting", "norm_setting", None),
    ("intent", "norm_intent", None),
    ("start", "date", 14),
    ("stop", "date", 30),
    ("first_pd", "date", 14),
    ("censor_date", "date", 30),
    ("pfs_flag", "num", None),
    ("pfs_reason", "istr", None),
    ("reason_switch", "norm_reason", None),
    ("sact_cytotoxic", "num", None),
    ("sact_targeted", "num", None),
    ("sact_immunotherapy", "num", None),
    ("sact_investigational", "num", None),
    ("local_modality", "istr", None),
    ("local_date", "date", 30),
]


def _istr(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip().lower()
    return s or None


def compare_field(kind, tol, gs_val, llm_val) -> Optional[bool]:
    if kind == "regimen":
        a, b = regimen_to_drug_set(gs_val), regimen_to_drug_set(llm_val)
        if not a or not b:
            return None
        return jaccard(a, b) >= REGIMEN_LENIENT_JACCARD
    if kind == "regimen_nullable":
        a, b = regimen_to_drug_set(gs_val), regimen_to_drug_set(llm_val)
        if not a and not b:
            return True
        if not a or not b:
            return False
        return jaccard(a, b) >= REGIMEN_LENIENT_JACCARD
    if kind == "date":
        return dates_match(gs_val, llm_val, tol)  # values already parsed
    if kind == "num":
        if gs_val is None and llm_val is None:
            return True
        if gs_val is None or llm_val is None:
            return False
        return gs_val == llm_val
    if kind == "norm_lot":
        return norm_lot_label(gs_val) == norm_lot_label(llm_val)
    if kind == "norm_setting":
        return norm_setting(gs_val) == norm_setting(llm_val)
    if kind == "norm_intent":
        return norm_intent(gs_val) == norm_intent(llm_val)
    if kind == "norm_reason":
        return norm_reason(gs_val) == norm_reason(llm_val)
    if kind == "istr":
        return _istr(gs_val) == _istr(llm_val)
    return None
