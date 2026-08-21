"""Stage 4 eval for the GPT-5.6 Terra variant -- NW-align enlist_agent_timeline_terra vs golds."""
from transforms.api import transform, Input, Output

from myproject import config as C
from myproject.datasets.stage4_eval_vs_gs import run_eval


@transform(
    agent_in=Input(C.OUT_AGENT_TIMELINE_TERRA),
    reviewer_1_crc=Input(C.GS_REVIEWER_1_CRC),
    reviewer_2_crc=Input(C.GS_REVIEWER_2_CRC),
    reviewer_2_panc=Input(C.GS_REVIEWER_2_PANCREAS),
    alignment_out=Output(C.OUT_EVAL_ALIGNMENT_TERRA),
    field_accuracy_out=Output(C.OUT_EVAL_FIELD_ACCURACY_TERRA),
)
def compute(ctx, agent_in, reviewer_1_crc, reviewer_2_crc, reviewer_2_panc, alignment_out, field_accuracy_out):
    run_eval(ctx, agent_in, reviewer_1_crc, reviewer_2_crc, reviewer_2_panc, alignment_out, field_accuracy_out)
