"""
Stage 3 -- EnLiST extraction agent (GPT-5.6 Sol = adopted default; Opus / GPT-5.6 Terra are variants), COMBINED LoT + PFS.

Per patient, pre-loads the orientation (secure_onchx events + treatment summaries + CTH note
blocks) + the pre-classified imaging trajectory + a clinical-notes index. The agent retrieves
targeted excerpts via get_notes, applies the EnLiST guidelines, and emits an ordered SACT episode
timeline (LoT + PFS) tagged with a machine-readable change_trigger; a deterministic post-processor
(notation.py) assigns X.Y labels and gold-exact formatting. Output = enlist_agent_timeline.
(A LoT/PFS split-agent variant was evaluated and shelved; the combined agent is the adopted design.)
The agent retrieves targeted excerpts via a single get_notes tool.

Output enlist_agent_timeline: one row per LoT/subline (gold-comparable columns) + audit.
"""
import json
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from pyspark.sql import Row, functions as F, types as T
from transforms.api import transform, Input, Output, configure
from palantir_models.transforms import GenericCompletionLanguageModelInput

from myproject import config as C
from myproject import llm
from myproject import notation
from myproject.prompts.enlist_prompts import SYSTEM_PROMPT

PROMPT_VERSION = "enlist_v2.7_sol_evidence"
MODEL_NAME = "gpt-5-6-sol"
MAX_AGENT_TURNS = 18   # +3 headroom for the one self-critique pass and any gap-fill retrieval

# Fallback model config for the adopted default (GPT-5.6 Sol). run_stage3() OVERRIDES all of
# these per variant (Opus / GPT-5 Terra / GPT-5 Sol) before the ThreadPoolExecutor starts, so
# these module globals are never read un-set. Each transform runs in its OWN process, so
# mutating them is safe (set once, then only read by worker threads).
_MAX_TOKENS = C.GPT5_MAX_TOKENS
_TEMPERATURE = C.GPT5_TEMPERATURE
_PRICING = C.MODEL_PRICING.get(C.MODEL_RID_GPT5_SOL)

# Treating-clinician author roles for cPD corroboration (coordinators / null excluded). The
# get_notes author_types shortcut "clinician" expands to this set.
CLINICIAN_AUTHOR_TYPES = ("physician", "physician assistant", "nurse practitioner", "fellow", "resident")

# One-time SELF-CRITIQUE injected after the agent's FIRST FINAL_ANSWER (the draft). It keeps tool
# access, so a gap can be filled by reading a note, then the agent re-emits a corrected answer.
SELF_CRITIQUE = (
    "SELF-CRITIQUE (one pass, then finalize). The FINAL_ANSWER above is a DRAFT. ASSUME it contains "
    "at least one error and verify it against this checklist. For any GAP, either issue a get_notes "
    "TOOL_CALL to check the note(s) or correct the draft. Do NOT invent lines or dates - only change "
    "something you can support from a retrieved note.\n"
    "A) LINES / DATES / SEGMENTATION: for each transition between consecutive episodes, READ the "
    "clinical notes bracketing the switch date to PIN the exact start/stop (e.g. a note that still "
    "shows the old regimen, then a later note documenting the switch). Confirm treatment_intent from "
    "the stated plan at line start. Check OVER-segmentation (same backbone resumed / de-escalated = "
    "ONE line) and UNDER-segmentation (two genuinely different regimens merged into one).\n"
    "B) PFS (progression vs censoring): for each line re-apply the TEMPORAL RACE - within the line "
    "window, whichever came FIRST decides: a clinician-corroborated cPD (=> PFS event) or a local "
    "therapy (=> censor 'Local therapy no PD'). cPD corroboration MUST come from a TREATING-CLINICIAN "
    "note (Physician / PA / NP / Fellow / Resident - use get_notes with author_types=[\"clinician\"]), "
    "NEVER a coordinator note or imaging alone. A switch with no corroborated cPD = censor "
    "'Switched no PD'; a completed line that later recurs with no intervening line = event at the "
    "recurrence. Set first_pd_date (flag=1) or censor_date + enum pfs_censor_reason (flag=0).\n"
    "When every item passes, output your corrected FINAL_ANSWER."
)
NEAR_DATE_DEFAULT_WINDOW = 30
GET_NOTES_MAX = 8            # max NEW excerpts returned per get_notes call
PREVIEW = 90
MAX_ENTRIES = 30

_ENUM_CHANGE = {"New", "Modified", "Same"}
_ENUM_U = {"U0", "U1", "U2", "U3", "U4"}


# --------------------------------------------------------------------- helpers
def _s(x: Any) -> str:
    return "" if x is None else str(x)


def _clean(x: Any) -> Optional[str]:
    if x is None:
        return None
    x = str(x).strip()
    return x or None


def _trunc(t: str, n: int) -> str:
    t = t or ""
    return t if len(t) <= n else t[:n] + "..."


def _int01(v: Any) -> Optional[int]:
    try:
        iv = int(v)
        return iv if iv in (0, 1) else None
    except (TypeError, ValueError):
        return None


def _jlist(v: Any) -> Optional[str]:
    if isinstance(v, list) and v:
        return json.dumps(v, ensure_ascii=False)
    return None


def _date10(x: Any) -> str:
    return _s(x)[:10]


