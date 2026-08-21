"""
Stage 3 (GPT-5.6 Terra variant) -- identical combined v2.5 agent, model swapped to GPT-5.6 Terra.

Thin wrapper over stage3_agent_extraction.run_stage3 (same orientation, tools, self-critique,
prompt). Output = enlist_agent_timeline_terra, scored by stage4_terra vs the golds. Used only to
A/B the MODEL vs the Opus baseline.
"""
from transforms.api import transform, Input, Output, configure
from palantir_models.transforms import GenericCompletionLanguageModelInput

from myproject import config as C
from myproject.datasets.stage3_agent_extraction import run_stage3


@configure(profile=["KUBERNETES_NO_EXECUTORS"])
@transform(
    output=Output(C.OUT_AGENT_TIMELINE_TERRA),
    source=Input(C.INPUT_DATASET),
    notes_prepped=Input(C.OUT_NOTES_PREPPED),
    imaging=Input(C.OUT_IMAGING_CLASSIFIED),
    model=GenericCompletionLanguageModelInput(C.MODEL_RID_GPT5_TERRA),
)
def compute(ctx, output, source, notes_prepped, imaging, model):
    run_stage3(ctx, output, source, notes_prepped, imaging, model,
               model_name="gpt-5-6-terra", prompt_version="enlist_v2.5_selfcritique_terra",
               max_tokens=C.GPT5_MAX_TOKENS, temperature=C.GPT5_TEMPERATURE,
               max_workers=C.GPT5_MAX_WORKERS, pricing=C.MODEL_PRICING.get(C.MODEL_RID_GPT5_TERRA))
