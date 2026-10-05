# Clinical Model Card: EnLiST LoT/PFS Extraction Pipeline

> **Research use only.** The system described here was evaluated retrospectively for research
> data abstraction. It has not been evaluated for, and must not be used for, direct patient care.
> This card is structured to support MI-CLAIM-GEN reporting (see the crosswalk in Section 20).

---

## 1. Model card metadata

| Item | Value |
|---|---|
| System name | EnLiST LoT/PFS extraction pipeline |
| Model card version | 0.1 |
| Evaluated code version | Commit `751b1a4e04c6d0dd2b4ecd3912a81f6f260190bf` (internal repository) |
| Evaluated configuration | Model-comparison experiment cell `opus5`, repeat 4 (prompt-version tag `exp_v2.10_opus5_crit_rep4`) |
| Evaluation run dates | Opus 5 repeats built 2026-08-27 to 2026-08-31 (repeat 4: 2026-08-31) |
| Data cutoff | EHR input snapshot of 15 July 2026 (the version read by the evaluated run) |
| Public release version / tag | The frozen implementation is version-tagged in the public repository |
| Licence | MIT |
| Study protocol | MD Anderson Cancer Center IRB protocol LAB09-0373 |
| Manuscript | *Large language model agentic extraction of consensus-defined lines of therapy and progression-free survival from oncology clinical notes* (in preparation) |
| Authors | Cynthia Yeung\*, Elena Huseni\*, Berta Martin Cullell\*, Songwit Payapwattanawong, Emerik Osterlund, Guglielmo Vetere, Saikat Chowdhury, Wenjie Zhu, Xiling Shen, John Paul Shen, Chong Wu, Scott Kopetz (\*equal contribution) |
| Maintainers | Chong Wu, Scott Kopetz |
| Contact | Chong Wu (cwu18@mdanderson.org), Scott Kopetz (SKopetz@mdanderson.org) |
| Date of this card | 2026-10-04 |

## 2. System summary

The pipeline reads a patient's structured and unstructured electronic health record (EHR)
documents and produces an ordered **line-of-therapy (LoT) timeline** for systemic anticancer
therapy (SACT), with **progression-free survival (PFS)** event/censoring information for each
line. Line designation follows the **ESMO adaptation of Lines of Systemic Therapy (EnLiST)**
consensus framework for standardising the designation of lines of therapy in solid tumours
(Saini et al., *Ann Oncol* 2026). The study team extended it with a prespecified,
clinician-anchored definition of PFS events and censoring (Section 5).

It is a **compound AI system**, not a single model. It combines deterministic preprocessing,
two large-language-model (LLM) classification steps, a tool-using LLM extraction agent with one
self-critique pass, and deterministic post-processing. LLMs are used for reading, retrieval and
clinical interpretation; line counting, notation and rule application are deterministic. **All
performance claims in this card refer to the end-to-end evaluated configuration** (Section 1),
not to any underlying foundation model in isolation. No model pretraining, fine-tuning,
reinforcement learning or architecture modification was performed.

Setting: retrospective, single-institution (The University of Texas MD Anderson Cancer Center),
colorectal cancer (CRC) and pancreatic cancer.

## 3. Intended use

- **Intended task:** retrospective abstraction of LoT and PFS from EHR documents to support
  real-world-evidence research.
- **Intended users:** clinical researchers and trained abstractors, working under qualified
  clinician review (Section 16).
- **Intended population:** adult patients with colorectal or pancreatic cancer who received SACT
  and whose records are in the institutional EHR, within the populations validated in Section 10.
- **Intended setting:** offline, batch processing in a governed research data environment.
- **Intended outputs use:** an abstraction with linked evidence quotes for cohort-level research analyses (e.g., real-world PFS by line).

## 4. Out-of-scope and prohibited uses

- Any **direct clinical decision-making** for an individual patient, including treatment selection,
  eligibility, or response assessment.
