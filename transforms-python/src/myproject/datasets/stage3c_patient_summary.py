"""
Stage 3c -- patient-level EnLiST composite summary (one row per patient).

Rolls up the per-episode enlist_agent_timeline into the ESMO EnLiST patient-level reporting
format: "[eLoT X.Y + aLoT X.Y] + iLoT X.Y" (0.0 for an unused track). Per track, X = total New
LoTs to date and Y = Modified LoTs since the most recent New. The patient value is the cumulative
end-state, computed here as the max X per track and the max Y at that X (order-independent, so it
does not rely on entry_order being dense/unique).

Output enlist_patient_summary: mrn + composite_label + per-track finals (elot/alot/ilot) +
patient context (diagnosis / mets / deceased / last follow-up) + episode count + audit.
"""
from pyspark.sql import Window, functions as F
from transforms.api import transform, Input, Output

from myproject import config as C


@transform(
    output=Output(C.OUT_PATIENT_SUMMARY),
    timeline=Input(C.OUT_AGENT_TIMELINE),
)
def compute(timeline, output):
    df = timeline.dataframe()

    # --- final X.Y per track (robust to row order): max X, then max Y at that X ---
    lab = (
        df.where(F.col("enlist_lot_label").isNotNull())
        .withColumn("track", F.lower(F.substring(F.col("enlist_lot_label"), 1, 1)))
        .withColumn("X", F.regexp_extract(F.col("enlist_lot_label"), r"[eai](\d+)\.(\d+)", 1).cast("int"))
        .withColumn("Y", F.regexp_extract(F.col("enlist_lot_label"), r"[eai](\d+)\.(\d+)", 2).cast("int"))
        .where(F.col("track").isin("e", "a", "i"))
    )
    w = Window.partitionBy("mrn", "track").orderBy(
        F.col("X").desc_nulls_last(), F.col("Y").desc_nulls_last()
    )
    top = (
        lab.withColumn("_rn", F.row_number().over(w))
        .where(F.col("_rn") == 1)
        .withColumn("xy", F.concat_ws(".", F.col("X"), F.col("Y")))
        .select("mrn", "track", "xy")
    )
    piv = top.groupBy("mrn").pivot("track", ["e", "a", "i"]).agg(F.first("xy"))
    piv = (
        piv.withColumn("elot", F.coalesce(F.col("e"), F.lit("0.0")))
        .withColumn("alot", F.coalesce(F.col("a"), F.lit("0.0")))
        .withColumn("ilot", F.coalesce(F.col("i"), F.lit("0.0")))
        .drop("e", "a", "i")
        .withColumn(
            "composite_label",
            F.concat(
                F.lit("[eLoT "), F.col("elot"),
                F.lit(" + aLoT "), F.col("alot"),
                F.lit("] + iLoT "), F.col("ilot"),
            ),
        )
    )

    # --- episode count per patient (labeled rows only) ---
    cnt = (
        df.where(F.col("enlist_lot_label").isNotNull())
        .groupBy("mrn").agg(F.count(F.lit(1)).alias("n_episodes"))
    )

    # --- patient-level context: constant per patient; take the first row per mrn ---
    wpf = Window.partitionBy("mrn").orderBy(F.col("entry_order").asc_nulls_last())
    pf = (
        df.withColumn("_rn", F.row_number().over(wpf))
        .where(F.col("_rn") == 1)
        .select(
            "mrn", "cancer_type", "initial_diagnosis_date", "stage_initial",
            "metastatic_diagnosis_date", "deceased_date", "last_follow_up_date",
            "model_name", "prompt_version", "processed_at_utc",
        )
    )

    out = (
        pf.join(piv, "mrn", "left").join(cnt, "mrn", "left")
        .withColumn("elot", F.coalesce(F.col("elot"), F.lit("0.0")))
        .withColumn("alot", F.coalesce(F.col("alot"), F.lit("0.0")))
        .withColumn("ilot", F.coalesce(F.col("ilot"), F.lit("0.0")))
        .withColumn(
            "composite_label",
            F.coalesce(F.col("composite_label"), F.lit("[eLoT 0.0 + aLoT 0.0] + iLoT 0.0")),
        )
        .withColumn("n_episodes", F.coalesce(F.col("n_episodes"), F.lit(0)))
        .select(
            "mrn", "cancer_type", "composite_label", "elot", "alot", "ilot", "n_episodes",
            "initial_diagnosis_date", "stage_initial", "metastatic_diagnosis_date",
            "deceased_date", "last_follow_up_date",
            "model_name", "prompt_version", "processed_at_utc",
        )
    )
    output.write_dataframe(out)
