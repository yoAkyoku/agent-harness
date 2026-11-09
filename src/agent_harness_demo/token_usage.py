"""Token totals per Run, and a cost estimate from prices the user configured.

Providers report usage per reply (OpenAI-style `prompt_tokens` / `completion_tokens`; the Claude
adapter maps `input_tokens` / `output_tokens` to the same names). A reply without both numbers
is counted as unreported, and an estimate that includes such replies is marked partial, never
filled in with a guess. There is no built-in price table: prices change, so they come from
`--price` and are pinned into each Run at admission.
"""

from typing import Any

TOKEN_KEYS = ("input_tokens", "output_tokens", "reported_responses", "unreported_responses")


def add_token_usage(totals: dict[str, Any], reported: dict[str, Any] | None, *, count: bool = True) -> dict[str, int]:
    """`totals` plus one reply's usage; with `count=False`, only normalise `totals`."""
    result = {key: int(totals.get(key, 0)) for key in TOKEN_KEYS}
    if not count:
        return result
    prompt = (reported or {}).get("prompt_tokens")
    completion = (reported or {}).get("completion_tokens")
    if isinstance(prompt, int) and isinstance(completion, int) and prompt >= 0 and completion >= 0:
        result["input_tokens"] += prompt
        result["output_tokens"] += completion
        result["reported_responses"] += 1
    else:
        result["unreported_responses"] += 1
    return result


def estimated_cost(tokens: dict[str, int], price_per_mtok: dict[str, float]) -> dict[str, Any]:
    """US dollars for the reported tokens at the pinned price; `partial` when some replies reported none."""
    usd = (tokens["input_tokens"] * price_per_mtok["input"] + tokens["output_tokens"] * price_per_mtok["output"]) / 1_000_000
    return {"usd": round(usd, 6), "partial": tokens["unreported_responses"] > 0}