- Use as a **substitute for RECIST** or other formal radiological response criteria.
- Use as a **replacement for clinician or abstractor review**.
- **Prospective or operational deployment** without local validation (Section 17).
- Use in **tumour types, institutions, EHR systems, languages, or time periods** not represented in
  Section 10.
- Use with a **different model, model version, prompt, or code version** than the evaluated
  configuration, without re-validation (Sections 9 and 19).
- Regulatory submissions or label claims based on unreviewed pipeline output.

## 5. Clinical task definitions

- **Line of therapy:** designated per the EnLiST framework. EnLiST represents SACT in separate
  early (`e`), advanced (`a`) and investigational (`i`) registers using X.Y notation, where X is a
  new line and Y a modification of the current line. The pipeline emits clinical setting (`1a`,
  `1bi`, `1bii`), treatment intent (`2a`, `2b`), change type (`New`, `Modified`, `Same`), and an
  X.Y line/sub-line label. The agent identifies the clinical reason for each treatment transition
  (a machine-readable `change_trigger`); the X.Y label is then assigned **deterministically** by
  the notation post-processor, not by the LLM.
- **Date uncertainty:** when the record does not support an exact date, the date is represented
  with earliest and latest bounds supported by the documentation. The uncertainty code reflects
  **documentation precision, not the model's probability of being correct**. The same scale was
  used by the clinician reviewers.

  | Code | Definition (reviewer scale) | Pipeline derivation from interval width |
  |---|---|---|
  | `U0` | Clear evidence: complete date on record | ≤ 1 day |
  | `U1` | Minor uncertainty: approximate day known within ~14 days, but exact date not in record (some calculation or estimation required) | ≤ 14 days |
  | `U2` | Moderate uncertainty: date unknown, but estimable within ~1 month | ≤ 31 days |
  | `U3` | High uncertainty: date unknown, within ~3-month range | ≤ 93 days |
  | `U4` | Very high uncertainty: beyond 3-month range | > 93 days |

  If a date has no bounds, the pipeline keeps the agent's own U-code; if there is none, it
  assigns `U2`.
- **PFS interval:** each EnLiST treatment interval begins on the date of first systemic therapy
  administration.
- **Progression (PFS event):** a radiology read flagged as a progression candidate is treated as
  **radiological evidence only**. It becomes a progression event only when the treating oncology
  team documents clinical progression or changes management because of the finding (treating
  clinicians: physician, physician assistant, nurse practitioner, fellow, or resident).
  Progression may also be established by the treating clinician without available institutional
  imaging. Equivocal language, radiologist-only re-reads, stability/response conclusions, and
  historical recurrences are not corroboration. Death from any cause is an event if the interval
  has not already ended.
- **PFS within a line ("temporal race"):** within each line's window, whichever comes first
  decides the outcome. A corroborated progression is a PFS event (`first_pd_date`, flag = 1). A
  local therapy is a censor (`Local therapy no PD`). A switch of therapy without corroborated
  progression (including toxicity-related changes) is a censor (`Switched no PD`). A completed
  line followed by recurrence, with no intervening line, is an event at the recurrence.
  Censoring uses a prespecified evidence hierarchy.
- **Full rule set:** the complete LoT, progression and censoring specification is given in the
  manuscript's Supplementary Note. The agent's system prompt at the evaluated commit is the
  operative implementation and is released with the code.

## 6. System components (evaluated configuration)