# --------------------------------------------------------- JSON action parsing
def _extract_json(text: str, marker: str) -> Optional[str]:
    idx = text.find(marker)
    if idx == -1:
        return None
    start = text.find("{", idx + len(marker))
    if start == -1:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _parse_tool_call(text: str) -> Optional[Tuple[str, Dict]]:
    js = _extract_json(text, "TOOL_CALL:")
    if not js:
        return None
    try:
        p = json.loads(js)
        if p.get("name"):
            return p["name"], (p.get("args") or {})
    except Exception:
        pass
    return None


def _truncate_at_tool_call(text: str) -> str:
    js = _extract_json(text, "TOOL_CALL:")
    if js:
        return text[: text.find(js) + len(js)]
    return text


def _parse_final(text: str) -> Optional[Dict[str, Any]]:
    js = _extract_json(text, "FINAL_ANSWER:")
    if js:
        try:
            return json.loads(js)
        except Exception:
            pass
    # salvage: patient scalars + complete episode objects from a truncated answer
    idx = text.find("FINAL_ANSWER:")
    seg = text[idx:] if idx >= 0 else text
    result: Dict[str, Any] = {}
    import re
    for key in ("mrn", "cancer_type", "initial_diagnosis_date", "stage_initial",
                "metastatic_diagnosis_date", "deceased_date", "last_follow_up_date"):
        m = re.search(r'"%s"\s*:\s*("(?:[^"\\]|\\.)*"|null)' % key, seg)
        if m:
            v = m.group(1)
            result[key] = None if v == "null" else json.loads(v)
    ep_i = seg.find('"episodes"')
    if ep_i != -1:
        arr = seg.find("[", ep_i)
        if arr != -1:
            eps = llm.salvage_objects(seg[arr:])
            if eps:
                result["episodes"] = eps
    return result or None


