"""
Central configuration for the EnLiST LoT pipeline.

Everything environment-specific (model RIDs, dataset paths) or tunable by a
clinician-reviewer (note-type routing / disregard rules) lives here so the
transform code stays stable.
"""

# ---------------------------------------------------------------------------
# 1. MODELS  (reuse the enlist714 RIDs: Haiku 4.5 = cheap map, Opus 4.8 = reason)
# ---------------------------------------------------------------------------
MODEL_RID_HAIKU = "ri.language-model-service..language-model.anthropic-claude-4-5-haiku"
MODEL_RID_OPUS = "ri.language-model-service..language-model.anthropic-claude-4-8-opus"
# GPT-5 comparison variants (same v2.5 combined agent; only the model is swapped).
MODEL_RID_GPT5_TERRA = "ri.language-model-service..language-model.gpt-5-6-terra"
MODEL_RID_GPT5_SOL = "ri.language-model-service..language-model.gpt-5-6-sol"
GPT5_MAX_TOKENS = 32000
GPT5_TEMPERATURE = None   # GPT-5 reasoning models reject a custom temperature -> omit
GPT5_MAX_WORKERS = 4

# Generation params
HAIKU_MAX_TOKENS = 16         # imaging classifier emits ONE label token (e.g. PROGRESSION_CANDIDATE)
HAIKU_TEMPERATURE = 0.0       # deterministic classification


# Concurrency (ThreadPoolExecutor workers for parallel LLM calls)
HAIKU_MAX_WORKERS = 8

# Model pricing (USD per 1,000,000 tokens) -- list-price ESTIMATES for cost audit.
MODEL_PRICING = {
    MODEL_RID_HAIKU: {"input_per_1m": 1.00, "output_per_1m": 5.00},
    MODEL_RID_OPUS: {"input_per_1m": 5.00, "output_per_1m": 25.00},
    MODEL_RID_GPT5_TERRA: {"input_per_1m": 1.25, "output_per_1m": 10.00},   # rough estimate
    MODEL_RID_GPT5_SOL: {"input_per_1m": 1.25, "output_per_1m": 10.00},     # rough estimate
}

# ---------------------------------------------------------------------------
# 2. DATASETS
# ---------------------------------------------------------------------------
# NOTE (public release): every dataset RID and Foundry path below is a
# non-functional PLACEHOLDER. To run this pipeline against a real deployment,
# replace them with the resource identifiers from your own Foundry instance.
# The language-model RIDs in section 1 are platform model identifiers and are real.

# Single denormalized clinical-record input (1 row = 1 clinical document).
# Reference cohort at development time: 8,982 rows.
INPUT_DATASET = "ri.foundry.main.dataset.00000000-0000-0000-0000-000000000000"

# Output datasets (folder: <namespace>/outputs).
_OUT = "/YOUR_ORG/YOUR_PROJECT/outputs"
OUT_NOTES_PREPPED = _OUT + "/enlist_notes_prepped"
OUT_IMAGING_CLASSIFIED = _OUT + "/enlist_imaging_classified"
OUT_AGENT_TIMELINE = _OUT + "/enlist_agent_timeline"      # Stage 3 (GPT-5.6 Sol -- adopted default): LoT + PFS
OUT_AGENT_TIMELINE_TERRA = _OUT + "/enlist_agent_timeline_terra"  # Stage 3 (GPT-5.6 Terra variant)
OUT_PATIENT_SUMMARY = _OUT + "/enlist_patient_summary"   # Stage 3c: one row / patient, EnLiST composite