| Stage | Component | Method | Model |
|---|---|---|---|
| 1 | Note preparation | Deterministic. Keeps progress/clinical notes and drops low-signal note types (discharge instructions, pre-procedure instructions, ED provider notes, discharge summaries, telephone encounters, advance-care-planning notes, brief op notes, and progress notes with no author type). Strips boilerplate and research-coordinator questionnaires, extracts oncology/chemotherapy-history sections, and de-duplicates copy-forward chemotherapy-summary paragraphs per patient. Unrecognised clinical text is retained. | None |
| 2 | Imaging classification | Each radiology impression (or the full report if there is no impression) is classified as `PROGRESSION_CANDIDATE`, `NO_PROGRESSION`, `INDETERMINATE`, or `NOT_RELEVANT`. Unparseable replies default to `INDETERMINATE`. | Claude Haiku 4.5, temperature 0, max 16 output tokens |
| 2.5 | Clinical-progression corroboration | For each `PROGRESSION_CANDIDATE`, reads the next ≤5 treating-clinician notes on or after the scan date (the last 5,000 characters of each, where the assessment & plan sits) and returns corroborated yes/no, date, evidence note, verbatim quote, and a one-sentence rationale. | Claude Haiku 4.5, temperature 0, max 400 output tokens |
| 3 | LoT/PFS extraction agent | ReAct-style reasoning and retrieval loop. Per patient, the agent is given the oncology history, treatment summaries, chemotherapy-history blocks, the classified imaging trajectory, corroborated progression events, and a notes index. It retrieves note excerpts through a single `get_notes` tool (≤8 new excerpts per call, ≤18 turns). It writes a draft timeline, then performs **exactly one self-critique pass** (with retrieval still available; any revision must be supported by a retrieved note) before its final answer. Every decision carries structured evidence (`decision`, `note_id`, verbatim `excerpt`, `why`). | Claude Opus 5 (default temperature; max 32,000 output tokens) |
| 3 post | Notation | Deterministic, with no second LLM judgment. Applies the EnLiST state-transition rules and assigns X.Y labels, derives U-codes from date intervals, derives reason-for-switching, and enforces consistency and enumerated-field formatting. | None |
| 4 | Evaluation (not part of the deployed system) | Needleman–Wunsch global alignment of agent lines to each reference standard (preserving temporal order), then per-field comparison on matched pairs (Section 11). | None |

## 7. Inputs

- **Source:** one denormalised table of institutional EHR documents, one row per document, from
  these sources: oncology history, treatment summaries, pathology (surgical/biopsy/molecular),
  progress/clinical notes (non-sensitive and sensitive), and radiology (impression and report).
- **Required fields:** patient identifier, document identifier, document type, authoring-provider
  type, document dates (service, exam, treatment), document text, radiology impression text,
  treatment fields, and verified death date where available.
- **Volume in the evaluation cohort:** records span 2003–2026, with a mean of 262 documents and
  213,022 words per patient (median 268 documents, IQR 153–399; median 195,454 words, IQR
  125,373–279,034; range 17,954–533,224 words).
- **EHR systems:** records span the institution's pre-Epic EHR and Epic (transition March 2016),
  with no EHR-specific branching in the pipeline.
- **Known input constraints:** care received outside the institution is captured only as
  documented in institutional notes; outside imaging may not be available as structured reports.

## 8. Outputs

One row per line/sub-line per patient. Key fields:

- **Line:** `entry_order`, `clinical_setting`, `treatment_intent`, `enlist_lot_label`,
  `enlist_composite_label`, `lot_change_type`, `change_trigger`, `reason_for_switching`.
- **Regimen:** `systemic_anticancer_agents`, `maintenance_regimens`, and SACT class flags
  (cytotoxic / targeted / immunotherapy / investigational).
- **Dates (each with earliest / latest / U-code):** `start_date`, `stop_date`, `first_pd_date`,
  `censor_date`, `recent_pd_date`.
- **PFS:** `pfs_censor_flag` (1 = event, 0 = censored), `pfs_censor_reason`.
- **Local therapies:** up to three, each with modality, date, and description.
- **Patient context:** initial diagnosis date and stage, metastatic diagnosis date, death date,
  last follow-up.
- **Traceability and audit:** `decision_evidence` (JSON array of verbatim excerpts with source note
  IDs), `reasoning`, `manual_review_flags`, `parse_status`, `error_message`, tool-call log, full
  agent transcript, token counts, cost, model name, prompt version, and processing timestamp.

