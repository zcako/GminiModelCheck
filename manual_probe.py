#!/usr/bin/env python3
"""Manual low-frequency probes for Gemini relay channel diagnosis.

This script mirrors the decision-tree probes in README without storing secrets
in source code. It is intentionally stdlib-only and safe to import in tests.
"""
from __future__ import annotations

import argparse
import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


DEFAULT_MODEL = "gemini-3.1-pro-preview"
DEFAULT_SIG_MODEL = "gemini-3-flash-preview"

INTERESTING_HEADERS = [
    "x-gemini-service-tier",
    "x-routing-group",
    "x-routing-priority",
    "x-routing-source",
    "x-new-api-version",
    "x-shellapi-request-id",
    "x-oneapi-request-id",
    "x-vertex-ai-version",
    "x-accel-buffering",
    "server-timing",
    "server",
    "alt-svc",
    "via",
    "x-request-id",
    "cf-ray",
]

KNOWLEDGE_QUESTIONS = [
    (
        "election2024",
        "In one short sentence, without web search: who won the 2024 United States presidential election?",
        ["trump"],
    ),
    (
        "olympics2024",
        "In one short sentence, without web search: which country won the most gold medals at the 2024 Paris Summer Olympics?",
        ["united states", "china", "u.s.", "us "],
    ),
    (
        "iphone16",
        "In one short sentence, without web search: what model name did Apple announce as their flagship phone in September 2024?",
        ["iphone 16"],
    ),
]


@dataclass
class Config:
    base: str
    key: str
    model: str
    sig_model: str
    timeout: int
    retries: int
    gap: float
    self_sig_n: int
    steps: list[str]