# Gold-standard clinician reviews (LONG format, one row per episode) for the NW eval.
# Two independent clinician reviewers; reviewer identities are not published.
GS_REVIEWER_1_CRC = "ri.foundry.main.dataset.00000000-0000-0000-0000-000000000002"
GS_REVIEWER_2_CRC = "ri.foundry.main.dataset.00000000-0000-0000-0000-000000000003"
GS_REVIEWER_2_PANCREAS = "ri.foundry.main.dataset.00000000-0000-0000-0000-000000000004"
# NW alignment eval outputs.
OUT_EVAL_ALIGNMENT = _OUT + "/enlist_eval_alignment"          # one row per aligned pair
OUT_EVAL_FIELD_ACCURACY = _OUT + "/enlist_eval_field_accuracy"  # per-field, per-reviewer accuracy
# GPT-5 variant eval outputs
OUT_EVAL_ALIGNMENT_TERRA = _OUT + "/enlist_eval_alignment_terra"
OUT_EVAL_FIELD_ACCURACY_TERRA = _OUT + "/enlist_eval_field_accuracy_terra"

# ---------------------------------------------------------------------------
# 2b. SCOPE  (empty list => process ALL patients in the input dataset)
# ---------------------------------------------------------------------------
TARGET_MRNS = []  # full cohort (all patients in the input dataset)

# ---------------------------------------------------------------------------
# 3. INPUT COLUMNS
# ---------------------------------------------------------------------------
COL_MRN = "mrn"
COL_CANCER_TYPE = "cancer_type"
COL_NOTE_KEY = "note_surrogate_pkey"        # unique per row
COL_NOTE_TEXT = "note_text"
COL_PROVIDER_TYPE = "authoring_provider_type_name"
COL_NOTE_TYPE = "note_type_name"
COL_EVENT_NOTE_TYPE = "event_note_type_name"
COL_DIAGNOSIS = "diagnosis_name"
COL_EVENT_NOTE_NUMBER = "event_note_number"
COL_NOTE_START_DATE = "note_start_date"
COL_NOTE_END_DATE = "note_end_date"
COL_TREATMENT_TYPE = "treatment_type_name"
COL_TREATMENT_DESC = "treatment_technique_description"
COL_TREATMENT_ORDER = "treatment_order_number"
COL_EARLIEST_TX = "earliest_treatment_datetime"
COL_LATEST_TX = "latest_treatment_datetime"
COL_EXAM_END = "exam_end_datetime"
COL_FIRST_PROC = "first_procedure_name"
COL_IMPRESSION = "impression_text"
COL_DATA_SOURCE = "data_source"
COL_NOTE_SERVICE_DT = "note_service_datetime"
COL_DEATH_DATE = "verified_death_date"

# ---------------------------------------------------------------------------
# 4. data_source values
# ---------------------------------------------------------------------------
SRC_ONCHX = "secure_onchx"                                    # oncology history (skeleton)
SRC_TX_SUMMARY = "treatment_summ"                             # treatment summaries (skeleton)
SRC_PATHOLOGY = "Pathology"                                   # surgical path / biopsy / molecular
SRC_NONSENSITIVE = "nonsensitive_dataset"                     # progress notes, H&P, consults
SRC_SENSITIVE = "sensitive_dataset"                           # clinical notes (sensitive)
# imaging data_source is matched by the substring "image" (full name is long).

# The two sources that go through the A&P section-excerpt reading path.
NOTE_READING_SOURCES = {SRC_NONSENSITIVE, SRC_SENSITIVE}

# ---------------------------------------------------------------------------
# 5. DISREGARD RULES (note-reading path only; nonsensitive + sensitive)
# ---------------------------------------------------------------------------
# Dropped entirely (low LoT signal).
DISREGARD_NOTE_TYPES = {
    "Discharge Instructions",
    "Pre-Procedure Instructions",
    "ED Provider Notes",
    "Discharge Summary",
    "Telephone Encounter",         # MyChart message threads - scan logistics, scheduling
    "Advance Care Planning",       # goals of care, not LoT-relevant
    "Brief Op Note",               # admin/specimen tables only - full Op Note covers procedures
}
# Additionally: Progress Notes with a NULL authoring_provider_type_name are dropped
# (handled in code, since it is a compound condition).
DISREGARD_PROGRESS_NOTE_TYPE = "Progress Notes"

