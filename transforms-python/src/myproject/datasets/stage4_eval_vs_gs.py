"""
Stage 4 -- NW-aligned accuracy metrics vs clinician gold standards.

Aligns the agent's episode timeline (enlist_agent_timeline) to each clinician
gold standard using Needleman-Wunsch global alignment (respects temporal order),
then compares every field on matched pairs

Two outputs:
  enlist_eval_alignment      -- one row per aligned pair (matched / gs_missed / agent_extra)
                                with per-field match booleans and mismatch reasons.
  enlist_eval_field_accuracy -- per-field accuracy % per reviewer (on matched pairs),
                                plus line-detection sensitivity / PPV / F1.

Regimen equivalence is deterministic (Jaccard >= 0.5 on canonical drug sets = lenient match).
"""
import pandas as pd
from transforms.api import transform, Input, Output

from myproject import config as C
from myproject import nw_align as NW

# Fields compared on matched pairs (label shown in output).
_MATCH_FIELDS = [spec[0] for spec in NW.FIELD_SPECS]


def _align_reviewer(agent_df, gs_df, reviewer):
    """Return (detail_rows, field_stats) for one reviewer."""
    common = sorted(set(agent_df["mrn_norm"].unique()) & set(gs_df["mrn_norm"].unique()))
    detail_rows = []
    # per-field counters
    fstats = {f: {"n": 0, "match": 0} for f in _MATCH_FIELDS}
    n_matched = n_gs = n_agent = 0

    for mrn in common:
        if not mrn:
            continue
        gs_lines = NW.extract_lines(gs_df, mrn)
        agent_lines = NW.extract_lines(agent_df, mrn)
        n_gs += len(gs_lines)
        n_agent += len(agent_lines)
        alignment = NW.needleman_wunsch(gs_lines, agent_lines)

        for gs_idx, ag_idx in alignment:
            rec = {"mrn_norm": mrn, "reviewer": reviewer,
                   "gs_line_count": len(gs_lines), "agent_line_count": len(agent_lines)}
            if gs_idx is not None and ag_idx is not None:
                n_matched += 1
                g, a = gs_lines[gs_idx], agent_lines[ag_idx]
                rec["alignment_type"] = "matched"
                rec["gs_lot_label"] = g["lot_label"]
                rec["agent_lot_label"] = a["lot_label"]
                rec["gs_regimen"] = g["regimen"]
                rec["agent_regimen"] = a["regimen"]
                rec["gs_start"] = str(g["start"].date()) if g["start"] else None
                rec["agent_start"] = str(a["start"].date()) if a["start"] else None
                failed = []
                for fname, kind, tol in NW.FIELD_SPECS:
                    m = NW.compare_field(kind, tol, g[fname], a[fname])
                    rec[f"match_{fname}"] = m
                    if m is not None:
                        fstats[fname]["n"] += 1
                        if m:
                            fstats[fname]["match"] += 1
                        else:
                            failed.append(fname)
                rec["mismatch_reasons"] = "; ".join(failed) if failed else None
            elif gs_idx is not None:
                g = gs_lines[gs_idx]
                rec["alignment_type"] = "gs_missed"
                rec["gs_lot_label"] = g["lot_label"]
                rec["agent_lot_label"] = None
                rec["gs_regimen"] = g["regimen"]
                rec["agent_regimen"] = None
                rec["gs_start"] = str(g["start"].date()) if g["start"] else None
                rec["agent_start"] = None
                rec["mismatch_reasons"] = "gs_line_missed_by_agent"
            else:
                a = agent_lines[ag_idx]
                rec["alignment_type"] = "agent_extra"
                rec["gs_lot_label"] = None
                rec["agent_lot_label"] = a["lot_label"]
                rec["gs_regimen"] = None
                rec["agent_regimen"] = a["regimen"]
                rec["gs_start"] = None
                rec["agent_start"] = str(a["start"].date()) if a["start"] else None
                rec["mismatch_reasons"] = "agent_extra_line_over_split"
            detail_rows.append(rec)

    # field accuracy summary rows
    field_rows = []
    for f in _MATCH_FIELDS:
        n = fstats[f]["n"]
        mt = fstats[f]["match"]
        field_rows.append({
            "reviewer": reviewer, "field": f,
            "n_compared": n, "n_match": mt,
            "accuracy_pct": round(100.0 * mt / n, 1) if n else None,
        })
    # line-detection metrics
    sens = round(100.0 * n_matched / n_gs, 1) if n_gs else None
    ppv = round(100.0 * n_matched / n_agent, 1) if n_agent else None
    f1 = round(2 * sens * ppv / (sens + ppv), 1) if sens and ppv else None
    field_rows.append({
        "reviewer": reviewer, "field": "_LINE_DETECTION",
        "n_compared": n_gs, "n_match": n_matched, "accuracy_pct": sens,
    })
    field_rows.append({
        "reviewer": reviewer, "field": "_LINE_PPV",
        "n_compared": n_agent, "n_match": n_matched, "accuracy_pct": ppv,
    })
    field_rows.append({
        "reviewer": reviewer, "field": "_LINE_F1",
        "n_compared": None, "n_match": None, "accuracy_pct": f1,
    })
    return detail_rows, field_rows


