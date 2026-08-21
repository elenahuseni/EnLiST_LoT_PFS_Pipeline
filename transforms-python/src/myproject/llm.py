"""
Thin wrapper around a Palantir-provided Claude model via the GENERIC completion
interface (the only path enabled for Anthropic models on this enrollment).

GenericCompletionRequest takes a SINGLE prompt string; there is no separate
system/user channel, so we concatenate system + user into one prompt. The model
is invoked with model.create_completion(request); reply text is on
response.completion (fallback .text), token usage on response.usage
(Anthropic naming: input_tokens / output_tokens).
"""
import json
import time

from language_model_service_api.languagemodelservice_api_completion_v3 import (
    GenericCompletionRequest,
)

MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 2.0


def build_prompt(system: str, user: str) -> str:
    """Combine system + user into one prompt for the generic completion API."""
    if system and user:
        return f"{system}\n\n=== INPUT ===\n{user}"
    return system or user or ""


def complete_with_trace(model, system: str, user: str, max_tokens: int,
                        temperature=0.0, pricing: dict = None) -> dict:
    """
    Run one completion (with retry) and return an auditable trace dict:
      {text, latency_ms, prompt_tokens, completion_tokens, total_tokens, cost_usd, error}
    Never raises: on final failure, `error` is populated and `text` is "".
    """
    prompt = build_prompt(system, user)
    t0 = time.time()
    last_exc = None

    for attempt in range(MAX_RETRIES):
        try:
            # Some models (e.g. Claude Opus 4.8) reject `temperature` -> only send it
            # when explicitly set (temperature is not None).
            kwargs = {"max_tokens": max_tokens}
            if temperature is not None:
                kwargs["temperature"] = temperature
            request = GenericCompletionRequest(prompt, **kwargs)
            response = model.create_completion(request)
            latency_ms = int((time.time() - t0) * 1000)

            text = _extract_text(response)
            pt, ct, tt = _extract_usage(response)
            if pt is None:
                pt = max(1, len(prompt) // 4)
            if ct is None:
                ct = max(1, len(text or "") // 4)
            if tt is None:
                tt = (pt or 0) + (ct or 0)

            return {
                "text": text,
                "latency_ms": latency_ms,
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "total_tokens": tt,
                "cost_usd": estimate_cost(pricing, pt, ct),
                "error": None,
            }
        except Exception as e:  # noqa: BLE001 - record, retry, continue
            last_exc = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))

    return {
        "text": "",
        "latency_ms": int((time.time() - t0) * 1000),
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
        "error": f"{type(last_exc).__name__}: {last_exc}",
    }


def _extract_text(response) -> str:
    for path in (
        lambda r: r.completion,
        lambda r: r.text,
        lambda r: r.choices[0].message.content,
        lambda r: r if isinstance(r, str) else None,
    ):
        try:
            val = path(response)
            if val:
                return str(val).strip()
        except Exception:
            continue
    return ""


def _extract_usage(response):
    usage = getattr(response, "usage", None) or response
    pt = _first_attr(usage, ["input_tokens", "prompt_tokens", "input_token_count", "prompt_token_count"])
    ct = _first_attr(usage, ["output_tokens", "completion_tokens", "output_token_count", "completion_token_count"])
    tt = _first_attr(usage, ["total_tokens", "total_token_count"])
    if tt is None and pt is not None and ct is not None:
        tt = pt + ct
    return pt, ct, tt


def _first_attr(obj, names):
    for n in names:
        v = getattr(obj, n, None)
        if isinstance(v, int):
            return v
    return None


def estimate_cost(pricing: dict, prompt_tokens, completion_tokens):
    if not pricing or prompt_tokens is None or completion_tokens is None:
        return None
    cost = (
        (prompt_tokens / 1_000_000.0) * pricing.get("input_per_1m", 0.0)
        + (completion_tokens / 1_000_000.0) * pricing.get("output_per_1m", 0.0)
    )
    return round(cost, 6)


def salvage_objects(s: str):
    """Extract every complete top-level {...} object from a (possibly truncated)
    string, string-aware so braces inside quotes are ignored."""
    objs = []
    depth = 0
    start = None
    in_str = False
    esc = False
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        objs.append(json.loads(s[start:i + 1]))
                    except Exception:
                        pass
                    start = None
    return objs