# ------------------------------------------------------------- patient bundle
class Bundle:
    def __init__(self, mrn: str):
        self.mrn = mrn
        self.cancer_type = None
        self.death_date = None
        self.cth_onc: Optional[str] = None        # latest note's cth_oncologic_history
        self.cth_treat: Optional[str] = None       # longest cth_treatment_history
        self.cth_chemo: Optional[str] = None       # consolidated cth_chemotherapy_summary
        self.onchx: List[Dict] = []
        self.treatment: List[Dict] = []
        self.notes: List[Dict] = []       # from enlist_notes_prepped
        self.imaging: List[Dict] = []      # from enlist_imaging_classified
        # treating-institution pathology reports (specimen collection date + report type only).
        # A dated, NON-retrievable timeline used to PIN local-therapy / biopsy dates. It is a
        # SUBSET (treating-institution pathology only): absence of a report does NOT mean a procedure did not
        # happen (it may be outside/OSH). See fmt_pathology().
        self.pathology: List[Dict] = []
        self.by_id: Dict[str, Dict] = {}
        self.returned_ids: set = set()     # note_ids already served (never read twice)

    def index(self):
        for n in self.notes:
            nid = _s(n.get("note_surrogate_pkey"))
            if nid:
                self.by_id[nid] = {"date": _date10(n.get("note_service_datetime")),
                                   "type": _s(n.get("note_type_name")),
                                   "author": _s(n.get("authoring_provider_type_name")),
                                   "text": n.get("relevant_excerpt") or n.get("note_text")}
        # secure_onchx events and treatment_summ rows are shown IN FULL in the orientation, so they
        # are intentionally NOT retrievable by id. Imaging is only summarized (class) in the
        # trajectory, so imaging stays retrievable by note_id to read the full impression.
        for im in self.imaging:
            nid = _s(im.get("note_id"))
            if nid:
                self.by_id[nid] = {"date": _date10(im.get("exam_date")),
                                   "type": "Imaging " + _s(im.get("modality")),
                                   "text": im.get("impression")}

    def latest_date(self) -> Optional[str]:
        ds = []
        for n in self.notes:
            ds.append(_date10(n.get("note_service_datetime")))
        for im in self.imaging:
            ds.append(_date10(im.get("exam_date")))
        ds = [d for d in ds if d]
        return max(ds) if ds else None

    # ---- formatting blocks ----
    def fmt_onchx(self) -> str:
        """secure_onchx orientation: the History Overview summary first, then the History Event
        Details ordered strictly by event_note_number (each: #num | start -> end | type | detail)."""
        if not self.onchx:
            return "=== ONCOLOGY HISTORY (secure_onchx) ===\n(none available)"
        overview, events = [], []
        for o in self.onchx:
            ntype = _s(o.get("note_type_name")).strip().lower()
            evtype = _s(o.get("event_note_type_name")).strip()
            if ntype == "history overview" or (not evtype and o.get("event_note_number") is None):
                txt = _s(o.get("note_text")).strip()
                if txt:
                    overview.append(txt)
            else:
                events.append(o)
        out = []
        if overview:
            out.append("=== ONCOLOGY HISTORY OVERVIEW (secure_onchx; structured diagnosis/mets/"
                       "genetics summary) ===\n" + "\n\n".join(overview))
        if events:
            def _evn(o):
                try:
                    return int(o.get("event_note_number"))
                except (TypeError, ValueError):
                    return 10 ** 9

            events.sort(key=lambda o: (_evn(o), _date10(o.get("note_start_date"))))
            lines = [f"=== ONCOLOGY HISTORY EVENTS (secure_onchx; {len(events)} events, ordered by "
                     "event_note_number; authoritative dated timeline) ===",
                     "event# | start -> end | event_type | detail"]
            for o in events:
                num = _s(o.get("event_note_number"))
                st = _date10(o.get("note_start_date")) or "?"
                en = _date10(o.get("note_end_date")) or "?"
                ev = _s(o.get("event_note_type_name"))
                txt = _trunc(_s(o.get("note_text")).replace(chr(10), " ").strip(), 600)
                lines.append(f"#{num} | {st} -> {en} | {ev} | {txt}")
            out.append("\n".join(lines))
        return "\n\n".join(out) if out else "=== ONCOLOGY HISTORY (secure_onchx) ===\n(empty)"

    def fmt_treatment(self) -> str:
        """treatment_summ orientation: treating-institution-administered records (systemic + surgery + radiation).
        Shows the regimen/procedure (note_text) + record category (treatment_type_name) + dates.
        Deliberately omits treatment_technique_description so its intent wording does not bias the
        agent's own treatment_intent call. This source is a SUBSET (misses outside/pre-referral lines)."""
        if not self.treatment:
            return ("=== TREATING-INSTITUTION TREATMENT SUMMARY (treatment_summ) ===\n"
                    "(none; treating-institution-administered only - trust OncHx for line existence)")
        rows = sorted(self.treatment, key=lambda t: _date10(t.get("earliest_treatment_datetime")))
        lines = [f"=== TREATING-INSTITUTION TREATMENT SUMMARY (treatment_summ; {len(rows)} records "
                 "- treating-institution-administered "
                 "systemic + surgery + radiation; a SUBSET, misses outside lines) ===",
                 "regimen/procedure | category | start -> end"]
        for t in rows:
            reg = _trunc(_s(t.get("note_text")).replace(chr(10), " ").strip(), 160)
            cat = _s(t.get("treatment_type_name"))
            st = _date10(t.get("earliest_treatment_datetime")) or "?"
            en = _date10(t.get("latest_treatment_datetime")) or "?"
            lines.append(f"- {reg} | {cat} | {st} -> {en}")
        return "\n".join(lines)

    def fmt_cth_blocks(self) -> str:
        """CTH orientation blocks from the clinical notes (Stage 1): the latest Oncology-History
        note, the most-complete Treatment-History narrative, and the deduplicated Chemotherapy
        summary. Authoritative for the regimen sequence, including outside/pre-referral lines."""
        parts = []
        if self.cth_onc:
            parts.append("=== ONCOLOGY HISTORY NOTE (latest clinical-note snapshot) ===\n" + str(self.cth_onc))
        if self.cth_treat:
            parts.append("=== TREATMENT HISTORY (most-complete clinical-note narrative; numbered "
                         "course list, incl. outside/prior regimens) ===\n" + str(self.cth_treat))
        if self.cth_chemo:
            parts.append("=== CHEMOTHERAPY SUMMARY (deduplicated per-regimen blocks) ===\n" + str(self.cth_chemo))
        return "\n\n".join(parts)

    def fmt_imaging(self) -> str:
        if not self.imaging:
            return "=== IMAGING TRAJECTORY ===\n(no classified imaging)"
        rows = sorted(self.imaging, key=lambda r: _date10(r.get("exam_date")))
        lines = [f"=== IMAGING TRAJECTORY ({len(rows)} scans; class = PROGRESSION_CANDIDATE / "
                 "NO_PROGRESSION / INDETERMINATE / NOT_RELEVANT) ===",
                 "PROGRESSION_CANDIDATE = radiologic CANDIDATE progression (new/worsening malignancy); "
                 "needs treating-clinician corroboration to become a PFS event. NO_PROGRESSION = "
                 "stable / response / no active disease. INDETERMINATE = equivocal or inadequate "
                 "comparison. NOT_RELEVANT = does not assess the indexed cancer. "
                 "Call get_notes on a scan's Note_ID to read its full impression.",
                 f"{'Date':<12}| {'Note_ID':<12}| Cls"]
        for r in rows:
            lines.append(
                f"{_date10(r.get('exam_date')):<12}| {_s(r.get('note_id')):<12}| {_s(r.get('imaging_class'))}"
            )
        return "\n".join(lines)

    def fmt_pathology(self) -> str:
        """treating-institution pathology / surgical-specimen timeline (Date | Type ONLY).

        Each row's date is the SPECIMEN COLLECTION date = the procedure date (~ surgery/biopsy
        date). Used to PIN local-therapy / biopsy dates and PFS censor dates. This is a SUBSET
        (treating-institution pathology only): absence of a report does NOT mean a procedure did not
        happen (it may be outside/OSH). Reports are NOT separately retrievable via get_notes."""
        if not self.pathology:
            return ("=== PATHOLOGY / SURGICAL SPECIMEN TIMELINE ===\n"
                    "(no treating-institution pathology on record; outside/OSH procedures still count if a note "
                    "documents them)")
        rows = sorted(self.pathology, key=lambda r: _date10(r.get("collected_date")))
        lines = [
            f"=== PATHOLOGY / SURGICAL SPECIMEN TIMELINE ({len(rows)} reports) ===",
            "Date = SPECIMEN COLLECTION date = the procedure date (~ surgery/biopsy date). Use it "
            "to PIN a local-therapy / biopsy date and the 'Local therapy no PD' censor date. Treating-institution "
            "pathology ONLY (a SUBSET): a MISSING report does NOT mean the procedure did not happen "
            "(it may be outside/OSH) - never drop a note-documented procedure for lack of one. Not "
            "separately retrievable.",
            f"{'Date':<12}| Specimen / Report Type",
        ]
        for r in rows:
            lines.append(f"{_date10(r.get('collected_date')):<12}| {_s(r.get('path_type'))}")
        return "\n".join(lines)

    def fmt_notes_index(self) -> str:
        if not self.notes:
            return "=== CLINICAL NOTES INDEX ===\n(none)"
        rows = sorted(self.notes, key=lambda n: _date10(n.get("note_service_datetime")))
        lines = [f"=== CLINICAL NOTES INDEX ({len(rows)} notes; call get_notes for the excerpt) ===",
                 f"{'Date':<12}| {'Note_ID':<12}| {'Type':<22}| {'Author':<20}| Excerpt preview"]
        for n in rows:
            lines.append(
                f"{_date10(n.get('note_service_datetime')):<12}| {_s(n.get('note_surrogate_pkey')):<12}| "
                f"{_s(n.get('note_type_name'))[:21]:<22}| {_s(n.get('authoring_provider_type_name'))[:19]:<20}| "
                f"{_trunc(_s(n.get('relevant_excerpt')).replace(chr(10), ' ').strip(), PREVIEW)}"
            )
        return "\n".join(lines)

    # ---- unified retrieval tool ----
    def tool_get_notes(self, note_ids=None, near_date=None,
                       window_days=NEAR_DATE_DEFAULT_WINDOW, author_types=None) -> str:
        """Merged retrieval: fetch by note_ids (clinical notes + imaging) and/or by a near_date
        +/- window (clinical notes). Optional author_types filters to notes whose
        authoring_provider_type matches (case-insensitive substring); the shortcut "clinician"
        expands to Physician/PA/NP/Fellow/Resident. Returns de-duplicated excerpts in
        CHRONOLOGICAL order; a note_id is never served twice."""
        if not note_ids and not near_date:
            return "[TOOL ERROR] provide note_ids and/or near_date (YYYY-MM-DD)."
        try:
            window_days = int(window_days)
        except (TypeError, ValueError):
            window_days = NEAR_DATE_DEFAULT_WINDOW

        selected: Dict[str, Dict] = {}

        # (a) explicit IDs — clinical notes or imaging reads (onchx/treatment are orientation-only)
        if note_ids:
            if isinstance(note_ids, str):
                note_ids = [note_ids]
            for nid in note_ids:
                nid = _s(nid)
                rec = self.by_id.get(nid)
                if rec is None:
                    selected[nid] = {"id": nid, "missing": True, "date": "", "type": "", "author": "", "text": None}
                else:
                    selected.setdefault(nid, {"id": nid, "date": rec.get("date"), "type": rec.get("type"),
                                              "author": rec.get("author", ""), "text": rec.get("text")})

        # (b) date window — over the clinical (nonsensitive/sensitive) prepped notes
        if near_date:
            d0 = _pdate(near_date)
            if not d0:
                return f"[TOOL ERROR] bad near_date '{near_date}'; use YYYY-MM-DD."
            for n in self.notes:
                nd = _pdate(_date10(n.get("note_service_datetime")))
                if nd and abs((nd - d0).days) <= window_days:
                    nid = _s(n.get("note_surrogate_pkey"))
                    selected.setdefault(nid, {"id": nid,
                                              "date": _date10(n.get("note_service_datetime")),
                                              "type": _s(n.get("note_type_name")),
                                              "author": _s(n.get("authoring_provider_type_name")),
                                              "text": n.get("relevant_excerpt") or n.get("note_text")})

        # optional author_types filter (case-insensitive substring on authoring_provider_type);
        # "clinician" expands to the treating-clinician roles. Missing ids are kept (NOT FOUND).
        if author_types:
            if isinstance(author_types, str):
                author_types = [author_types]
            ats = []
            for a in author_types:
                al = _s(a).lower()
                if al == "clinician":
                    ats.extend(CLINICIAN_AUTHOR_TYPES)
                elif al:
                    ats.append(al)
            if ats:
                selected = {k: v for k, v in selected.items()
                            if v.get("missing") or any(t in _s(v.get("author")).lower() for t in ats)}

        already = sorted(k for k in selected if k in self.returned_ids)
        new_items = [v for k, v in selected.items() if k not in self.returned_ids]
        if not new_items:
            msg = "(no NEW notes for this request"
            if already:
                msg += f"; already provided: {', '.join(already)}"
            return msg + ")"

        new_items.sort(key=lambda v: _s(v.get("date")))   # chronological
        truncated = len(new_items) > GET_NOTES_MAX
        new_items = new_items[:GET_NOTES_MAX]

        parts = []
        for v in new_items:
            self.returned_ids.add(v["id"])
            if v.get("missing"):
                parts.append(f"[id={v['id']}] NOT FOUND - check the id.")
            else:
                auth = _s(v.get("author"))
                head = f"[id={v['id']} | {v.get('date')} | {v.get('type')}" + (f" | {auth}" if auth else "") + "]"
                parts.append(f"{head}\n{_s(v.get('text'))}")
        out = "\n\n---\n\n".join(parts)
        notes_tail = []
        if truncated:
            notes_tail.append("(+more in window than shown; narrow window_days or use author_types)")
        if already:
            notes_tail.append(f"(already provided earlier, not repeated: {', '.join(already)})")
        return out + ("\n\n" + " ".join(notes_tail) if notes_tail else "")


