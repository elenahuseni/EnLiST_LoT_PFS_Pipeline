"""
Stage 2 -- imaging classifier (Haiku).

Label each radiology impression PROGRESSION_CANDIDATE / NO_PROGRESSION / INDETERMINATE /
NOT_RELEVANT from the read text only, so the LoT/PFS agent gets a compact imaging
trajectory (event-vs-censor becomes a timeline lookup instead of prose judgement).

Read text:
  - the WHOLE impression_text if present;
  - else the WHOLE note_text (the full radiology report), NOT a sliced excerpt;
  - else None -> row excluded.
The full read text is sent to the classifier (no slicing / no truncation).

CLINICAL RULE (enforced downstream, not here): a PROGRESSION_CANDIDATE read is radiologic
evidence ONLY; it becomes cPD / a PFS EVENT only when the treating clinician corroborates
it in a note. This classifier just tags the trajectory.
"""
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from pyspark.sql import types as T, functions as F
from transforms.api import transform, Input, Output, configure
from palantir_models.transforms import GenericCompletionLanguageModelInput

from myproject import config as C
from myproject import llm

_VALID = ("PROGRESSION_CANDIDATE", "NO_PROGRESSION", "INDETERMINATE", "NOT_RELEVANT")

SYSTEM_PROMPT = r"""You classify a SINGLE radiology IMPRESSION for a colorectal- or pancreatic-cancer
patient. Output EXACTLY ONE token and nothing else:

  PROGRESSION_CANDIDATE = Imaging shows unequivocal NEW malignant disease or CLEAR WORSENING
        of known disease compared with prior imaging. 
  NO_PROGRESSION = Imaging shows stable disease, response/improvement, or no evidence of
        active disease, with no clear new or worsening malignancy. 
  INDETERMINATE = Imaging is equivocal, mixed, technically limited, lacks an adequate
        comparison, or otherwise cannot reliably establish whether progression occurred.
  NOT_RELEVANT = The study does not meaningfully evaluate the indexed cancer or the disease
        sites under review, or is a non-oncologic examination with no usable tumour
        assessment.

Judge ONLY the cancer / metastatic disease trajectory. If the impression mixes findings,
choose the label for the CANCER (e.g. "post-op changes, new liver metastasis" ->
PROGRESSION_CANDIDATE).
Output just one of: PROGRESSION_CANDIDATE, NO_PROGRESSION, INDETERMINATE, NOT_RELEVANT."""


def _classify(model, rec, pricing, run_ts):
    # Read the WHOLE impression_text if present; otherwise the WHOLE note_text (full
    # radiology report). No slicing and no truncation - the entire read text is classified.
    imp = rec.get("impression_text")
    note = rec.get("note_text")
    if imp is not None and str(imp).strip():
        read_text, read_source = str(imp), "impression_text"
    elif note is not None and str(note).strip():
        read_text, read_source = str(note), "note_text"
    else:
        return None
    trace = llm.complete_with_trace(
        model, SYSTEM_PROMPT, read_text, C.HAIKU_MAX_TOKENS,
        temperature=C.HAIKU_TEMPERATURE, pricing=pricing,
    )
    resp = (trace.get("text") or "").upper()
    label = "INDETERMINATE"
    for tok in _VALID:                 # PROGRESSION_CANDIDATE checked before NO_PROGRESSION
        if tok in resp:
            label = tok
            break
    return {
        "mrn": rec.get("mrn"),
        "note_id": rec.get("note_id"),
        "exam_date": rec.get("exam_date"),
        "modality": rec.get("modality"),
        "read_source": read_source,
        "impression": read_text[:500],
        "imaging_class": label,
        "raw_response": (trace.get("text") or "")[:200],
        "cost_usd": trace.get("cost_usd") or 0.0,
        "model_rid": C.MODEL_RID_HAIKU,
        "run_ts": run_ts,
        "error": trace.get("error"),
    }


@configure(profile=["KUBERNETES_NO_EXECUTORS"])
@transform(
    output=Output(C.OUT_IMAGING_CLASSIFIED),
    source=Input(C.INPUT_DATASET),
    model=GenericCompletionLanguageModelInput(C.MODEL_RID_HAIKU),
)
def compute(ctx, output, source, model):
    run_ts = datetime.datetime.utcnow().isoformat()
    df = source.dataframe()

    # imaging rows only
    df = df.where(F.lower(F.col(C.COL_DATA_SOURCE)).contains("image"))
    if C.TARGET_MRNS:
        df = df.where(F.col(C.COL_MRN).isin([str(m).strip() for m in C.TARGET_MRNS]))

    recs = []
    for r in df.select(
        C.COL_MRN, C.COL_NOTE_KEY, C.COL_EXAM_END, C.COL_NOTE_SERVICE_DT,
        C.COL_FIRST_PROC, C.COL_IMPRESSION, C.COL_NOTE_TEXT,
    ).collect():
        d = r.asDict()
        recs.append({
            "mrn": d.get(C.COL_MRN),
            "note_id": d.get(C.COL_NOTE_KEY),
            "exam_date": str(d.get(C.COL_EXAM_END) or d.get(C.COL_NOTE_SERVICE_DT) or "")[:10],
            "modality": d.get(C.COL_FIRST_PROC),
            "impression_text": d.get(C.COL_IMPRESSION),
            "note_text": d.get(C.COL_NOTE_TEXT),
        })

    pricing = C.MODEL_PRICING.get(C.MODEL_RID_HAIKU)
    out = []
    with ThreadPoolExecutor(max_workers=C.HAIKU_MAX_WORKERS) as ex:
        futs = [ex.submit(_classify, model, rec, pricing, run_ts) for rec in recs]
        for fut in as_completed(futs):
            try:
                row = fut.result()
                if row:
                    out.append(row)
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] imaging classify failed: {type(e).__name__}: {e}")

    output.write_dataframe(ctx.spark_session.createDataFrame(out or [], _schema()))


def _schema():
    return T.StructType([
        T.StructField("mrn", T.StringType()),
        T.StructField("note_id", T.StringType()),
        T.StructField("exam_date", T.StringType()),
        T.StructField("modality", T.StringType()),
        T.StructField("read_source", T.StringType()),
        T.StructField("impression", T.StringType()),
        T.StructField("imaging_class", T.StringType()),
        T.StructField("raw_response", T.StringType()),
        T.StructField("cost_usd", T.DoubleType()),
        T.StructField("model_rid", T.StringType()),
        T.StructField("run_ts", T.StringType()),
        T.StructField("error", T.StringType()),
    ])