**Interpretation:** outputs are LLM **abstraction**. They are not a clinical
determination of progression or response. Rows with `manual_review_flags`, a non-success
`parse_status`, or high date-uncertainty codes need priority human review. Treat a missing value
as "not found in the documents", not as evidence that something did not happen.

## 9. Models, prompts, tools, and dependencies

| Component | Model / version | Access | Parameters |
|---|---|---|---|
| Stage 3 agent | Anthropic Claude Opus 5 | Palantir Foundry Language Model Service (generic completion interface), within institutional infrastructure | Default temperature (none sent); max 32,000 output tokens |
| Stages 2, 2.5 | Anthropic Claude Haiku 4.5 | Same | Temperature 0 |

- **Model access date:** 2026-08-27 to 2026-08-31 for the evaluated runs.
- **Prompt:** system and critique prompts as of commit `751b1a4`. The prompt is sent as a single
  string (system and user content concatenated); the completion interface has no separate system
  channel.
- **Retries:** non-rate-limit errors are retried 4 times; rate-limit (HTTP 429) errors are retried
  up to 12 times with capped, jittered backoff. If a call still fails, an error is recorded and no
  output is fabricated.
- **Software:** Python transforms on Palantir Foundry (PySpark). Dependency versions are pinned in
  the repository conda lockfiles at the evaluated commit.
- **Non-determinism:** Stage 3 runs at the provider's default temperature, so repeated runs can
  differ. Five independent repeats were run with Claude Opus 5. The headline results and the
  evidence audit both use **repeat 4**. Run-to-run reproducibility across the five repeats is
  reported in the manuscript (Section 12). The deterministic post-processing gives identical
  results on fixed model output.
- **Model selection:** Claude Opus 5 was selected from a comparison of four reading models, each
  run with the identical preprocessing inputs, retrieval tool, agent, self-critique,
  deterministic adjudication and evaluation code: Claude Opus 4.8 (5 repeats), Claude Opus 5 (5
  repeats), GPT-5.6 Sol (5 repeats) and GPT-5.6 Terra (1 repeat). Claude Opus 5 was selected
  because it showed slightly better agreement on PFS dates. It did not have the highest
  line-recovery F1 or the lowest cost per patient (Extended Data Fig. 5; Supplementary Table 4).

## 10. Development and validation data

- **Institution / setting:** MD Anderson Cancer Center, Houston, TX, USA. Retrospective.
- **Study period and data cutoff:** records from 2003 to 2026. The evaluated run read the input
  snapshot built on 15 July 2026, so no document added to the EHR source after that snapshot was
  available to the pipeline.
- **Development patients:** 4 randomly selected CRC patients (25 lines in oncologist 2's
  abstraction) were used as worked examples during prompt and rule development. They are
  **excluded from all reported performance metrics** and from the evidence audit. No pancreatic
  cancer records or pancreas-specific examples were used during development.
- **Held-out status:** prompts and rules were developed using only the 4 development patients.
  They were **not** tuned on scores or errors from the 26 evaluated patients.
- **Reference standards:** manual chart review by expert gastrointestinal medical oncologists
  (oncologist 1, O1; oncologist 2, O2), using the same written EnLiST and PFS definitions. No
  calibration round was held between reviewers.
  - **CRC:** O1 and O2 each reviewed **the same 20 CRC patients**, independently of each other.
    Disagreements were **not adjudicated**. Each oncologist's abstraction is kept as a separate
    reference standard, and their agreement with each other is reported as the human baseline.
  - **Pancreatic:** O2 alone reviewed 10 pancreatic cancer patients. No patients are shared with
    the CRC set.
- **Blinding:** reviewers had **not** seen pipeline output before or during their review.
- **Review effort:** about one hour of medical-oncologist time per patient (mean 51 minutes;
  median 45; range 15–120).

| Reference standard | Tumour type | Patients (all) | Lines (all) | Patients evaluated (dev excluded) | Lines evaluated (dev excluded) |
|---|---|---|---|---|---|
| Oncologist 1 (O1) | CRC | 20 | 126 | 16 | 99 |
| Oncologist 2 (O2) | CRC | 20 (same patients) | 121 | 16 | 96 |
| Oncologist 2 (O2) | Pancreatic | 10 | 40 | 10 | 40 |

