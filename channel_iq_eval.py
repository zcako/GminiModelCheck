#!/usr/bin/env python3
"""Small black-box capability eval for two Gemini-compatible relay channels."""
from __future__ import annotations

import argparse
import json
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class Case:
    case_id: str
    category: str
    prompt: str
    scorer: str
    expected: str


CASES: list[Case] = [
    Case(
        "bat_ball",
        "math_reasoning",
        "Answer with final answer only. A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. How many cents does the ball cost?",
        "number:5",
        "5 cents",
    ),
    Case(
        "lily_pads",
        "math_reasoning",
        "Answer with final answer only. A patch of lily pads doubles every day. It covers the lake on day 48. On which day did it cover half the lake?",
        "number:47",
        "47",
    ),
    Case(
        "machines",
        "math_reasoning",
        "Answer with final answer only. If 5 machines make 5 widgets in 5 minutes, how many minutes do 100 machines need to make 100 widgets?",
        "number:5",
        "5 minutes",
    ),
    Case(
        "mod_7",
        "math_reasoning",
        "Answer with final answer only. What are the last two digits of 7^222?",
        "number:49",
        "49",
    ),
    Case(
        "conditional_coin",
        "probability",
        "Answer with final answer only. A fair coin is flipped 3 times. Given that at least one flip is heads, what is the probability that all three flips are heads?",
        "fraction:1/7",
        "1/7",
    ),
    Case(
        "price_change",
        "math_reasoning",
        "Answer with final answer only. A price is increased by 25%, then discounted by 20%. What percent of the original price is the final price?",
        "number:100",
        "100%",
    ),
    Case(
        "logic_syllogism",
        "logic",
        "Answer yes or no only. All bloops are razzies. Some razzies are lazzies. Does it logically follow that some bloops are lazzies?",
        "exact:no",
        "no",
    ),
    Case(
        "oldest",
        "logic",
        "Answer with one name only. Alice is older than Bob. Bob is older than Chen. Dan is younger than Alice. Who must be the oldest among Alice, Bob, Chen, and Dan?",
        "contains:alice",
        "Alice",
    ),
    Case(
        "python_default_arg",
        "code_reasoning",
        "Answer with final printed output only. In Python, what does this print?\n\ndef f(x=[]):\n    x.append(len(x))\n    return x\nprint(f(), f())",
        "contains:[0, 1] [0, 1]",
        "[0, 1] [0, 1]",
    ),
    Case(
        "sort_word",
        "instruction_following",
        "Return only the 3rd word after sorting these words alphabetically: kiwi apple banana grape",
        "exact:grape",
        "grape",
    ),
    Case(
        "replace_cn",
        "chinese_instruction",
        "只输出结果，不要解释：把字符串“蓝红红蓝蓝红”里的“红”换成 A，“蓝”换成 B。",
        "exact:BAABBA",
        "BAABBA",
    ),
    Case(
        "json_counts",
        "structured_output",
        "Return JSON only with keys strawberry_r and mississippi_s. Count the letter r in strawberry and the letter s in Mississippi.",
        "json_counts",
        '{"strawberry_r":3,"mississippi_s":4}',
    ),
    Case(
        "weekday",
        "math_reasoning",
        "Answer with final answer only. If today is Wednesday, what day of the week is 100 days later?",
        "exact:friday",
        "Friday",
    ),
    Case(
        "compare_weight",
        "trick_question",
        "Answer with one word only: Which is heavier, 2 kg of feathers or 1 kg of steel?",
        "contains:feathers",
        "feathers",
    ),
]


def parse_channel(raw: str) -> tuple[str, str, str]:
    parts = raw.split("|", 2)
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("channel must be name|base|key")
    name, base, key = (part.strip() for part in parts)
    if not name or not base or not key:
        raise argparse.ArgumentTypeError("channel name/base/key must be non-empty")
    return name, base.rstrip("/"), key


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", action="append", type=parse_channel, required=True)
    parser.add_argument("--model", default="gemini-3.1-pro-preview")
    parser.add_argument("--name", default="channel-iq-eval")
    parser.add_argument("--out-root", type=Path, default=Path("reports"))
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--gap", type=float, default=0.8)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-wait", type=float, default=12.0)
    return parser.parse_args()


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        key: "<redacted>" if key.lower() in {"x-goog-api-key", "authorization"} else value
        for key, value in headers.items()
    }


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower()


def normalize_exact(text: str) -> str:
    return normalize(text).strip(" .。!！?？,，;；:：`'\"")


def extract_text(data: dict[str, Any]) -> str:
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    if not isinstance(parts, list):
        return ""
    return "".join(part.get("text", "") for part in parts if isinstance(part, dict))


def score_text(text: str, scorer: str) -> tuple[bool, str]:
    low = normalize(text)
    kind, _, value = scorer.partition(":")
    if kind == "exact":
        return normalize_exact(text) == normalize_exact(value), f"expected exact {value!r}"
    if kind == "contains":
        return normalize(value) in low, f"expected contains {value!r}"
    if kind == "number":
        expected = value.strip()
        numbers = re.findall(r"-?\d+(?:\.\d+)?", low.replace(",", ""))
        return expected in numbers, f"expected number {expected}, found {numbers}"
    if kind == "fraction":
        expected = value.strip().lower()
        variants = {expected, expected.replace("/", " / ")}
        return any(v in low for v in variants), f"expected fraction {expected}"
    if scorer == "json_counts":
        try:
            match = re.search(r"\{.*\}", text, flags=re.S)
            data = json.loads(match.group(0) if match else text)
            ok = data.get("strawberry_r") == 3 and data.get("mississippi_s") == 4
            return ok, f"expected strawberry_r=3 and mississippi_s=4, got {data}"
        except Exception as exc:
            return False, f"expected parseable JSON counts, parse error: {exc}"
    return False, f"unknown scorer {scorer}"


