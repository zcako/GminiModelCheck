#!/usr/bin/env python3
"""Capture redacted application-layer HTTP transcripts for relay auditing."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import socket
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--name", default="forensic-capture")
    parser.add_argument("--out-root", type=Path, default=Path("reports"))
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--gap", type=float, default=1.0)
    return parser.parse_args()


def json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    sensitive = {"authorization", "x-goog-api-key", "x-api-key", "api-key"}
    return {
        key: "<redacted>" if key.lower() in sensitive else value
        for key, value in headers.items()
    }


def pretty_body(body: bytes | None) -> str:
    if not body:
        return ""
    text = body.decode("utf-8", "replace")
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except (json.JSONDecodeError, ValueError):
        return text


def save_capture(
    out_dir: Path,
    capture_id: str,
    request_data: dict[str, Any],
    response_data: dict[str, Any],
) -> dict[str, Any]:
    capture = {
        "id": capture_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "request": request_data,
        "response": response_data,
    }
    json_path = out_dir / f"{capture_id}.json"
    json_path.write_text(json.dumps(capture, ensure_ascii=False, indent=2), encoding="utf-8")

    parsed = urlsplit(request_data["url"])
    request_target = parsed.path or "/"
    if parsed.query:
        request_target += f"?{parsed.query}"
    request_lines = [f"{request_data['method']} {request_target} HTTP/1.1"]
    request_lines.extend(f"{key}: {value}" for key, value in request_data["headers"].items())
    response_status = response_data["status"]
    status_text = http.client.responses.get(response_status, "") if response_status > 0 else ""
    response_lines = [f"HTTP/1.1 {response_status} {status_text}".rstrip()]
    response_lines.extend(f"{key}: {value}" for key, value in response_data["headers"].items())
    transcript = "\n".join(
        request_lines
        + ["", request_data["body"], "", "----- RESPONSE -----"]
        + response_lines
        + ["", response_data["body"]]
    )
    (out_dir / f"{capture_id}.http").write_text(transcript + "\n", encoding="utf-8")
    return capture


def capture_request(
    out_dir: Path,
    capture_id: str,
    method: str,
    url: str,
    headers: dict[str, str],
    payload: Any | None,
    timeout: int,
) -> dict[str, Any]:
    body = json_bytes(payload) if payload is not None else None
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    started = time.perf_counter()
    status = -1
    response_headers: dict[str, str] = {}
    response_body = b""
    network_error = ""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            response_headers = dict(response.getheaders())
            response_body = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        response_headers = dict(exc.headers.items()) if exc.headers else {}
        response_body = exc.read()
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        network_error = f"{type(exc).__name__}: {exc}"
        response_body = json_bytes({"error": "timeout_or_network", "detail": str(exc)})
    elapsed = round(time.perf_counter() - started, 3)

    recorded_headers = dict(headers)
    recorded_headers.setdefault("Host", urlsplit(url).netloc)
    if body is not None:
        recorded_headers.setdefault("Content-Length", str(len(body)))
    redacted = redact_headers(recorded_headers)
    request_data = {
        "method": method,
        "url": url,
        "headers": redacted,
        "body": pretty_body(body),
        "body_sha256": hashlib.sha256(body or b"").hexdigest(),
    }
    response_data = {
        "status": status,
        "elapsed_seconds": elapsed,
        "headers": response_headers,
        "body": response_body.decode("utf-8", "replace"),
        "body_sha256": hashlib.sha256(response_body).hexdigest(),
        "network_error": network_error or None,
    }
    capture = save_capture(out_dir, capture_id, request_data, response_data)
    print(f"{capture_id}: HTTP {status}, {elapsed:.3f}s", flush=True)
    return capture


def gemini_payload(text: str, generation_config: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": text}]}],
    }
    if generation_config:
        payload["generationConfig"] = generation_config
    return payload


def response_json(capture: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(capture["response"]["body"])
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def summarize(captures: list[dict[str, Any]], sample_count: int) -> dict[str, Any]:
    samples = [capture for capture in captures if capture["id"].startswith("sample-")]
    statuses: Counter[str] = Counter()
    traffic_types: Counter[str] = Counter()
    service_tiers: Counter[str] = Counter()
    model_versions: Counter[str] = Counter()
    header_values: dict[str, Counter[str]] = {
        "x-routing-group": Counter(),
        "x-gemini-service-tier": Counter(),
        "x-vertex-ai-version": Counter(),
        "x-new-api-version": Counter(),
        "x-oneapi-request-id": Counter(),
    }
    for capture in samples:
        status = str(capture["response"]["status"])
        statuses[status] += 1
        data = response_json(capture)
        usage = data.get("usageMetadata") if isinstance(data.get("usageMetadata"), dict) else {}
        traffic_types[str(usage.get("trafficType") or "<missing>")] += 1
        service_tiers[str(usage.get("serviceTier") or "<missing>")] += 1
        model_versions[str(data.get("modelVersion") or "<missing>")] += 1
        lowered = {key.lower(): value for key, value in capture["response"]["headers"].items()}
        for header, values in header_values.items():
            if header in lowered:
                values[lowered[header]] += 1
    return {
        "requested_samples": sample_count,
        "captured_samples": len(samples),
        "sample_statuses": dict(statuses),
        "trafficType": dict(traffic_types),
        "serviceTier": dict(service_tiers),
        "modelVersion": dict(model_versions),
        "response_headers": {key: dict(value) for key, value in header_values.items()},
        "all_capture_statuses": {
            capture["id"]: capture["response"]["status"] for capture in captures
        },
    }


def render_report(base: str, summary: dict[str, Any]) -> str:
    def table(counter: dict[str, int]) -> str:
        return "\n".join(f"- `{key}`: {value}" for key, value in counter.items()) or "- none"

    headers = summary["response_headers"]
    return f"""# Relay Forensic Capture