*Evaluated set: 26 distinct patients (16 CRC + 10 pancreatic).*

The evaluated run processed 33 patients. The 30 reference-standard patients are scored, minus the
4 development patients. The other 3 patients have no reference standard and were **not scored**.

- **Cohort characteristics (age, sex, race/ethnicity, stage, number of lines, follow-up):**
  reported in Table 1 of the accompanying manuscript (in preparation; see Section 1). They will be
  available there once the manuscript is released.
- **Model selection on the evaluation set:** the 4-model comparison, which selected Claude Opus 5,
  was scored against the same reference standards. The model choice was therefore made on the
  evaluation patients (Section 13).
- **Validation type:** internal, single institution. Transfer to pancreatic cancer and
  robustness across the pre-Epic → Epic EHR transition were assessed within the institution. No
  external validation was performed.

## 11. Evaluation methods

- **Unit of analysis:** line of therapy, aligned within patient. Patients, not lines, are the
  independent sampling unit.
- **Alignment:** Needleman–Wunsch global alignment between the reference-standard line sequence
  and the agent line sequence for each patient, preserving temporal order. Unmatched
  reference-standard lines are counted as *missed*; unmatched agent lines as *extra*
  (over-segmentation). The same alignment and scoring are used for agent–expert and
  expert–expert comparisons.
- **Line recovery:** primarily sensitivity (matched / reference lines) and PPV (matched / agent
  lines); F1 reported secondarily.
- **Alignment scoring:** a similarity score combines drug-set Jaccard similarity (weight 0.5),
  start-date proximity within 60 days (0.3), and stop-date proximity within 90 days (0.2). The gap
  penalty is −0.5. Lines are ordered by start date; agent lines with neither a regimen nor a LoT
  label are dropped before alignment.
- **Field accuracy:** computed on matched pairs only, with deterministic rules and no LLM
  adjudication.
  - **Regimen:** each regimen is converted to a canonical drug set. Brand names and synonyms
    are mapped to generic names; named regimens (e.g. FOLFOX, CAPOX) are expanded to their
    drugs; leucovorin and placebo are ignored. Two regimens match if their Jaccard similarity is
    ≥ 0.5.
  - **Start, stop, first-progression and censor dates:** the match tolerance is set by the
    **reference-standard** line's uncertainty code for that date:

    | Reference U-code | Tolerance (± days) |
    |---|---|
    | `U0` | 7 |
    | `U1` | 14 |
    | `U2` | 31 |
    | `U3` | 93 |
    | `U4` | 183 |
    | missing / unrecognised | 30 |

  - **Local-therapy date:** fixed tolerance of ± 30 days.
  - **Categorical fields** (LoT label, change type, clinical setting, intent, reason for
    switching, PFS censor reason, local-therapy modality): exact match after normalisation
    (case/whitespace, and code-prefix extraction where applicable).
  - **Binary fields** (PFS event/censor flag, SACT class flags): exact match.
  - **Missing values:** for dates, binary fields and maintenance regimen, *both* missing counts
    as a match and *one* missing as a mismatch. For the main regimen, a pair where either side
    has no recognisable drug is excluded from that field's denominator.
- **Implementation:** the manuscript results were produced with the alignment and scoring code at
  the evaluated commit (`nw_align.py`, unchanged since).
- **Human baseline:** agreement between O1 and O2 on the 16 CRC patients, computed with the
  identical alignment and field methodology. O1 is the reference side, so O1's U-codes set the
  date tolerances. Inter-reviewer agreement is treated as an empirical measure of how
  reproducibly these variables can be reconstructed from the record, not as an error-free gold
  standard.
- **Uncertainty analyses:** agreement of agent dates with expert dates, stratified by the agent's
  declared U-code, using a prespecified ordered trend analysis. Treatment dates and progression
  dates are analysed separately.