def _pdate(x):
    try:
        return datetime.datetime.strptime(_s(x)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _exec_tool(name: str, args: Dict, bundle: Bundle) -> str:
    try:
        if name in ("get_notes", "get_note_text", "get_notes_near_date"):
            # unified retrieval; old tool names are accepted as aliases.
            near_date = args.get("near_date") or args.get("date")
            return bundle.tool_get_notes(
                note_ids=args.get("note_ids"),
                near_date=near_date,
                window_days=args.get("window_days") or NEAR_DATE_DEFAULT_WINDOW,
                author_types=args.get("author_types"),
            )
        return f"[TOOL ERROR] unknown tool '{name}'. Only tool: get_notes."
    except Exception as e:  # noqa: BLE001
        return f"[TOOL ERROR] {name} failed: {str(e)[:400]}"


# ------------------------------------------------------------------ agent loop
def run_agent(bundle: Bundle, model) -> Dict[str, Any]:
    preload = (
        f"{SYSTEM_PROMPT}\n\n"
        "==========================================================\n"
        f"PRE-LOADED DATA FOR MRN: {bundle.mrn}\n"
        "==========================================================\n\n"
        f"{bundle.fmt_onchx()}\n\n"
        f"{bundle.fmt_treatment()}\n\n"
        + (bundle.fmt_cth_blocks() + "\n\n" if bundle.fmt_cth_blocks() else "")
        + f"{bundle.fmt_imaging()}\n\n{bundle.fmt_pathology()}\n\n{bundle.fmt_notes_index()}\n\n"
        "=== PATIENT METADATA ===\n"
        f"MRN: {bundle.mrn}\ncancer_type: {bundle.cancer_type or 'unknown'}\n"
        f"latest_note_date: {bundle.latest_date() or 'unknown'}\n"
        f"verified_death_date: {bundle.death_date or 'none on record'}\n\n"
        "---\nExtract the complete EnLiST SACT timeline (LoT + PFS). Begin from the OncHx to identify "
        "all systemic regimens and their sequence, then use targeted retrieval to resolve settings, "
        "intent, cPD and reasons. If verified_death_date is provided, set deceased_date to it. "
        "When done, output FINAL_ANSWER.\n\nAssistant:"
    )
    conversation = "Human: " + preload
    cum_p = cum_c = cum_t = 0
    tool_log: List[Dict] = []
    final = None
    turns = 0
    critiqued = False

    for turn in range(MAX_AGENT_TURNS):
        tr = llm.complete_with_trace(model, "", conversation, _MAX_TOKENS,
                                     temperature=_TEMPERATURE, pricing=_PRICING)
        txt = tr["text"]
        cum_p += tr.get("prompt_tokens") or 0
        cum_c += tr.get("completion_tokens") or 0
        cum_t += tr.get("total_tokens") or 0
        turns += 1

        tc = _parse_tool_call(txt)
        if tc:
            conversation += " " + _truncate_at_tool_call(txt)
            name, args = tc
            tool_log.append({"turn": turn + 1, "tool": name, "args": args})
            result = _exec_tool(name, args, bundle)
            conversation += (f"\n\nHuman: TOOL_RESULT:\n{result}\n\n"
                             "Continue. Call another tool if needed, or output FINAL_ANSWER.\n\nAssistant:")
        elif "FINAL_ANSWER:" in txt:
            conversation += " " + txt
            if not critiqued:
                # First FINAL_ANSWER = DRAFT. Run one self-critique pass (with tool access) before
                # accepting it; the agent may retrieve to fill a gap, then re-emit a corrected answer.
                critiqued = True
                conversation += "\n\nHuman: " + SELF_CRITIQUE + "\n\nAssistant:"
                continue
            final = _parse_final(txt)
            break
        else:
            conversation += " " + txt
            conversation += ("\n\nHuman: Please call a tool or output FINAL_ANSWER.\n\nAssistant:")

    if final is None:
        tr = llm.complete_with_trace(model, "", conversation + "\n\nHuman: Max turns reached. Output FINAL_ANSWER now.\n\nAssistant:",
                                     _MAX_TOKENS, temperature=_TEMPERATURE, pricing=_PRICING)
        final = _parse_final(tr["text"])
        cum_p += tr.get("prompt_tokens") or 0
        cum_c += tr.get("completion_tokens") or 0
        cum_t += tr.get("total_tokens") or 0
        turns += 1

    return {"final": final or {}, "turns": turns, "tool_calls": tool_log,
            "prompt_tokens": cum_p, "completion_tokens": cum_c, "total_tokens": cum_t,
            "transcript": conversation}


# --------------------------------------------------------------- row building
def _episode_row(ctx: Dict, ep: Dict, meta: Dict) -> Row:
    def lt(i, k):
        return ep.get(f"local_therapy_{i}_{k}")
    return Row(
        mrn=ctx.get("mrn"),
        cancer_type=_clean(ctx.get("cancer_type")),
        initial_diagnosis_date=_clean(ctx.get("initial_diagnosis_date")),
        stage_initial=_clean(ctx.get("stage_initial")),
        metastatic_diagnosis_date=_clean(ctx.get("metastatic_diagnosis_date")),
        deceased_date=_clean(ctx.get("deceased_date")),
        last_follow_up_date=_clean(ctx.get("last_follow_up_date")),
        review_time_minutes=_clean(ctx.get("review_time_minutes")),
        entry_order=int(ep["order"]) if ep.get("order") is not None else None,
        clinical_setting=notation.fmt_clinical_setting(ep.get("clinical_setting")),
        treatment_intent=notation.fmt_treatment_intent(ep.get("treatment_intent")),
        enlist_lot_label=_clean(ep.get("enlist_lot_label")),
        enlist_composite_label=_clean(ep.get("enlist_composite_label")),
        lot_change_type=ep.get("lot_change_type") if ep.get("lot_change_type") in _ENUM_CHANGE else None,
        change_trigger=_clean(ep.get("change_trigger")),
        notation_note=_clean(ep.get("_notation_note")),
        systemic_anticancer_agents=_clean(ep.get("systemic_anticancer_agents")),
        maintenance_regimens=_clean(ep.get("maintenance_regimens")),
        sact_cytotoxic=_int01(ep.get("sact_cytotoxic")),
        sact_targeted=_int01(ep.get("sact_targeted")),
        sact_immunotherapy=_int01(ep.get("sact_immunotherapy")),
        sact_investigational=_int01(ep.get("sact_investigational")),
        start_date=_clean(ep.get("start_date")),
        start_date_earliest=_clean(ep.get("start_date_earliest")),
        start_date_latest=_clean(ep.get("start_date_latest")),
        start_date_U=ep.get("start_date_U") if ep.get("start_date_U") in _ENUM_U else None,
        stop_date=_clean(ep.get("stop_date")),
        stop_date_earliest=_clean(ep.get("stop_date_earliest")),
        stop_date_latest=_clean(ep.get("stop_date_latest")),
        stop_date_U=ep.get("stop_date_U") if ep.get("stop_date_U") in _ENUM_U else None,
        reason_for_switching=notation.fmt_reason(ep.get("reason_for_switching")),
        pfs_censor_flag=_int01(ep.get("pfs_censor_flag")),
        first_pd_date=_clean(ep.get("first_pd_date")),
        first_pd_date_earliest=_clean(ep.get("first_pd_date_earliest")),
        first_pd_date_latest=_clean(ep.get("first_pd_date_latest")),
        first_pd_date_U=ep.get("first_pd_date_U") if ep.get("first_pd_date_U") in _ENUM_U else None,
        censor_date=_clean(ep.get("censor_date")),
        censor_date_earliest=_clean(ep.get("censor_date_earliest")),
        censor_date_latest=_clean(ep.get("censor_date_latest")),
        censor_date_U=ep.get("censor_date_U") if ep.get("censor_date_U") in _ENUM_U else None,
        pfs_censor_reason=notation.fmt_pfs_reason(ep.get("pfs_censor_reason")),
        recent_pd_date=_clean(ep.get("recent_pd_date")),
        recent_pd_date_earliest=_clean(ep.get("recent_pd_date_earliest")),
        recent_pd_date_latest=_clean(ep.get("recent_pd_date_latest")),
        recent_pd_date_U=ep.get("recent_pd_date_U") if ep.get("recent_pd_date_U") in _ENUM_U else None,
        local_therapy_1_modality=notation.fmt_local_modality(lt(1, "modality")),
        local_therapy_1_date=_clean(lt(1, "date")),
        local_therapy_1_description=_clean(lt(1, "description")),
        local_therapy_2_modality=notation.fmt_local_modality(lt(2, "modality")),
        local_therapy_2_date=_clean(lt(2, "date")),
        local_therapy_2_description=_clean(lt(2, "description")),
        local_therapy_3_modality=notation.fmt_local_modality(lt(3, "modality")),
        local_therapy_3_date=_clean(lt(3, "date")),
        local_therapy_3_description=_clean(lt(3, "description")),
        comments=_clean(ep.get("comments")),
        reasoning=_clean(ep.get("reasoning")),
        decision_evidence=_jlist(ep.get("evidence")),
        agents_source=_jlist(ep.get("systemic_anticancer_agents_source")),
        start_date_source=_jlist(ep.get("start_date_source")),
        first_pd_date_source=_jlist(ep.get("first_pd_date_source")),
        parse_status=meta.get("parse_status"),
        error_message=meta.get("error_message"),
        agent_turns=meta.get("agent_turns"),
        tool_calls_json=meta.get("tool_calls_json"),
        manual_review_flags=meta.get("manual_review_flags"),
        prompt_tokens=meta.get("prompt_tokens"),
        completion_tokens=meta.get("completion_tokens"),
        total_tokens=meta.get("total_tokens"),
        total_cost_usd=meta.get("total_cost_usd"),
        model_name=MODEL_NAME,
        prompt_version=PROMPT_VERSION,
        processed_at_utc=meta.get("processed_at_utc"),
        full_conversation_transcript=meta.get("transcript"),
    )


def _schema() -> T.StructType:
    S = lambda n: T.StructField(n, T.StringType(), True)
    I = lambda n: T.StructField(n, T.IntegerType(), True)
    D = lambda n: T.StructField(n, T.DoubleType(), True)
    return T.StructType([
        S("mrn"), S("cancer_type"), S("initial_diagnosis_date"), S("stage_initial"),
        S("metastatic_diagnosis_date"), S("deceased_date"), S("last_follow_up_date"), S("review_time_minutes"),
        I("entry_order"), S("clinical_setting"), S("treatment_intent"), S("enlist_lot_label"),
        S("enlist_composite_label"),
        S("lot_change_type"), S("change_trigger"), S("notation_note"),
        S("systemic_anticancer_agents"), S("maintenance_regimens"),
        I("sact_cytotoxic"), I("sact_targeted"), I("sact_immunotherapy"), I("sact_investigational"),
        S("start_date"), S("start_date_earliest"), S("start_date_latest"), S("start_date_U"),
        S("stop_date"), S("stop_date_earliest"), S("stop_date_latest"), S("stop_date_U"),
        S("reason_for_switching"), I("pfs_censor_flag"),
        S("first_pd_date"), S("first_pd_date_earliest"), S("first_pd_date_latest"), S("first_pd_date_U"),
        S("censor_date"), S("censor_date_earliest"), S("censor_date_latest"), S("censor_date_U"),
        S("pfs_censor_reason"),
        S("recent_pd_date"), S("recent_pd_date_earliest"), S("recent_pd_date_latest"), S("recent_pd_date_U"),
        S("local_therapy_1_modality"), S("local_therapy_1_date"), S("local_therapy_1_description"),
        S("local_therapy_2_modality"), S("local_therapy_2_date"), S("local_therapy_2_description"),
        S("local_therapy_3_modality"), S("local_therapy_3_date"), S("local_therapy_3_description"),
        S("comments"), S("reasoning"), S("decision_evidence"),
        S("agents_source"), S("start_date_source"), S("first_pd_date_source"),
        S("parse_status"), S("error_message"), I("agent_turns"), S("tool_calls_json"),
        S("manual_review_flags"), I("prompt_tokens"), I("completion_tokens"), I("total_tokens"),
        D("total_cost_usd"), S("model_name"), S("prompt_version"), S("processed_at_utc"),
        S("full_conversation_transcript"),
    ])


def _process_patient(pdata: Dict, model) -> List[Row]:
    processed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    mrn = _s(pdata.get("mrn"))
    bundle = Bundle(mrn)
    bundle.cancer_type = pdata.get("cancer_type")
    bundle.death_date = pdata.get("death_date")
    bundle.cth_onc = pdata.get("cth_onc")
    bundle.cth_treat = pdata.get("cth_treat")
    bundle.cth_chemo = pdata.get("cth_chemo")
    bundle.onchx = pdata.get("onchx") or []
    bundle.treatment = pdata.get("treatment") or []
    bundle.notes = pdata.get("notes") or []
    bundle.imaging = pdata.get("imaging") or []
    bundle.pathology = pdata.get("pathology") or []
    bundle.index()
    try:
        res = run_agent(bundle, model)
        final = res["final"]
        ctx = {
            "mrn": mrn,
            "cancer_type": final.get("cancer_type") or bundle.cancer_type,
            "initial_diagnosis_date": final.get("initial_diagnosis_date"),
            "stage_initial": final.get("stage_initial"),
            "metastatic_diagnosis_date": final.get("metastatic_diagnosis_date"),
            "deceased_date": final.get("deceased_date") or bundle.death_date,
            "last_follow_up_date": final.get("last_follow_up_date") or bundle.latest_date(),
            "review_time_minutes": final.get("review_time_minutes"),
        }
        episodes = final.get("episodes") or []
        episodes.sort(key=lambda e: (e.get("order") if e.get("order") is not None else 9999, _s(e.get("start_date"))))
        episodes = episodes[:MAX_ENTRIES]
        episodes = notation.assign_enlist_notation(episodes)  # combined: LoT labels + PFS derivation
        _, _, cost = _cost(res["prompt_tokens"], res["completion_tokens"])
        meta = {
            "parse_status": "success", "error_message": None,
            "agent_turns": res["turns"],
            "tool_calls_json": json.dumps(res["tool_calls"], ensure_ascii=False),
            "manual_review_flags": json.dumps(list(final.get("manual_review_flags") or []), ensure_ascii=False),
            "prompt_tokens": res["prompt_tokens"], "completion_tokens": res["completion_tokens"],
            "total_tokens": res["total_tokens"], "total_cost_usd": cost,
            "processed_at_utc": processed_at, "transcript": res["transcript"],
        }
        if not episodes:
            return [_episode_row(ctx, {"order": None}, meta)]
        return [_episode_row(ctx, ep, meta) for ep in episodes]
    except Exception as e:  # noqa: BLE001
        meta = {"parse_status": "failed", "error_message": str(e)[:4000], "processed_at_utc": processed_at}
        return [_episode_row({"mrn": mrn}, {"order": None}, meta)]


def _cost(pt, ct):
    p = _PRICING or {}
    if pt is None or ct is None:
        return (None, None, None)
    ic = pt / 1e6 * p.get("input_per_1m", 0.0)
    oc = ct / 1e6 * p.get("output_per_1m", 0.0)
    return (round(ic, 5), round(oc, 5), round(ic + oc, 5))


def run_stage3(ctx, output, source, notes_prepped, imaging, model, *,
               model_name, prompt_version, max_tokens, temperature, max_workers, pricing):
    """Shared Stage-3 core. Sets the active model config (module globals read by run_agent /
    _episode_row / _cost), builds per-patient orientation bundles, runs the agent in parallel,
    and writes the episode rows. Reused by the Opus transform and the GPT-5 Terra/Sol variants."""
    global MODEL_NAME, PROMPT_VERSION, _MAX_TOKENS, _TEMPERATURE, _PRICING
    MODEL_NAME, PROMPT_VERSION = model_name, prompt_version
    _MAX_TOKENS, _TEMPERATURE, _PRICING = max_tokens, temperature, pricing

    targets = [str(m).strip() for m in C.TARGET_MRNS] if C.TARGET_MRNS else None

    # skeleton: onchx + treatment_summ + pathology rows from the raw input.
    # (Pathology contributes ONLY a Date|Type specimen timeline -> local-therapy/biopsy dates.)
    sdf = source.dataframe().where(
        F.col(C.COL_DATA_SOURCE).isin([C.SRC_ONCHX, C.SRC_TX_SUMMARY, C.SRC_PATHOLOGY])
    )
    if targets:
        sdf = sdf.where(F.col(C.COL_MRN).isin(targets))
    skel_cols = [C.COL_MRN, C.COL_CANCER_TYPE, C.COL_NOTE_KEY, C.COL_NOTE_TEXT, C.COL_NOTE_TYPE,
                 C.COL_EVENT_NOTE_TYPE, C.COL_EVENT_NOTE_NUMBER, C.COL_DIAGNOSIS, C.COL_NOTE_START_DATE,
                 C.COL_NOTE_END_DATE, C.COL_TREATMENT_TYPE,
                 C.COL_TREATMENT_DESC, C.COL_TREATMENT_ORDER, C.COL_EARLIEST_TX, C.COL_LATEST_TX,
                 C.COL_NOTE_SERVICE_DT, C.COL_DEATH_DATE, C.COL_DATA_SOURCE]

    ndf = notes_prepped.dataframe()
    idf = imaging.dataframe()
    if targets:
        ndf = ndf.where(F.col("mrn").isin(targets))
        idf = idf.where(F.col("mrn").isin(targets))

    pmap: Dict[str, Dict[str, Any]] = {}

    def _p(mrn):
        return pmap.setdefault(mrn, {"mrn": mrn, "cancer_type": None, "death_date": None,
                                     "cth_onc": None, "cth_onc_date": "", "cth_treat": None,
                                     "cth_chemo": None,
                                     "onchx": [], "treatment": [], "notes": [], "imaging": [],
                                     "pathology": []})

    for r in sdf.select(*skel_cols).collect():
        d = r.asDict()
        mrn = _s(d.get(C.COL_MRN)).strip()
        if not mrn:
            continue
        p = _p(mrn)
        if p["cancer_type"] is None and d.get(C.COL_CANCER_TYPE):
            p["cancer_type"] = _s(d.get(C.COL_CANCER_TYPE))
        if p["death_date"] is None and d.get(C.COL_DEATH_DATE):
            p["death_date"] = _s(d.get(C.COL_DEATH_DATE))[:10]
        ds = _s(d.get(C.COL_DATA_SOURCE))
        if ds == C.SRC_ONCHX:
            p["onchx"].append(d)
        elif ds == C.SRC_PATHOLOGY:
            # Pathology: keep ONLY the specimen collection date + report type (Date|Type timeline).
            p["pathology"].append({
                "collected_date": _date10(d.get(C.COL_NOTE_SERVICE_DT)),
                "path_type": _s(d.get(C.COL_NOTE_TYPE)),
            })
        else:
            p["treatment"].append(d)

    for r in ndf.collect():
        d = r.asDict()
        mrn = _s(d.get("mrn")).strip()
        if not mrn:
            continue
        p = _p(mrn)
        p["notes"].append(d)
        if p["cancer_type"] is None and d.get("cancer_type"):
            p["cancer_type"] = _s(d.get("cancer_type"))
        # CTH orientation blocks (selection rules):
        #   oncologic_history   = the LATEST note's value,
        #   treatment_history   = the LONGEST value,
        #   chemotherapy_summary = the consolidated dedup block (single non-null per patient).
        dt = _s(d.get("note_service_datetime"))[:10]
        onc = d.get("cth_oncologic_history")
        if onc and str(onc).strip() and dt >= (p["cth_onc_date"] or ""):
            p["cth_onc"], p["cth_onc_date"] = onc, dt
        tr = d.get("cth_treatment_history")
        if tr and len(str(tr)) > len(str(p["cth_treat"] or "")):
            p["cth_treat"] = tr
        ch = d.get("cth_chemotherapy_summary")
        if ch and len(str(ch)) > len(str(p["cth_chemo"] or "")):
            p["cth_chemo"] = ch

    for r in idf.collect():
        d = r.asDict()
        mrn = _s(d.get("mrn")).strip()
        if mrn:
            _p(mrn)["imaging"].append(d)

    rows: List[Row] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_process_patient, p, model): mrn for mrn, p in pmap.items()}
        for fut in as_completed(futs):
            try:
                rows.extend(fut.result())
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] patient {futs[fut]} failed: {e}")

    output.write_dataframe(ctx.spark_session.createDataFrame(rows or [], _schema()))


@configure(profile=["KUBERNETES_NO_EXECUTORS"])
@transform(
    output=Output(C.OUT_AGENT_TIMELINE),
    source=Input(C.INPUT_DATASET),
    notes_prepped=Input(C.OUT_NOTES_PREPPED),
    imaging=Input(C.OUT_IMAGING_CLASSIFIED),
    model=GenericCompletionLanguageModelInput(C.MODEL_RID_GPT5_SOL),
)
def compute(ctx, output, source, notes_prepped, imaging, model):
    """Stage 3 with GPT-5.6 Sol -- ADOPTED DEFAULT (best PFS + line detection in the model A/B;
    established by the model A/B evaluation). To revert to Opus, swap the model input back to
    C.MODEL_RID_OPUS and the model_name/config below."""
    run_stage3(ctx, output, source, notes_prepped, imaging, model,
               model_name="gpt-5-6-sol", prompt_version="enlist_v2.8_sol_pathology",
               max_tokens=C.GPT5_MAX_TOKENS, temperature=C.GPT5_TEMPERATURE,
               max_workers=C.GPT5_MAX_WORKERS, pricing=C.MODEL_PRICING.get(C.MODEL_RID_GPT5_SOL))