- Base: `{base}`
- Request authentication: redacted in all saved files
- Capture type: application-layer HTTP request/response transcripts, not TCP PCAP
- Requested field samples: {summary["requested_samples"]}

## Sample Statuses

{table(summary["sample_statuses"])}

## trafficType

{table(summary["trafficType"])}

## serviceTier

{table(summary["serviceTier"])}

## modelVersion

{table(summary["modelVersion"])}

## Routing And Framework Headers

- `x-routing-group`: {json.dumps(headers["x-routing-group"], ensure_ascii=False)}
- `x-gemini-service-tier`: {json.dumps(headers["x-gemini-service-tier"], ensure_ascii=False)}
- `x-vertex-ai-version`: {json.dumps(headers["x-vertex-ai-version"], ensure_ascii=False)}
- `x-new-api-version`: {json.dumps(headers["x-new-api-version"], ensure_ascii=False)}

## Captures

Each probe has:

- `<id>.json`: structured request/response record
- `<id>.http`: human-readable raw HTTP transcript

See `summary.json` for all probe status codes.
"""


def main() -> int:
    args = parse_args()
    base = args.base.rstrip("/")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_dir = args.out_root / f"{args.name}-{timestamp}"
    raw_dir = run_dir / "raw-http"
    raw_dir.mkdir(parents=True, exist_ok=True)

    common_headers = {
        "Accept": "application/json",
        "Connection": "close",
        "Content-Type": "application/json",
        "User-Agent": "gemini-relay-audit-forensic/1.0",
    }
    gemini_headers = {**common_headers, "x-goog-api-key": args.key}
    bearer_headers = {**common_headers, "Authorization": f"Bearer {args.key}"}
    captures: list[dict[str, Any]] = []

    for index in range(1, args.samples + 1):
        captures.append(capture_request(
            raw_dir,
            f"sample-{index:03d}",
            "POST",
            f"{base}/v1beta/models/gemini-3.1-pro-preview:generateContent",
            gemini_headers,
            gemini_payload("reply with: ok"),
            args.timeout,
        ))
        if index < args.samples:
            time.sleep(args.gap)

    probes = [
        (
            "thinking-zero-31-pro",
            "POST",
            f"{base}/v1beta/models/gemini-3.1-pro-preview:generateContent",
            gemini_headers,
            gemini_payload("reply with: ok", {"thinkingConfig": {"thinkingBudget": 0}}),
        ),
        (
            "thinking-zero-25-pro",
            "POST",
            f"{base}/v1beta/models/gemini-2.5-pro:generateContent",
            gemini_headers,
            gemini_payload("reply with: ok", {"thinkingConfig": {"thinkingBudget": 0}}),
        ),
        (
            "error-path",
            "POST",
            f"{base}/v1beta/models/gemini-not-real-9.9:generateContent",
            gemini_headers,
            gemini_payload("x"),
        ),
        (
            "count-tokens",
            "POST",
            f"{base}/v1beta/models/gemini-3.1-pro-preview:countTokens",
            gemini_headers,
            gemini_payload("hello world"),
        ),
        (
            "cached-contents",
            "GET",
            f"{base}/v1beta/cachedContents",
            gemini_headers,
            None,
        ),
        (
            "openai-chat-completions",
            "POST",
            f"{base}/v1/chat/completions",
            bearer_headers,
            {
                "model": "gemini-3.1-pro-preview",
                "messages": [{"role": "user", "content": "reply with: ok"}],
                "stream": False,
            },
        ),
    ]
    for capture_id, method, url, headers, payload in probes:
        time.sleep(args.gap)
        captures.append(capture_request(
            raw_dir, capture_id, method, url, headers, payload, args.timeout,
        ))

    summary = summarize(captures, args.samples)
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (run_dir / "report.md").write_text(render_report(base, summary), encoding="utf-8")
    print(f"Saved forensic capture: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