- **Downstream PFS:** identical deterministic endpoint rules were applied to the agent, O1 and O2
  abstractions. Line-specific PFS distributions (Kaplan–Meier medians), restricted mean survival
  time (primary horizon 12 months), paired interval durations and Bland–Altman agreement were
  compared, with sensitivity analyses restricted by documentation precision and supported date
  bounds.
- **Evidence audit:** a prespecified, stratified random sample of 100 note-anchored evidence
  spans from the evaluated run (27 CRC-LoT, 23 CRC-PFS, 27 pancreas-LoT, 23 pancreas-PFS), drawn in
  two batches (30 + 70) using a fixed-seed hash ordering, with development patients excluded.
  Provenance was checked mechanically (verbatim presence, case- and whitespace-normalised, in the
  identified source document) and semantically (whether the excerpt supports the attached value,
  judged by a board-certified oncologist).
- **Confidence intervals / statistics:** 95% CIs from patient-clustered bootstrap resampling,
  keeping all lines of each sampled patient. Agent–expert results are interpreted descriptively
  relative to inter-reviewer reproducibility; **no formal equivalence or non-inferiority testing
  was performed**. Stratified analyses by line number and documentation era are descriptive.
- **Run-to-run variability:** 5 repeats were run for each model except GPT-5.6 Terra (1 run).
  Headline results use Opus 5 repeat 4. Reproducibility across the five Opus 5 repeats is
  reported (identical complete LoT sequences, lines recovered by all runs, pairwise run-to-run
  F1).

## 12. Performance

Quantitative results are **not reproduced in this card**. The manuscript and supplement are the
single authoritative source, so the two documents cannot drift apart.

| Result | Location in manuscript |
|---|---|
| Line recovery (sensitivity, PPV, F1) vs O1, O2, and O1 vs O2 (CRC) | Fig. 2a; Extended Data Fig. 1 |
| Field agreement on aligned lines (dates, setting, intent, regimen, change type) | Fig. 2b; Extended Data Table 1; Supplementary Table 7 |
| PFS event/censor classification and progression-date agreement | Fig. 2c; Supplementary Fig. 1 (censoring reasons) |
| Declared uncertainty vs date error | Fig. 3a–b; Supplementary Table 6 |
| Evidence provenance (verbatim recovery; semantic support in 100-span sample) | Fig. 3d; Supplementary Table 1 |
| Downstream PFS (medians, RMST, paired durations, Bland–Altman), TTNT, TTD | Fig. 4; Extended Data Fig. 2; Supplementary Fig. 2; Supplementary Table 2 |
| Pancreatic cancer transfer; pre-Epic vs Epic | Extended Data Fig. 3 |
| Residual errors and ablations | Extended Data Fig. 4; Supplementary Table 3 |
| Model comparison, cost per patient, run-to-run reproducibility, pre-EnLiST comparison | Extended Data Fig. 5; Supplementary Table 4; Results text |
| Performance by line number | Supplementary Fig. 3 |
| Demographic subgroup analyses | Not evaluated (sample too small) |

**What the reported results cover:**

- **System:** the evaluated configuration only (Claude Opus 5, repeat 4, commit `751b1a4`;
  Sections 1, 9 and 19).
- **Patients:** 16 CRC patients (each reviewed by both O1 and O2) and 10 pancreatic patients
  (O2), with the 4 development patients excluded (Section 10).
- **Method:** the alignment and scoring method in Section 11. Each reference standard is scored
  separately; there is no adjudicated consensus.
- **Uncertainty:** the evaluation sets are small, so point estimates should be read together with
  their confidence intervals in the manuscript. There is no human baseline for pancreatic cancer.
- **Variability:** the headline results come from one run of a non-deterministic system;
  run-to-run reproducibility is reported separately (Section 11).

## 13. Limitations