def parse_args(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(description="Run manual low-frequency Gemini relay probes.")
    parser.add_argument("steps", nargs="*", help="Steps to run, e.g. 1a 1b 2a. Empty means all steps.")
    parser.add_argument("--base", required=True, help="Relay base URL, e.g. https://relay.example.com")
    parser.add_argument("--key", required=True, help="API key for this relay. Do not commit it.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Probe model, default {DEFAULT_MODEL}")
    parser.add_argument("--sig-model", default=DEFAULT_SIG_MODEL, help=f"thoughtSignature model, default {DEFAULT_SIG_MODEL}")
    parser.add_argument("--timeout", type=int, default=90, help="Per-request timeout seconds")
    parser.add_argument("--retries", type=int, default=3, help="Retry count for HTTP 429")
    parser.add_argument("--gap", type=float, default=3.0, help="Seconds to sleep between steps")
    parser.add_argument("--self-sig-n", type=int, default=4, help="Self thoughtSignature replay sample count")
    args = parser.parse_args(argv)
    return Config(
        base=normalize_base(args.base),
        key=args.key.strip(),
        model=args.model.strip(),
        sig_model=args.sig_model.strip(),
        timeout=max(1, args.timeout),
        retries=max(1, args.retries),
        gap=max(0.0, args.gap),
        self_sig_n=max(1, args.self_sig_n),
        steps=args.steps,
    )


def normalize_base(base: str) -> str:
    return base.strip().rstrip("/")


def generate_payload(text: str, generation_config: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": text}]}],
    }
    if generation_config:
        payload["generationConfig"] = generation_config
    return payload


def safe_get(data: Any, *keys: Any, default: Any = None) -> Any:
    current = data
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(current, list) and isinstance(key, int) and 0 <= key < len(current):
            current = current[key]
        else:
            return default
        if current is None:
            return default
    return current


def clip(value: Any, limit: int = 240) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    return text[:limit] + ("..." if len(text) > limit else "")


def load_json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {"raw": data}
    except (json.JSONDecodeError, ValueError):
        return {"raw": text}


def call(
    cfg: Config,
    path: str,
    payload: dict[str, Any] | None = None,
    method: str = "POST",
    timeout: int | None = None,
) -> tuple[int, str, dict[str, str]]:
    url = f"{cfg.base}{path}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    for attempt in range(cfg.retries):
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={"x-goog-api-key": cfg.key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or cfg.timeout) as response:
                text = response.read().decode("utf-8", "replace")
                headers = {k.lower(): v for k, v in response.getheaders()}
                return response.status, text, headers
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace") if exc else ""
            headers = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
            if exc.code == 429 and attempt < cfg.retries - 1:
                wait = 8 * (attempt + 1)
                print(f"    [429] backoff {wait}s before retry ({attempt + 1}/{cfg.retries})", flush=True)
                time.sleep(wait)
                continue
            return exc.code, text, headers
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            return -1, json.dumps({"error": "timeout_or_network", "detail": str(exc)}), {}
    return -2, "{}", {}


def select_interesting_headers(headers: dict[str, str]) -> dict[str, str]:
    lowered = {k.lower(): v for k, v in headers.items()}
    selected = {k: lowered[k] for k in INTERESTING_HEADERS if k in lowered}
    for key, value in lowered.items():
        if key.startswith("x-") and key not in selected:
            selected[key] = value
    return selected


def show_headers(headers: dict[str, str]) -> None:
    selected = select_interesting_headers(headers)
    if not selected:
        print("    no interesting headers")
        return
    for key, value in selected.items():
        print(f"    {key}: {value}")


def classify_thinking_budget_result(status: int, data: dict[str, Any], elapsed: float) -> dict[str, Any]:
    model_version = str(data.get("modelVersion") or "")
    if status == 400:
        signal = "strict_reject"
        note = "Vertex/AI Studio style strict rejection"
    elif status == 200 and "nothinking" in model_version.lower():
        signal = "rewritten_to_nothinking"
        note = "relay rewrote request to a -nothinking alias"
    elif status == 200:
        signal = "oauth_accepted_as_is"
        note = "non-Vertex strong signal; OAuth/CLI wrapper is likely"
    elif status == 422:
        signal = "nothinking_alias_rejected"
        note = "relay/model rejected a nothinking alias or rewritten request"
    elif status == -1:
        signal = "timeout_or_network"
        note = "timeout can be an OAuth-wrapper latency symptom"
    else:
        signal = f"status_{status}"
        note = "unexpected status"
    return {
        "signal": signal,
        "note": note,
        "modelVersion": model_version or None,
        "elapsed": round(elapsed, 3),
    }


def extract_text(data: dict[str, Any]) -> str:
    parts = safe_get(data, "candidates", 0, "content", "parts", default=[]) or []
    return "".join(part.get("text", "") for part in parts if isinstance(part, dict))


def step1_baseline(cfg: Config) -> dict[str, Any]:
    print("\n========== Step 1a: generateContent baseline ==========")
    status, body, headers = call(
        cfg,
        f"/v1beta/models/{cfg.model}:generateContent",
        generate_payload("reply with: ok"),
    )
    data = load_json(body)
    usage = safe_get(data, "usageMetadata", default={}) or {}
    print(f"  HTTP {status}")
    show_headers(headers)
    print(f"    modelVersion : {data.get('modelVersion')}")
    print(f"    trafficType  : {usage.get('trafficType')}")
    print(f"    serviceTier  : {usage.get('serviceTier')}")
    print(f"    createTime   : {data.get('createTime')}")
    print(f"    responseId   : {data.get('responseId')}")
    if status != 200:
        print(f"    body: {clip(body, 300)}")
    return {"step": "1a", "status": status, "headers": select_interesting_headers(headers), "body": data}


def step1_error_leak(cfg: Config) -> dict[str, Any]:
    print("\n========== Step 1b: error path leak ==========")
    status, body, headers = call(
        cfg,
        "/v1beta/models/gemini-not-real-9.9:generateContent",
        generate_payload("x"),
        timeout=30,
    )
    data = load_json(body)
    message = safe_get(data, "error", "message", default=body)
    print(f"  HTTP {status}")
    show_headers(headers)
    print(f"    error: {clip(message, 400)}")
    return {"step": "1b", "status": status, "headers": select_interesting_headers(headers), "message": message}


def step1_count_tokens(cfg: Config) -> dict[str, Any]:
    print("\n========== Step 1c: countTokens schema ==========")
    status, body, headers = call(
        cfg,
        f"/v1beta/models/{cfg.model}:countTokens",
        generate_payload("hello world"),
        timeout=30,
    )
    data = load_json(body)
    fields = list(data.keys()) if isinstance(data, dict) else []
    verdict = "endpoint_polluted" if isinstance(data, dict) and "candidates" in data else "count_tokens_response"
    print(f"  HTTP {status}")
    print(f"    fields: {fields}")
    if verdict == "endpoint_polluted":
        print("    -> endpoint_polluted (forwarded to generateContent)")
    return {"step": "1c", "status": status, "headers": select_interesting_headers(headers), "fields": fields, "verdict": verdict}


def step2_thinking_budget_zero(cfg: Config) -> dict[str, Any]:
    print("\n========== Step 2a: thinkingBudget=0 ==========")
    payload = generate_payload(
        "reply with: ok",
        {"thinkingConfig": {"thinkingBudget": 0}},
    )
    start = time.time()
    status, body, headers = call(cfg, f"/v1beta/models/{cfg.model}:generateContent", payload, timeout=90)
    elapsed = time.time() - start
    data = load_json(body)
    result = classify_thinking_budget_result(status, data, elapsed)
    print(f"  HTTP {status}  (latency {elapsed:.1f}s)")
    print(f"    modelVersion: {result.get('modelVersion')}")
    print(f"    signal: {result['signal']} - {result['note']}")
    error_message = safe_get(data, "error", "message", default="")
    if error_message:
        print(f"    error: {clip(error_message, 200)}")
    return {"step": "2a", "status": status, "headers": select_interesting_headers(headers), **result}


def step2_identity(cfg: Config) -> dict[str, Any]:
    print("\n========== Step 2b: identity ==========")
    status, body, headers = call(
        cfg,
        f"/v1beta/models/{cfg.model}:generateContent",
        generate_payload("In one short sentence: which AI model are you, and what is your knowledge cutoff date?"),
        timeout=90,
    )
    data = load_json(body)
    text = extract_text(data)
    print(f"  HTTP {status}")
    print(f"    answer: {clip(text, 300)}")
    flags = [kw for kw in ("antigravity", "openai", "chatgpt", "claude", "anthropic") if kw in text.lower()]
    for flag in flags:
        print(f"    keyword: {flag}")
    return {"step": "2b", "status": status, "headers": select_interesting_headers(headers), "text": text, "flags": flags}


def step3_knowledge(cfg: Config) -> dict[str, Any]:
    print("\n========== Step 3a: knowledge cutoff ==========")
    rows = []
    for question_id, question, keywords in KNOWLEDGE_QUESTIONS:
        payload = generate_payload(
            question,
            {"thinkingConfig": {"thinkingBudget": -1}, "maxOutputTokens": 2048},
        )
        status, body, _ = call(cfg, f"/v1beta/models/{cfg.model}:generateContent", payload, timeout=90)
        data = load_json(body)
        text = extract_text(data)
        outcome = "no_answer"
        if status == 200 and text.strip():
            outcome = "pass" if any(keyword in text.lower() for keyword in keywords) else "fail"
        print(f"  [{question_id}] {outcome}: {clip(text, 120) or f'HTTP {status}'}")
        rows.append({"id": question_id, "status": status, "outcome": outcome, "answer": clip(text, 200)})
        time.sleep(2)
    answered = sum(1 for row in rows if row["outcome"] != "no_answer")
    passed = sum(1 for row in rows if row["outcome"] == "pass")
    verdict = "real_3x" if passed >= 1 else ("model_replaced_or_old" if answered else "inconclusive")
    print(f"  -> pass {passed}/{answered} answered ({len(rows)} total) -> {verdict}")
    return {"step": "3a", "verdict": verdict, "results": rows}


def step3_self_sig(cfg: Config) -> dict[str, Any]:
    print(f"\n========== Step 3b: self thoughtSignature replay (N={cfg.self_sig_n}) ==========")
    prompt = "Think step by step: 7 times 8 = ?"
    follow_up = "Now multiply that by 2"
    pass_count = fail_count = corrupt = step1_fail = no_sig = 0
    for index in range(1, cfg.self_sig_n + 1):
        status1, body1, _ = call(
            cfg,
            f"/v1beta/models/{cfg.sig_model}:generateContent",
            generate_payload(prompt, {"thinkingConfig": {"includeThoughts": True, "thinkingBudget": 1024}}),
            timeout=120,
        )
        if status1 != 200:
            print(f"  [{index}] step1_fail HTTP {status1}")
            step1_fail += 1
            time.sleep(2)
            continue
        parts = safe_get(load_json(body1), "candidates", 0, "content", "parts", default=[]) or []
        if not any(isinstance(part, dict) and "thoughtSignature" in part for part in parts):
            print(f"  [{index}] no_sig")
            no_sig += 1
            time.sleep(2)
            continue
        replay_payload = {
            "contents": [
                {"role": "user", "parts": [{"text": prompt}]},
                {"role": "model", "parts": parts},
                {"role": "user", "parts": [{"text": follow_up}]},
            ],
            "generationConfig": {"thinkingConfig": {"includeThoughts": True, "thinkingBudget": 1024}},
        }
        status2, body2, _ = call(cfg, f"/v1beta/models/{cfg.sig_model}:generateContent", replay_payload, timeout=120)
        if status2 == 200:
            print(f"  [{index}] PASS")
            pass_count += 1
        else:
            error = safe_get(load_json(body2), "error", "message", default="")
            if "orrupted" in error:
                corrupt += 1
            print(f"  [{index}] FAIL({status2}) {clip(error, 80)}")
            fail_count += 1
        time.sleep(2)
    pool = "multi_pool" if corrupt else "single_pool" if fail_count == 0 and step1_fail == 0 else "no_sig_returned" if no_sig else "unstable"
    print(f"  -> PASS={pass_count} FAIL={fail_count} corrupt={corrupt} no_sig={no_sig} -> {pool}")
    return {
        "step": "3b",
        "verdict": pool,
        "pass": pass_count,
        "fail": fail_count,
        "corrupt": corrupt,
        "step1_fail": step1_fail,
        "no_sig": no_sig,
    }


StepFn = Callable[[Config], dict[str, Any]]

STEPS: dict[str, StepFn] = {
    "1a": step1_baseline,
    "1b": step1_error_leak,
    "1c": step1_count_tokens,
    "2a": step2_thinking_budget_zero,
    "2b": step2_identity,
    "3a": step3_knowledge,
    "3b": step3_self_sig,
}


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv)
    requested = cfg.steps or list(STEPS.keys())
    for index, step_id in enumerate(requested):
        step = STEPS.get(step_id)
        if step is None:
            print(f"Unknown step: {step_id}")
            return 2
        step(cfg)
        if index < len(requested) - 1:
            time.sleep(cfg.gap)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
