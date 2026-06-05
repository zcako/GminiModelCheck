from __future__ import annotations

from typing import Any


SCHEME_VERSION = "v1.9"


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def model_zero_capability(model: str, model_version: str | None = None) -> str:
    """Classify how thinkingBudget=0 should behave for a Gemini model."""
    combined = f"{_norm(model)} {_norm(model_version)}"
    if "nothinking" in combined:
        return "rewritten_alias"
    if "flash-lite" in combined and "2.5" in combined:
        return "supports_zero"
    if "flash" in combined and "2.5" in combined and "pro" not in combined:
        return "supports_zero"
    if "pro" in combined:
        return "requires_thinking"
    if "flash" in combined:
        return "flash_compat"
    return "unknown"


def classify_thinking_budget_zero(
    *,
    requested_model: str,
    status: int,
    body: dict[str, Any],
    elapsed: float,
) -> dict[str, Any]:
    model_version = str(body.get("modelVersion") or "")
    capability = model_zero_capability(requested_model, model_version)
    error_message = ""
    error = body.get("error")
    if isinstance(error, dict):
        error_message = str(error.get("message") or "")

    latency_warning = elapsed >= 15.0 or status == -1

    if status == 400:
        if capability == "requires_thinking":
            signal = "strict_reject_expected"
            note = "Pro / requires-thinking model rejected thinkingBudget=0 as expected."
        else:
            signal = "strict_reject"
            note = "The upstream rejected thinkingBudget=0; this is strict but not OAuth evidence."
        oauth_suspect = False
        hard_oauth_evidence = False
    elif status == 200 and "nothinking" in model_version.lower():
        signal = "rewritten_to_nothinking"
        note = "Relay or upstream returned a -nothinking alias; treat as rewrite evidence, not standalone OAuth proof."
        oauth_suspect = capability == "requires_thinking"
        hard_oauth_evidence = False
    elif status == 200 and capability == "supports_zero":
        signal = "zero_supported_accept"
        note = "Flash / Flash-Lite accepts thinkingBudget=0 by official behavior; not OAuth evidence."
        oauth_suspect = False
        hard_oauth_evidence = False
    elif status == 200 and capability == "flash_compat":
        signal = "flash_compat_accept"
        note = "Flash-family compatibility accept; record latency and fields but do not classify as OAuth."
        oauth_suspect = False
        hard_oauth_evidence = False
    elif status == 200 and capability == "requires_thinking":
        signal = "unexpected_accept"
        note = "Requires-thinking model accepted thinkingBudget=0; OAuth or relay rewrite is suspected and needs cross evidence."
        oauth_suspect = True
        hard_oauth_evidence = True
    elif status == -1:
        signal = "timeout_or_network"
        note = "Timeout or network failure; high latency is only auxiliary evidence."
        oauth_suspect = False
        hard_oauth_evidence = False
    else:
        signal = f"status_{status}"
        note = "No standalone conclusion from this status."
        oauth_suspect = False
        hard_oauth_evidence = False

    return {
        "classification_version": SCHEME_VERSION,
        "requested_model": requested_model,
        "modelVersion": model_version,
        "capability": capability,
        "status": status,
        "elapsed_seconds": round(float(elapsed), 3),
        "signal": signal,
        "note": note,
        "error_message": error_message,
        "latency_warning": latency_warning,
        "oauth_suspect": oauth_suspect,
        "hard_oauth_evidence": hard_oauth_evidence,
    }