- Single academic health system and retrospective, with small evaluation sets: 16 CRC patients
  (96–99 lines, after excluding 4 development patients) and 10 pancreatic patients (40 lines).
  These cannot support formal equivalence testing or subgroup-specific performance estimates.
- No adjudicated consensus reference standard. Inter-reviewer disagreement limits how precisely
  the pipeline's error can be measured.
- The pancreatic reference standard has a single reviewer, so no human baseline exists for
  pancreatic cancer.
- The PFS definition extends EnLiST with study-defined choices (clinician corroboration of
  radiographic progression; censoring at treatment changes without progression, such as local
  therapy). These choices affect event-versus-censor classification and LoT-specific PFS.
- Progression and censoring are less reproducible than line structure, for both the agent and
  the oncologists. Disagreement is driven mainly by *whether* a progression event occurred
  rather than its timing.
- Ascertainment depends on documentation: events at outside institutions, or events not
  documented by a treating clinician, may be missed or mis-dated. Reliability is lower in later
  lines, where documentation, surveillance and outside care are less complete.
- Progression is clinician-anchored, not RECIST, so it may differ from trial-style PFS.
- The headline results come from **one of five repeats** (repeat 4) of a non-deterministic
  system. No prespecified rule selected that repeat. A future run may score higher or lower; the
  reported run-to-run reproducibility shows how much results vary.
- The model (Claude Opus 5) was selected using the same evaluation patients, so the reported
  performance may be somewhat optimistic relative to new patients.
- The hosted proprietary models may change or be retired, which limits exact reproducibility.
- Validated only for CRC and pancreatic cancer at one institution. Formal external validation
  remains necessary; it requires date-preserving multi-institutional collaboration, because the
  longitudinal dates needed for LoT and PFS are themselves protected health information.

## 14. Biases and known failure modes

- **Documentation bias:** patients with fewer or shorter notes (e.g., shared care with outside
  oncologists) may get less complete timelines.
- **Segmentation errors:** over-splitting a single line (e.g., a resumed or de-escalated backbone)
  or merging two distinct regimens.
- **Copy-forward text:** cumulative chemotherapy-history paragraphs can carry outdated statements.
  Stage 1 de-duplicates exact copies only.
- **Progression misattribution:** selecting a different clinical event as progression than an
  expert would, assigning an event to the wrong line window, or taking equivocal imaging language
  as progression (mitigated, not eliminated, by clinician corroboration).
- **Note truncation:** Stage 2.5 reads only the last 5,000 characters of each clinician note, so
  progression documented only early in a long note can be missed.
- **Unsupported or non-verbatim evidence:** the agent may cite an excerpt that is paraphrased or
  assembled from multiple passages rather than verbatim, or one that does not support the
  decision (quantified by the evidence audit; see Section 12).
- **Imaging-label defaulting:** an unparseable classifier reply is labelled `INDETERMINATE`.
- **Uncertainty-code defaulting:** a date with neither bounds nor an agent-supplied U-code is
  assigned `U2`.
- **Rare regimens, clinical trials, and investigational agents:** may be under-represented in the
  reference standards.
- **Demographic subgroup performance:** not evaluated.

## 15. Privacy, security, and data governance

- **Data:** identifiable EHR data (PHI), processed only inside MD Anderson's governed Palantir
  Foundry environment, under markings and access controls inherited from the source data.
- **LLM access:** all model calls go through Palantir's Language Model Service within
  institutional infrastructure. PHI was not sent to public third-party model interfaces. Under
  the applicable agreements, prompts and outputs are **not retained by, or used to train, the
  model providers**.
- **Ethics:** approved by the MD Anderson Cancer Center Institutional Review Board (LAB09-0373)
  with a **waiver of informed consent**; conducted in accordance with the Declaration of
  Helsinki. Patients and the public were not involved in the design or conduct of the study.
- **Data availability:** patient-level source records cannot be shared publicly (PHI, IRB
  restrictions). Aggregate data underlying figures and tables are in the manuscript's Source Data
  and Supplementary Information. Sharing of de-identified derived data is subject to the MD
  Anderson data-access and IRB process.
