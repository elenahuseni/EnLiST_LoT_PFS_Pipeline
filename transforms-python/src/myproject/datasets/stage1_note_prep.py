"""
Stage 1 -- deterministic note preprocessing (NO LLM).

Scope: the note-reading path only = data_source in {nonsensitive_dataset, sensitive_dataset}.
Applies the disregard rules and attaches the cleaned narrative excerpt (relevant_excerpt) via
note_cleaning.strip_boilerplate().

CTH handling:
  - strip_boilerplate extracts each note's CTH sections (per-section columns).
  - MOST CTH sections are kept PER-NOTE (each note retains its own snapshot; no cross-note merge).
  - EXCEPTION -- cth_chemotherapy_summary: this section is copy-pasted and grows cumulatively, so
    the same paragraph recurs verbatim across dozens of notes. We consolidate it PER PATIENT by
    BLANK-LINE PARAGRAPH exact-dedup (note_cleaning.dedupe_cth_paragraphs): pool every note's
    snapshot, split on blank lines, keep only DISTINCT paragraphs (oldest-first), and place the
    single consolidated block on ONE canonical row per patient (the latest note), NULLing it on
    every other row. Paragraphs that differ at all (e.g. drifting 'X of Y cycles') are each kept.

Output enlist_notes_prepped (one row per kept note):
  mrn, cancer_type, note_surrogate_pkey, data_source, note_type_name,
  authoring_provider_type_name, note_service_datetime, note_start_date, note_text,
  relevant_excerpt, excerpt_tier, matched_header,
  cth_history_block + per-section cth_* columns. All cth_* are per-note EXCEPT
  cth_chemotherapy_summary, which is the per-patient paragraph-deduped block on the canonical row.
"""
from pyspark.sql import Window, functions as F, types as T
from transforms.api import transform, Input, Output

from myproject import config as C
from myproject.note_cleaning import (
    strip_boilerplate, CTH_SECTION_KEYS, dedupe_chemo_summary,
    strip_research_coordinator_questionnaires,
)
from myproject.text_sections import extract_excerpt  # fallback for flagged_empty notes

_CTH_COLS = ["cth_" + k for k in CTH_SECTION_KEYS]
# The one CTH section consolidated per-patient (paragraph exact-dedup); the rest stay per-note.
_CHEMO_SUMMARY_COL = "cth_chemotherapy_summary"

_EXCERPT_RT = T.StructType(
    [
        T.StructField("relevant_excerpt", T.StringType()),
        T.StructField("excerpt_tier", T.StringType()),
        T.StructField("matched_header", T.StringType()),
    ]
    + [T.StructField(c, T.StringType()) for c in _CTH_COLS]
)


@F.udf(returnType=_EXCERPT_RT)
def _excerpt_udf(note_text, note_type_name, author_type):
    # Research-Coordinator "Study Interim" trial notes: pre-remove the Questionnaires ->
    # Adverse-Events -> meds span (up to the Notes/Assessment section) before stripping.
    if note_type_name == "Study Interim" and author_type == "Research Coordinator":
        note_text = strip_research_coordinator_questionnaires(note_text)

    result = strip_boilerplate(note_text)
    if not result["flagged_empty"]:
        secs = result.get("cth_sections") or {}
        return tuple(
            [result["cleaned_note"], "cleaned", "strip_boilerplate"]
            + [secs.get(k) for k in CTH_SECTION_KEYS]
        )
    # Fallback: if the stripper killed everything, use the old A&P extractor (no CTH).
    excerpt, tier, header = extract_excerpt(note_text)
    return tuple([excerpt, tier, header] + [None] * len(CTH_SECTION_KEYS))


@F.udf(returnType=T.StringType())
def _dedupe_paragraphs_udf(arr):
    """Regimen-level dedup (FIX 3) across a patient's Chemotherapy-summary snapshots: one
    paragraph per regimen (first drug + treatment start date), latest version wins."""
    blocks = [
        (r["v"], (str(r["d"]) if r["d"] is not None else None))
        for r in (arr or []) if r is not None and r["v"]
    ]
    return dedupe_chemo_summary(blocks)