def run_eval(ctx, agent_in, reviewer_1_crc, reviewer_2_crc, reviewer_2_panc,
             alignment_out, field_accuracy_out):
    """Shared eval core: NW-align an agent timeline to each gold + emit alignment + field accuracy.
    Reused by the Opus eval and the GPT-5 Terra/Sol eval variants."""
    agent_df = agent_in.pandas()
    agent_df = agent_df[agent_df["mrn"].notna()].copy()
    agent_df["mrn_norm"] = agent_df["mrn"].apply(NW.normalize_mrn)

    def _prep(df):
        df = df[df["mrn"].notna()].copy()
        df["mrn_norm"] = df["mrn"].apply(NW.normalize_mrn)
        return df

    reviewer_1_df = _prep(reviewer_1_crc.pandas())
    reviewer_2_df = _prep(reviewer_2_crc.pandas())
    panc_df = _prep(reviewer_2_panc.pandas())

    all_detail, all_fields = [], []

    # Agent vs each reviewer.
    for name, gs in [("reviewer_1_crc", reviewer_1_df), ("reviewer_2_crc", reviewer_2_df),
                     ("reviewer_2_pancreas", panc_df)]:
        detail, fields = _align_reviewer(agent_df, gs, name)
        all_detail.extend(detail)
        all_fields.extend(fields)

    # HUMAN FLOOR: Reviewer 2 vs Reviewer 1 (CRC) using the identical NW methodology.
    # Treat Reviewer 1 as the "gs" baseline and Reviewer 2 as the "candidate"; the resulting
    # per-field accuracy is the inter-rater agreement ceiling the agent is measured against.
    detail, fields = _align_reviewer(reviewer_2_df, reviewer_1_df, "human_floor_reviewer_2_vs_reviewer_1")
    all_detail.extend(detail)
    all_fields.extend(fields)

    detail_df = pd.DataFrame(all_detail)

    # Convert nullable-boolean match_ columns to strings to avoid Spark's
    # BooleanType/DoubleType merge error (matched rows = bool, gap rows = NaN).
    def _b2s(v):
        if v is True:
            return "true"
        if v is False:
            return "false"
        return None

    for f in _MATCH_FIELDS:
        col = f"match_{f}"
        if col not in detail_df.columns:
            detail_df[col] = None
        detail_df[col] = detail_df[col].map(_b2s)

    field_df = pd.DataFrame(all_fields)

    alignment_out.write_dataframe(ctx.spark_session.createDataFrame(detail_df))
    field_accuracy_out.write_dataframe(ctx.spark_session.createDataFrame(field_df))


@transform(
    agent_in=Input(C.OUT_AGENT_TIMELINE),
    reviewer_1_crc=Input(C.GS_REVIEWER_1_CRC),
    reviewer_2_crc=Input(C.GS_REVIEWER_2_CRC),
    reviewer_2_panc=Input(C.GS_REVIEWER_2_PANCREAS),
    alignment_out=Output(C.OUT_EVAL_ALIGNMENT),
    field_accuracy_out=Output(C.OUT_EVAL_FIELD_ACCURACY),
)
def compute(ctx, agent_in, reviewer_1_crc, reviewer_2_crc, reviewer_2_panc, alignment_out, field_accuracy_out):
    """Eval the Opus agent timeline vs the golds."""
    run_eval(ctx, agent_in, reviewer_1_crc, reviewer_2_crc, reviewer_2_panc, alignment_out, field_accuracy_out)