- **Public release:** the public code release contains **no patient data, patient identifiers, or
  model outputs**. Institution-specific dataset identifiers are replaced with placeholders.
- **Audit trail:** each output row records the model, prompt version, timestamp, tool calls, and
  full agent transcript. These stay in the protected environment.

## 16. Human oversight

- **Required reviewer:** a clinician or trained abstractor, supervised by a clinician, with
  oncology expertise in the relevant tumour type.
- **Reviewer materials:** the timeline, per-decision verbatim evidence with source note IDs,
  reasoning, date-uncertainty codes, and manual-review flags.
- **Mandatory review:** lines with manual-review flags, parse errors, high-uncertainty dates
  (`U3`/`U4`), PFS events, and any patient where the run failed.
- **Accountability:** the final abstraction is the responsibility of the human reviewer and study
  team, not the system.

## 17. Recommendations for deployment

Before any use beyond the evaluated setting:

1. **Local validation** against a locally abstracted reference standard, with patient-level
   separation from any patients used for prompt tuning.
2. **Pin the configuration** (code commit, prompts, model versions) and re-validate after **any**
   change to the model, prompt, preprocessing rules, or source-data feed.
3. **Ongoing audit sampling** of outputs and evidence spans, with performance monitoring against
   pre-set, locally defined thresholds.
4. **Monitor for drift** in note templates, EHR systems, documentation practice, treatment
   standards, and hosted model behaviour.
5. **Change control and rollback:** keep the evaluated configuration available and able to be
   re-run.
6. **Prospective evaluation** is required before any use that informs patient care. That use would
   also require appropriate regulatory and institutional review.

## 18. Ethical and regulatory considerations

- Research tool. Not cleared or approved by any regulator.
- Potential harms if misused: wrong PFS estimates feeding research conclusions, and
  misattribution of progression. Mitigated by mandatory human review and the out-of-scope
  restrictions above.
- Oversight: MD Anderson Cancer Center IRB, protocol LAB09-0373 (waiver of informed consent).

## 19. Reproducibility and change history

- **Evaluated snapshot:** commit `751b1a4`. This includes the system and critique prompts, Stage
  1–4 code, and the conda lockfiles. The public release (system prompt, deterministic EnLiST
  adjudication, sequence alignment and scoring code; MIT licence) carries a version tag for this
  frozen implementation.
- **Input data version:** the evaluated run (2026-08-31) read the EHR input snapshot built on
  **15 July 2026**.

| Card version | Date | Change |
|---|---|---|
| 0.1 | 2026-10-04 | Initial |

## 20. MI-CLAIM-GEN crosswalk

> The manuscript's reporting was guided by STROBE, MI-CLAIM-GEN and TRIPOD-LLM. The table maps
> MI-CLAIM-GEN reporting areas to this card and the manuscript; it is not an item-by-item
> checklist.

| MI-CLAIM-GEN reporting area | Model card section | Manuscript / supplement location |
|---|---|---|
| Study design and clinical question | 2, 3, 5 | Introduction; Methods (Study design and cohorts) |
| Data sources, cohort, and splits | 7, 10 | Methods (Study design and cohorts); Fig. 1 |
| Model / system description and versions | 6, 9 | Methods (LLM agentic pipeline); Supplementary Note |
| Prompting and configuration | 6, 9, 19 | Supplementary Note; released code |
| Reference standard | 10 | Methods (Expert reference standard and evaluation) |
| Evaluation metrics and statistics | 11 | Methods (Expert reference standard; Statistical analysis) |
| Results, including variability | 12 | Results; Figs 2–4; Extended Data Figs 1–5 |
| Limitations, bias, and failure modes | 13, 14 | Discussion (limitations); Extended Data Fig. 4 |
| Privacy and ethics | 15, 18 | Methods (ethics statement); Data availability |
| Human oversight and deployment | 16, 17 | Model card only |
| Reproducibility / code availability | 19 | Code availability statement |