@transform(
    output=Output(C.OUT_NOTES_PREPPED),
    source=Input(C.INPUT_DATASET),
)
def compute(ctx, source, output):
    df = source.dataframe()

    # 1) scope to the note-reading data sources
    df = df.where(F.col(C.COL_DATA_SOURCE).isin(list(C.NOTE_READING_SOURCES)))
    # 2) scope to target patients (optional)
    if C.TARGET_MRNS:
        df = df.where(F.col(C.COL_MRN).isin([str(m).strip() for m in C.TARGET_MRNS]))
    # 3) disregard rules: low-signal note types; Progress Notes with a null author
    df = df.where(~F.col(C.COL_NOTE_TYPE).isin(list(C.DISREGARD_NOTE_TYPES)))
    df = df.where(
        ~(
            (F.col(C.COL_NOTE_TYPE) == C.DISREGARD_PROGRESS_NOTE_TYPE)
            & (F.col(C.COL_PROVIDER_TYPE).isNull())
        )
    )

    # 4) per-note excerpt + per-note CTH sections
    df = df.withColumn(
        "_sec",
        _excerpt_udf(F.col(C.COL_NOTE_TEXT), F.col(C.COL_NOTE_TYPE), F.col(C.COL_PROVIDER_TYPE)),
    )
    noted = df.select(
        F.col(C.COL_MRN).alias("mrn"),
        F.col(C.COL_CANCER_TYPE).alias("cancer_type"),
        F.col(C.COL_NOTE_KEY).alias("note_surrogate_pkey"),
        F.col(C.COL_DATA_SOURCE).alias("data_source"),
        F.col(C.COL_NOTE_TYPE).alias("note_type_name"),
        F.col(C.COL_PROVIDER_TYPE).alias("authoring_provider_type_name"),
        F.col(C.COL_NOTE_SERVICE_DT).alias("note_service_datetime"),
        F.col(C.COL_NOTE_START_DATE).alias("note_start_date"),
        F.col(C.COL_NOTE_TEXT).alias("note_text"),
        F.col("_sec.relevant_excerpt").alias("relevant_excerpt"),
        F.col("_sec.excerpt_tier").alias("excerpt_tier"),
        F.col("_sec.matched_header").alias("matched_header"),
        *[F.col("_sec." + c).alias(c) for c in _CTH_COLS],   # per-note CTH sections
    )

    # 5) Consolidate ONLY cth_chemotherapy_summary per patient by blank-line paragraph dedup.
    chemo_agg = noted.groupBy("mrn").agg(
        _dedupe_paragraphs_udf(
            F.collect_list(F.struct(F.col(_CHEMO_SUMMARY_COL).alias("v"),
                                    F.col("note_service_datetime").alias("d")))
        ).alias("_chemo_dedup")
    )

    # canonical row per patient = latest note (tie-break note id)
    w = Window.partitionBy("mrn").orderBy(
        F.col("note_service_datetime").desc_nulls_last(),
        F.col("note_surrogate_pkey").desc_nulls_last(),
    )
    noted = noted.withColumn("_rn", F.row_number().over(w))
    noted = noted.join(chemo_agg, "mrn", "left")
    # deduped chemo summary lives ONLY on the canonical row; per-note copies are dropped.
    noted = noted.withColumn(
        _CHEMO_SUMMARY_COL,
        F.when(F.col("_rn") == 1, F.col("_chemo_dedup")).otherwise(F.lit(None)),
    )

    # 6) per-note combined CTH block (now uses the consolidated chemo summary on the canonical row)
    _combined = F.concat_ws("\n\n", *[F.col(c) for c in _CTH_COLS])
    noted = noted.withColumn(
        "cth_history_block", F.when(F.length(_combined) > 0, _combined).otherwise(F.lit(None))
    )

    out = noted.select(
        "mrn", "cancer_type", "note_surrogate_pkey", "data_source", "note_type_name",
        "authoring_provider_type_name", "note_service_datetime", "note_start_date", "note_text",
        "relevant_excerpt", "excerpt_tier", "matched_header", "cth_history_block", *_CTH_COLS,
    )
    output.write_dataframe(out)
