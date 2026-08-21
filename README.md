# EnLiST — LLM Extraction of Lines of Therapy and PFS from Clinical Notes

A four-stage pipeline that reads unstructured oncology clinical documents and emits a
structured, per-patient treatment timeline: an ordered list of systemic anti-cancer therapy
(SACT) episodes labelled with **EnLiST line-of-therapy notation**, together with a
**progression-free survival (PFS)** event-or-censor determination for each line.

The pipeline pairs LLM reasoning with deterministic post-processing. The model proposes an
ordered episode list tagged with machine-readable change triggers; a rule engine
(`notation.py`) — not the model — assigns the final line labels. Accuracy is measured by
sequence alignment against independent clinician-reviewed gold standards.

> **This repository targets Palantir Foundry and will not build or run as-is elsewhere.**
> See [Running it](#running-it) before attempting to use it.

> **All dataset identifiers and paths in this repository are placeholders.** See
> [Configuration](#configuration).

---

## Pipeline

| Stage | File | LLM | What it does |
|---|---|---|---|
| 1 | `stage1_note_prep.py` | no | Deterministic note preprocessing. Applies note-type disregard rules, strips boilerplate (ROS, physical exam, labs, medications, histories, signatures) while keeping the clinical narrative, and consolidates the cumulative Cancer Treatment History (CTH) block to one canonical copy per patient. |
| 2 | `stage2_imaging_classifier.py` | yes | Labels each radiology impression `PROGRESSION_CANDIDATE` / `NO_PROGRESSION` / `INDETERMINATE` / `NOT_RELEVANT`, turning PFS event adjudication into a timeline lookup rather than a prose judgement. Full read text, no truncation. |
| 3 | `stage3_agent_extraction.py` | yes | The core agent. Per patient, pre-loads an orientation (oncology-history events, treatment summaries, CTH blocks), the pre-classified imaging trajectory, and a note index. The agent retrieves targeted excerpts through a single `get_notes` tool, applies the EnLiST guidelines, and emits an ordered SACT episode timeline covering both LoT and PFS. |
| 3c | `stage3c_patient_summary.py` | no | Rolls episodes up into the patient-level ESMO composite `[eLoT X.Y + aLoT X.Y] + iLoT X.Y`. |
| 4 | `stage4_eval_vs_gs.py` | no | Scores the agent against clinician gold standards using Needleman–Wunsch alignment. |

`stage3_terra.py` and `stage4_terra.py` are thin wrappers that re-run stages 3 and 4 with a
different model, to A/B the model while holding the agent, prompt, and tools fixed.

### Deterministic modules

These carry the domain logic and are the most reusable part of the repository. None of them
call an LLM.

| Module | Responsibility |
|---|---|
| `notation.py` | The EnLiST adjudicator. Walks the guideline table over the agent's ordered episode list, assigning `X.Y` labels and New/Modified/Same using three independent counters (eLoT / aLoT / iLoT), deriving the U-code date-certainty scale and the reason for switching. |
| `nw_align.py` | Needleman–Wunsch alignment of predicted vs. gold episode sequences. Respects temporal order. Regimen equivalence by Jaccard similarity over canonical drug sets — no LLM adjudication. Composite similarity = drug Jaccard (0.5) + start proximity within 60d (0.3) + end proximity within 90d (0.2). |
| `note_cleaning.py` | Boilerplate stripper. Removes low-value sections and copy-paste footers while preserving the narrative. |
| `text_sections.py` | Section-header extraction. Mimics a clinician's reading strategy — find the earliest Assessment/Plan/Impression anchor and keep from there, with tiered fallbacks. |
| `llm.py` | Thin model wrapper over the platform's generic completion interface, with latency, token-usage, and cost accounting. |

---

## Why alignment-based evaluation

Line-of-therapy extraction produces a *sequence*, so per-row comparison is not meaningful — a
single missed or spurious early line shifts every subsequent line and would score as a total
failure under naive row matching. Needleman–Wunsch alignment finds the best order-preserving
correspondence between predicted and gold episodes first, then compares fields only on matched
pairs, and reports unmatched episodes explicitly as `gs_missed` or `agent_extra`.

Stage 4 emits two datasets:

- `enlist_eval_alignment` — one row per aligned pair, with per-field match booleans and mismatch reasons
- `enlist_eval_field_accuracy` — per-field accuracy per reviewer, plus line-detection sensitivity, PPV, and F1

Stage 4 also computes a **human floor**: the same alignment methodology applied between two
independent clinician reviewers on the same cohort. This gives inter-reviewer agreement as a
reference point, so model accuracy can be read against what human agreement actually looks like
on this task rather than against an implicit assumption of a perfect gold standard.

---

## Configuration

All environment-specific values live in `transforms-python/src/myproject/config.py`.

**Every dataset RID and Foundry path in this repository is a non-functional placeholder.**
They are zero-filled by design, not broken configuration:

```
ri.foundry.main.dataset.00000000-0000-0000-0000-00000000000N
/YOUR_ORG/YOUR_PROJECT/outputs
```

To run against a real deployment, replace them with identifiers from your own instance.

The **language-model RIDs are real** platform identifiers and are left intact, since they
document what the pipeline actually ran on. Constants marked `RESERVED` or `DIAGNOSTIC` are
retained deliberately but are not read by any transform in this release.

`config.py` also holds the clinician-tunable knobs — note-type routing, disregard rules, and
operative-note handling — so those can be adjusted without touching transform code.

### Expected input

A single denormalized table, **one row per clinical document**. All column names are declared
in `config.py` section 3 and can be remapped there without touching transform code.

**A note on types.** The pipeline is deliberately type-tolerant: every field is read through a
stringifying accessor (`_s()`), and every date field is normalised by taking the first ten
characters (`str(value)[:10]` → `YYYY-MM-DD`). Date columns are therefore **never parsed as
dates**. A native `TimestampType` and an ISO-formatted `StringType` both work, because a
timestamp's string form already begins `YYYY-MM-DD`. The "Type" column below gives the Spark
type the pipeline assumes; "→ date10" marks the fields truncated to `YYYY-MM-DD` on read.

#### Patient

| Column | Type | Purpose | Used by |
|---|---|---|---|
| `mrn` | string | Patient identifier. Grouping key for all per-patient processing, and the cohort filter when `TARGET_MRNS` is non-empty. | 1, 2, 3 |
| `cancer_type` | string | Tumour type. Carried through to patient-level output as context. | 1, 3 |
| `verified_death_date` | string / timestamp → date10 | Date of death.

#### Document identity and routing

| Column | Type | Purpose | Used by |
|---|---|---|---|
| `note_surrogate_pkey` | string | Unique per row. This is the `note_id` the Stage 3 agent passes to its `get_notes` retrieval tool, so it must be stable and unique. | 1, 2, 3 |
| `data_source` | string | Routes each row to a processing path — see the table below. | 1, 2, 3 |
| `note_type_name` | string | Applies the disregard rules; also supplies the report type in the pathology timeline. | 1, 3 |
| `authoring_provider_type_name` | string | Author role. Gates clinician-corroborated progression (cPD must come from Physician / PA / NP / Fellow / Resident). Progress Notes with a null author are dropped. | 1 |
| `event_note_type_name` | string | Event type within the oncology-history and treatment-summary skeleton. | 3 |
| `event_note_number` | string | Event ordering within the orientation skeleton. | 3 |
| `diagnosis_name` | string | Diagnosis label shown in the patient orientation. | 3 |

#### Document content and timing

| Column | Type | Purpose | Used by |
|---|---|---|---|
| `note_text` | string | Full document body. Boilerplate-stripped in Stage 1; used whole for imaging rows that have no impression. | 1, 2, 3 |
| `note_service_datetime` | string / timestamp → date10 | Primary document date. Anchors timeline construction and note retrieval by `near_date`; also the fallback exam date for imaging. | 1, 2, 3 |
| `note_start_date` | string / timestamp → date10 | Start of the note's coverage period. | 1, 3 |
| `note_end_date` | string / timestamp → date10 | End of the note's coverage period. | 3 |

#### Structured treatment summary

Rows where `data_source` is `treatment_summ`. These form the treatment skeleton — authoritative
for what was administered **at the treating institution**, but a subset that misses outside lines.

| Column | Type | Purpose | Used by |
|---|---|---|---|
| `treatment_type_name` | string | Treatment category (systemic / surgery / radiation). | 3 |
| `treatment_technique_description` | string | Regimen or procedure description. | 3 |
| `treatment_order_number` | string | Ordering of records within a patient's treatment summary. | 3 |
| `earliest_treatment_datetime` | string / timestamp → date10 | Episode start bound. | 3 |
| `latest_treatment_datetime` | string / timestamp → date10 | Episode end bound. | 3 |

#### Imaging

Rows whose `data_source` contains the substring `image` (matched case-insensitively).

| Column | Type | Purpose | Used by |
|---|---|---|---|
| `impression_text` | string | Radiology impression. Preferred read text for the Stage 2 classifier; falls back to `note_text` when absent. | 2 |
| `exam_end_datetime` | string / timestamp → date10 | Exam date. Falls back to `note_service_datetime` when null. | 2 |
| `first_procedure_name` | string | Imaging modality. | 2 |

#### `data_source` values

| Value | Contents | Path |
|---|---|---|
| `secure_onchx` | Oncology history events | Orientation skeleton (Stage 3) |
| `treatment_summ` | Structured treatment records | Orientation skeleton (Stage 3) |
| `Pathology` | Surgical pathology, biopsy, molecular | Specimen timeline (Stage 3) |
| `nonsensitive_dataset` | Progress notes, H&P, consults | Section-excerpt reading path (Stage 1) |
| `sensitive_dataset` | Clinical notes flagged sensitive | Section-excerpt reading path (Stage 1) |
| *contains* `image` | Radiology reports | Imaging classifier (Stage 2) |

Only `nonsensitive_dataset` and `sensitive_dataset` go through boilerplate stripping and
section extraction; the others are consumed as structured records or as whole text.

> **Columns present in the source table but not consumed.** The development dataset also carried
> `treatment_site_list` and `order_type_name`. No transform reads them, so they were dropped from
> `config.py` rather than left as dead declarations. Add them back to section 3 if you extend the
> pipeline to use them.

---

## Running it

This repository is a Foundry transforms project. `ci.yml`, the Gradle build files,
`transforms-shrinkwrap`-style dataset pinning, the conda lockfiles, and the Gradle wrapper all
resolve against Foundry infrastructure. It will not build standalone.

To reuse this work outside Foundry, take the deterministic modules and the prompts, which have
no platform dependency beyond `llm.py`:

```
transforms-python/src/myproject/
├── prompts/enlist_prompts.py   # the agent prompts — the domain core
├── notation.py                 # EnLiST adjudication rules
├── nw_align.py                 # evaluation alignment
├── note_cleaning.py            # boilerplate stripping
└── text_sections.py            # section extraction
```

Those five files carry the clinical logic. The `datasets/` stage files are Foundry transform
wiring around them.

---

## Limitations

- **Institution-scoped structured data.** The treatment summary and pathology timeline cover
  only care delivered at the treating institution. Outside and pre-referral therapy appears in
  narrative text but not in the structured feeds, so the agent is explicitly instructed to
  enumerate outside lines from the narrative and not to restrict itself to institutional
  records. A missing pathology report does not imply a procedure did not occur.
- **Gold-standard scale.** Reported accuracy derives from a modest clinician-reviewed cohort.
  Treat the human floor as the meaningful comparison point.
- **Disease scope.** Developed and validated on colorectal cancer, with a pancreatic gold
  standard used as a secondary check. Generalization to other tumour types is untested.
- **Model dependence.** Stage 3 behaviour is sensitive to the model. Re-validate against your
  own gold standard before changing models.
- **No patient data.** This repository contains code and prompts only. No clinical data,
  cohort definitions, or model outputs are included.

---

## License

MIT — see [LICENSE](LICENSE).
