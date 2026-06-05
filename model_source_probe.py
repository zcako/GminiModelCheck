#!/usr/bin/env python3
"""Capture source fingerprints for specific Gemini-compatible relay models."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from probes.thinking import classify_thinking_budget_zero


INTERESTING_HEADERS = [
    "x-routing-group",
    "x-routing-priority",
    "x-routing-source",
    "x-gemini-service-tier",
    "x-vertex-ai-version",
    "x-new-api-version",
    "x-oneapi-request-id",
    "x-shellapi-request-id",
    "server-timing",
    "alt-svc",
    "server",
    "via",
    "x-accel-buffering",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--model", action="append")
    parser.add_argument("--name", default="model-source-probe")
    parser.add_argument("--out-root", type=Path, default=Path("reports"))
    parser.add_argument("--samples", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--gap", type=float, default=1.0)
    parser.add_argument("--pre-wait", type=float, default=0.0)
    parser.add_argument("--skip-extra", action="store_true", help="Skip countTokens/thinking/global probes.")
    parser.add_argument("--list-only", action="store_true", help="Only fetch /v1/models and /v1beta/models.")
    parser.add_argument("--deep-model", action="append", default=[], help="Run identity/knowledge/OpenAI chat probes for this model.")
    return parser.parse_args()


def slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        key: "<redacted>" if key.lower() in {"x-goog-api-key", "authorization"} else value
        for key, value in headers.items()
    }


def json_body(payload: Any | None) -> bytes | None:
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def parse_json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {"raw": data}
    except (json.JSONDecodeError, ValueError):
        return {"raw": text}


def safe_get(data: Any, *keys: Any, default: Any = None) -> Any:
    cur = data
    for key in keys:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and isinstance(key, int) and 0 <= key < len(cur):
            cur = cur[key]
        else:
            return default
        if cur is None:
            return default
    return cur


def compact_body(body_text: str, max_chars: int = 20000) -> str:
    """Keep raw text, but cap very large inline image responses in .http transcripts."""
    if len(body_text) <= max_chars:
        return body_text
    return body_text[:max_chars] + f"\n... <truncated for transcript, original length={len(body_text)} chars>"


def write_http_transcript(path: Path, record: dict[str, Any]) -> None:
    request = record["request"]
    response = record["response"]
    parsed = urlsplit(request["url"])
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    lines = [f"{request['method']} {target} HTTP/1.1"]
    lines.extend(f"{key}: {value}" for key, value in request["headers"].items())
    lines.extend(["", request["body"] or "", "", "----- RESPONSE -----"])
    lines.append(f"HTTP/1.1 {response['status']}")
    lines.extend(f"{key}: {value}" for key, value in response["headers"].items())
    lines.extend(["", compact_body(response["body"])])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def capture(
    out_dir: Path,
    capture_id: str,
    method: str,
    url: str,
    headers: dict[str, str],
    payload: Any | None,
    timeout: int,
) -> dict[str, Any]:
    body = json_body(payload)
    request_headers = dict(headers)
    request_headers.setdefault("Host", urlsplit(url).netloc)
    if body is not None:
        request_headers.setdefault("Content-Length", str(len(body)))
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    started = time.perf_counter()
    status = -1
    response_headers: dict[str, str] = {}
    response_text = ""
    network_error: str | None = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.status
            response_headers = dict(response.getheaders())
            response_text = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        response_headers = dict(exc.headers.items()) if exc.headers else {}
        response_text = exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        network_error = f"{type(exc).__name__}: {exc}"
        response_text = json.dumps({"error": "timeout_or_network", "detail": str(exc)})
    elapsed = round(time.perf_counter() - started, 3)
    record = {
        "id": capture_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "request": {
            "method": method,
            "url": url,
            "headers": redact_headers(request_headers),
            "body": json.dumps(payload, ensure_ascii=False, indent=2) if payload is not None else "",
            "body_sha256": hashlib.sha256(body or b"").hexdigest(),
        },
        "response": {
            "status": status,
            "elapsed_seconds": elapsed,
            "headers": response_headers,
            "body": response_text,
            "body_sha256": hashlib.sha256(response_text.encode("utf-8", "replace")).hexdigest(),
            "network_error": network_error,
        },
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{capture_id}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    write_http_transcript(out_dir / f"{capture_id}.http", record)
    print(f"{capture_id}: HTTP {status}, {elapsed:.3f}s", flush=True)
    return record


def interesting_headers(headers: dict[str, str]) -> dict[str, str]:
    lowered = {key.lower(): value for key, value in headers.items()}
    result = {key: lowered[key] for key in INTERESTING_HEADERS if key in lowered}
    for key, value in lowered.items():
        if key.startswith("x-") and key not in result:
            result[key] = value
    return result


def summarize_body(data: dict[str, Any]) -> dict[str, Any]:
    usage = data.get("usageMetadata") if isinstance(data.get("usageMetadata"), dict) else {}
    parts = safe_get(data, "candidates", 0, "content", "parts", default=[]) or []
    part_types: list[str] = []
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, dict):
                part_types.append(type(part).__name__)
            elif "text" in part:
                part_types.append("text")
            elif "inlineData" in part:
                mime = safe_get(part, "inlineData", "mimeType", default="?")
                part_types.append(f"inlineData:{mime}")
            elif "thoughtSignature" in part:
                part_types.append("thoughtSignature")
            else:
                part_types.append(",".join(part.keys()))
    return {
        "modelVersion": data.get("modelVersion"),
        "createTime": data.get("createTime"),
        "responseId": data.get("responseId"),
        "trafficType": usage.get("trafficType"),
        "serviceTier": usage.get("serviceTier"),
        "usageKeys": sorted(usage.keys()) if isinstance(usage, dict) else [],
        "partTypes": part_types,
        "error": safe_get(data, "error", "message"),
    }


def summarize_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    status = Counter()
    traffic = Counter()
    service = Counter()
    model_versions = Counter()
    header_values: dict[str, Counter[str]] = {key: Counter() for key in INTERESTING_HEADERS}
    rows = []
    for record in records:
        data = parse_json(record["response"]["body"])
        body_summary = summarize_body(data)
        headers = interesting_headers(record["response"]["headers"])
        status[str(record["response"]["status"])] += 1
        traffic[str(body_summary["trafficType"] or "<missing>")] += 1
        service[str(body_summary["serviceTier"] or "<missing>")] += 1
        model_versions[str(body_summary["modelVersion"] or "<missing>")] += 1
        for key, value in headers.items():
            if key in header_values:
                header_values[key][value] += 1
        rows.append({
            "id": record["id"],
            "status": record["response"]["status"],
            "elapsed_seconds": record["response"]["elapsed_seconds"],
            "headers": headers,
            **body_summary,
        })
    return {
        "status": dict(status),
        "trafficType": dict(traffic),
        "serviceTier": dict(service),
        "modelVersion": dict(model_versions),
        "headers": {key: dict(counter) for key, counter in header_values.items() if counter},
        "rows": rows,
    }


def classify_count_tokens_response(status: int, data: dict[str, Any]) -> dict[str, Any]:
    if status != 200:
        return {"verdict": "unavailable", "status": status}
    if "candidates" in data or "modelVersion" in data:
        return {
            "verdict": "endpoint_polluted",
            "note": "countTokens returned generateContent-like fields.",
        }
    if "totalBillableCharacters" in data:
        return {"verdict": "vertex_likely", "note": "countTokens contains totalBillableCharacters."}
    if "totalTokens" in data:
        return {"verdict": "count_tokens_ok", "note": "countTokens schema is present."}
    return {"verdict": "unknown", "note": "Unrecognized countTokens schema."}


def classify_thinking_zero_record(model: str, record: dict[str, Any]) -> dict[str, Any]:
    response = record.get("response", {})
    data = parse_json(response.get("body", ""))
    return classify_thinking_budget_zero(
        requested_model=model,
        status=int(response.get("status", 0)),
        body=data,
        elapsed=float(response.get("elapsed_seconds") or 0.0),
    )


def render_report(meta: dict[str, Any], model_summaries: dict[str, Any], global_summary: dict[str, Any]) -> str:
    lines = [
        "# Model Source Probe",
        "",
        f"- Base: `{meta['base']}`",
        f"- Started: `{meta['started_at']}`",
        f"- Scheme: `{meta.get('scheme_version', 'v1.9')}`",
        f"- Samples per model: `{meta['samples']}`",
        "",
        "## Global Probes",
        "",
        f"- Invalid model status: `{global_summary.get('invalid_model', {}).get('status')}`",
        f"- OpenAI `/v1/models` status: `{global_summary.get('openai_models', {}).get('status')}`",
        f"- Gemini `/v1beta/models` status: `{global_summary.get('gemini_models', {}).get('status')}`",
        "",
        "## Model Summary",
        "",
        "| Model | Status | trafficType | serviceTier | modelVersion | thinkingBudget=0 | countTokens | Key Headers |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for model, summary in model_summaries.items():
        header_bits = []
        for key in ("x-new-api-version", "x-gemini-service-tier", "x-routing-group", "x-vertex-ai-version", "server-timing"):
            values = summary["headers"].get(key)
            if values:
                header_bits.append(f"`{key}`={json.dumps(values, ensure_ascii=False)}")
        extras = summary.get("extra_probes", {})
        thinking = extras.get("thinking_zero", {})
        count_tokens = extras.get("count_tokens", {})
        thinking_cell = thinking.get("signal") or "n/a"
        count_cell = count_tokens.get("verdict") or "n/a"
        lines.append(
            f"| `{model}` | {json.dumps(summary['status'], ensure_ascii=False)} "
            f"| {json.dumps(summary['trafficType'], ensure_ascii=False)} "
            f"| {json.dumps(summary['serviceTier'], ensure_ascii=False)} "
            f"| {json.dumps(summary['modelVersion'], ensure_ascii=False)} "
            f"| `{thinking_cell}` "
            f"| `{count_cell}` "
            f"| {'; '.join(header_bits) or 'none'} |"
        )
    lines.extend([
        "",
        "## Raw Files",
        "",
        "- `raw/<model>/sample-*.http`: redacted HTTP transcripts",
        "- `raw/<model>/count-tokens.http`: countTokens transcript",
        "- `raw/<model>/thinking-zero.http`: thinkingBudget=0 transcript",
        "- `raw/_global/*.http`: model list and invalid model probes",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    if not args.list_only and not args.model:
        raise SystemExit("--model is required unless --list-only is used")
    base = args.base.rstrip("/")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_dir = args.out_root / f"{args.name}-{timestamp}"
    raw_root = run_dir / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    common_headers = {
        "Accept": "application/json",
        "Connection": "close",
        "Content-Type": "application/json",
        "User-Agent": "gemini-relay-audit-source-probe/1.0",
    }
    gemini_headers = {**common_headers, "x-goog-api-key": args.key}
    bearer_headers = {**common_headers, "Authorization": f"Bearer {args.key}"}
    prompt_payload = {
        "contents": [{"role": "user", "parts": [{"text": "Reply with exact text: ok. Do not generate an image."}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 64},
    }
    identity_payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": "In one short sentence: which AI model are you, and what is your knowledge cutoff date?"}],
        }],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 512},
    }
    knowledge_payloads = {
        "knowledge-election2024": {
            "contents": [{
                "role": "user",
                "parts": [{"text": "In one short sentence, without web search: who won the 2024 United States presidential election?"}],
            }],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 1024},
        },
        "knowledge-olympics2024": {
            "contents": [{
                "role": "user",
                "parts": [{"text": "In one short sentence, without web search: which country won the most gold medals at the 2024 Paris Summer Olympics?"}],
            }],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 1024},
        },
        "knowledge-iphone16": {
            "contents": [{
                "role": "user",
                "parts": [{"text": "In one short sentence, without web search: what model name did Apple announce as their flagship phone in September 2024?"}],
            }],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 1024},
        },
    }
    model_summaries: dict[str, Any] = {}
    if args.pre_wait > 0:
        print(f"pre-wait: {args.pre_wait:.1f}s", flush=True)
        time.sleep(args.pre_wait)
    for model in args.model or []:
        model_dir = raw_root / slug(model)
        records = []
        extra_summary: dict[str, Any] = {}
        for index in range(1, args.samples + 1):
            records.append(capture(
                model_dir,
                f"sample-{index:03d}",
                "POST",
                f"{base}/v1beta/models/{model}:generateContent",
                gemini_headers,
                prompt_payload,
                args.timeout,
            ))
            if index < args.samples:
                time.sleep(args.gap)
        if not args.skip_extra:
            count_record = capture(
                model_dir,
                "count-tokens",
                "POST",
                f"{base}/v1beta/models/{model}:countTokens",
                gemini_headers,
                {"contents": [{"role": "user", "parts": [{"text": "hello world"}]}]},
                args.timeout,
            )
            records.append(count_record)
            extra_summary["count_tokens"] = {
                "status": count_record["response"]["status"],
                **classify_count_tokens_response(
                    count_record["response"]["status"],
                    parse_json(count_record["response"]["body"]),
                ),
            }
            thinking_record = capture(
                model_dir,
                "thinking-zero",
                "POST",
                f"{base}/v1beta/models/{model}:generateContent",
                gemini_headers,
                {
                    "contents": [{"role": "user", "parts": [{"text": "Reply with exact text: ok."}]}],
                    "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}, "maxOutputTokens": 64},
                },
                args.timeout,
            )
            records.append(thinking_record)
            extra_summary["thinking_zero"] = classify_thinking_zero_record(model, thinking_record)
            if model in set(args.deep_model):
                records.append(capture(
                    model_dir,
                    "identity",
                    "POST",
                    f"{base}/v1beta/models/{model}:generateContent",
                    gemini_headers,
                    identity_payload,
                    args.timeout,
                ))
                records.append(capture(
                    model_dir,
                    "openai-chat",
                    "POST",
                    f"{base}/v1/chat/completions",
                    bearer_headers,
                    {
                        "model": model,
                        "messages": [{"role": "user", "content": "reply with: ok"}],
                        "stream": False,
                    },
                    args.timeout,
                ))
                for probe_name, payload in knowledge_payloads.items():
                    records.append(capture(
                        model_dir,
                        probe_name,
                        "POST",
                        f"{base}/v1beta/models/{model}:generateContent",
                        gemini_headers,
                        payload,
                        args.timeout,
                    ))
        model_summaries[model] = summarize_records([r for r in records if r["id"].startswith("sample-")])
        if extra_summary:
            model_summaries[model]["extra_probes"] = extra_summary
        time.sleep(args.gap)

    global_summary: dict[str, Any] = {}
    if args.list_only:
        global_dir = raw_root / "_global"
        openai_models = capture(
            global_dir,
            "openai-v1-models",
            "GET",
            f"{base}/v1/models",
            bearer_headers,
            None,
            min(args.timeout, 60),
        )
        gemini_models = capture(
            global_dir,
            "gemini-v1beta-models",
            "GET",
            f"{base}/v1beta/models",
            gemini_headers,
            None,
            min(args.timeout, 60),
        )
        global_summary = {
            "openai_models": {"status": openai_models["response"]["status"]},
            "gemini_models": {"status": gemini_models["response"]["status"]},
        }
    elif not args.skip_extra:
        global_dir = raw_root / "_global"
        invalid = capture(
            global_dir,
            "invalid-model",
            "POST",
            f"{base}/v1beta/models/gemini-not-real-source-probe:generateContent",
            gemini_headers,
            {"contents": [{"role": "user", "parts": [{"text": "x"}]}]},
            min(args.timeout, 60),
        )
        openai_models = capture(
            global_dir,
            "openai-v1-models",
            "GET",
            f"{base}/v1/models",
            bearer_headers,
            None,
            min(args.timeout, 60),
        )
        gemini_models = capture(
            global_dir,
            "gemini-v1beta-models",
            "GET",
            f"{base}/v1beta/models",
            gemini_headers,
            None,
            min(args.timeout, 60),
        )
        global_summary = {
            "invalid_model": {"status": invalid["response"]["status"], **summarize_body(parse_json(invalid["response"]["body"]))},
            "openai_models": {"status": openai_models["response"]["status"]},
            "gemini_models": {"status": gemini_models["response"]["status"]},
        }
    summary = {
        "meta": {
            "base": base,
            "started_at": timestamp,
            "models": args.model,
            "samples": args.samples,
            "scheme_version": "v1.9",
            "tool_version": "model-source-probe-v1.1",
        },
        "global": global_summary,
        "models": model_summaries,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "report.md").write_text(render_report(summary["meta"], model_summaries, global_summary), encoding="utf-8")
    print(f"Saved source probe: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