def call_generate(
    base: str,
    key: str,
    model: str,
    prompt: str,
    timeout: int,
    retries: int,
    retry_wait: float,
) -> tuple[int, dict[str, Any], dict[str, str], float, str | None]:
    url = f"{base}/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 2048,
        },
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Connection": "close",
        "User-Agent": "gemini-relay-audit-iq-eval/1.0",
        "x-goog-api-key": key,
    }
    started = time.perf_counter()
    status = -1
    response_headers: dict[str, str] = {}
    response_text = ""
    network_error: str | None = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
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
        if status != 429 or attempt >= retries:
            break
        time.sleep(retry_wait * attempt)
    elapsed = time.perf_counter() - started
    try:
        data = json.loads(response_text)
        if not isinstance(data, dict):
            data = {"raw": data}
    except (json.JSONDecodeError, ValueError):
        data = {"raw": response_text}
    return status, data, response_headers, elapsed, network_error


def save_raw(out_dir: Path, channel: str, case: Case, record: dict[str, Any]) -> None:
    case_dir = out_dir / "raw" / channel
    case_dir.mkdir(parents=True, exist_ok=True)
    path = case_dir / f"{case.case_id}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def render_report(meta: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    channels = sorted({row["channel"] for row in rows})
    lines = [
        "# Channel Capability Eval",
        "",
        f"- Model: `{meta['model']}`",
        f"- Temperature: `0`",
        f"- Started: `{meta['started_at']}`",
        "",
        "## Summary",
        "",
        "| Channel | Score | Avg Latency | HTTP Errors |",
        "|---|---:|---:|---:|",
    ]
    for channel in channels:
        subset = [row for row in rows if row["channel"] == channel]
        correct = sum(1 for row in subset if row["correct"])
        avg_latency = sum(row["elapsed_seconds"] for row in subset) / max(len(subset), 1)
        http_errors = sum(1 for row in subset if row["status"] != 200)
        lines.append(f"| {channel} | {correct}/{len(subset)} | {avg_latency:.2f}s | {http_errors} |")
    lines.extend([
        "",
        "## Per Case",
        "",
        "| Case | Category | Expected | " + " | ".join(channels) + " |",
        "|---|---|---|" + "|".join("---" for _ in channels) + "|",
    ])
    by_case = {case.case_id: case for case in CASES}
    for case in CASES:
        cells = []
        for channel in channels:
            row = next((item for item in rows if item["channel"] == channel and item["case_id"] == case.case_id), None)
            if not row:
                cells.append("missing")
                continue
            icon = "PASS" if row["correct"] else "FAIL"
            answer = row["answer"].replace("\n", " ")[:120]
            cells.append(f"{icon}: `{answer}`")
        lines.append(f"| {case.case_id} | {case.category} | `{case.expected}` | " + " | ".join(cells) + " |")
    lines.extend([
        "",
        "## Notes",
        "",
        "- This is a small deterministic black-box eval, not a full intelligence benchmark.",
        "- Raw responses are saved under `raw/<channel>/<case>.json` with authentication headers redacted.",
        "- A score tie means no measurable capability difference on this test set.",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_dir = args.out_root / f"{args.name}-{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    meta = {
        "model": args.model,
        "started_at": timestamp,
        "channels": [name for name, _, _ in args.channel],
    }

    for name, base, key in args.channel:
        print(f"========== {name} ==========", flush=True)
        for index, case in enumerate(CASES):
            status, data, headers, elapsed, network_error = call_generate(
                base, key, args.model, case.prompt, args.timeout, args.retries, args.retry_wait,
            )
            answer = extract_text(data)
            correct, reason = score_text(answer, case.scorer) if status == 200 else (False, f"HTTP {status}")
            row = {
                "channel": name,
                "case_id": case.case_id,
                "category": case.category,
                "status": status,
                "elapsed_seconds": round(elapsed, 3),
                "correct": correct,
                "expected": case.expected,
                "answer": answer,
                "score_reason": reason,
                "modelVersion": data.get("modelVersion"),
                "usageMetadata": data.get("usageMetadata"),
                "network_error": network_error,
                "response_headers": headers,
            }
            rows.append(row)
            record = {
                "request": {
                    "base": base,
                    "model": args.model,
                    "headers": redact_headers({"x-goog-api-key": key, "Content-Type": "application/json"}),
                    "prompt": case.prompt,
                    "generationConfig": {"temperature": 0, "maxOutputTokens": 2048},
                },
                "response": row,
                "raw_response": data,
            }
            save_raw(run_dir, name, case, record)
            print(
                f"{case.case_id}: {'PASS' if correct else 'FAIL'} HTTP {status} {elapsed:.2f}s",
                flush=True,
            )
            if index < len(CASES) - 1:
                time.sleep(args.gap)

    (run_dir / "results.json").write_text(
        json.dumps({"meta": meta, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "report.md").write_text(render_report(meta, rows), encoding="utf-8")
    print(f"Saved eval: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
