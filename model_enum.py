#!/usr/bin/env python3
"""Enumerate real model availability and field fingerprints for a Gemini relay."""
from __future__ import annotations

import argparse
import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_MODELS = [
    "gemini-3.1-pro-preview-thinking",
    "gemini-2.5-flash-nothinking",
    "gemini-3-pro-preview",
    "gemini-2.5-flash-lite",
    "gemini-3-pro-preview-thinking",
    "gemini-2.5-flash",
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
    "gemini-2.5-pro",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.1-flash-lite",
]


@dataclass
class Config:
    base: str
    key: str
    models: list[str]
    out: Path
    gap: float
    timeout: int
    retries: int


def parse_args(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(description="Enumerate real model availability through generateContent.")
    parser.add_argument("--base", required=True, help="Relay base URL, e.g. https://relay.example.com")
    parser.add_argument("--key", required=True, help="API key for this relay. Do not commit it.")
    parser.add_argument("--model", action="append", dest="models", help="Model to probe. Repeat for multiple models.")
    parser.add_argument("--models-file", type=Path, help="Text file with one model name per line. # comments allowed.")
    parser.add_argument("--out", type=Path, default=Path("reports/model-enum.json"), help="JSON output path")
    parser.add_argument("--gap", type=float, default=3.0, help="Seconds to sleep between model calls")
    parser.add_argument("--timeout", type=int, default=90, help="Per-request timeout seconds")
    parser.add_argument("--retries", type=int, default=3, help="Retry count for HTTP 429")
    args = parser.parse_args(argv)
    models = resolve_models(args.models or [], args.models_file)
    return Config(
        base=normalize_base(args.base),
        key=args.key.strip(),
        models=models,
        out=args.out,
        gap=max(0.0, args.gap),
        timeout=max(1, args.timeout),
        retries=max(1, args.retries),
    )


def normalize_base(base: str) -> str:
    return base.strip().rstrip("/")


def resolve_models(cli_models: list[str], models_file: Path | None) -> list[str]:
    models: list[str] = []
    models.extend(model.strip() for model in cli_models if model.strip())
    if models_file:
        models.extend(load_models_file(models_file))
    if not models:
        models.extend(DEFAULT_MODELS)
    return dedupe(models)


def load_models_file(path: Path) -> list[str]:
    models = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        models.append(value)
    return models


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def generate_payload() -> dict[str, Any]:
    return {
        "contents": [{"role": "user", "parts": [{"text": "reply with: ok"}]}],
        "generationConfig": {"maxOutputTokens": 16},
    }


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


def call_model(cfg: Config, model: str) -> tuple[int, dict[str, Any]]:
    url = f"{cfg.base}/v1beta/models/{model}:generateContent"
    data = json.dumps(generate_payload()).encode("utf-8")
    for attempt in range(cfg.retries):
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"x-goog-api-key": cfg.key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=cfg.timeout) as response:
                return response.status, parse_body(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace") if exc else ""
            if exc.code == 429 and attempt < cfg.retries - 1:
                time.sleep(8 * (attempt + 1))
                continue
            return exc.code, parse_body(body)
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            return -1, {"error": {"message": str(exc)}}
    return -2, {"error": {"message": "retry_exhausted"}}


def parse_body(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {"raw": data}
    except (json.JSONDecodeError, ValueError):
        return {"raw": text}


def build_row(model: str, status: int, data: dict[str, Any]) -> dict[str, Any]:
    model_version = data.get("modelVersion")
    usage = safe_get(data, "usageMetadata", default={}) or {}
    error_message = safe_get(data, "error", "message", default="")
    row = {
        "model": model,
        "status": status,
        "ok": status == 200,
        "modelVersion": model_version,
        "trafficType": usage.get("trafficType"),
        "serviceTier": usage.get("serviceTier"),
        "alias": bool(model_version and model_version != model),
        "nothinking": "nothinking" in str(model_version or "").lower(),
        "error": clip(error_message or data.get("raw") or "", 160),
    }
    return row


def clip(value: Any, limit: int = 160) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    return text[:limit] + ("..." if len(text) > limit else "")


def format_row(row: dict[str, Any]) -> str:
    if row["ok"]:
        markers = []
        if row["alias"]:
            markers.append(f"alias->{row['modelVersion']}")
        if row["nothinking"]:
            markers.append("nothinking")
        suffix = f" [{' '.join(markers)}]" if markers else ""
        return (
            f"OK   {row['model']:38s} "
            f"mv={row['modelVersion']} tt={row['trafficType']} sv={row['serviceTier']}{suffix}"
        )
    return f"FAIL {row['model']:38s} HTTP {row['status']}: {row['error']}"


def run(cfg: Config) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, model in enumerate(cfg.models):
        status, data = call_model(cfg, model)
        row = build_row(model, status, data)
        rows.append(row)
        print(format_row(row), flush=True)
        if index < len(cfg.models) - 1:
            time.sleep(cfg.gap)
    return rows


def write_rows(rows: list[dict[str, Any]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv)
    rows = run(cfg)
    write_rows(rows, cfg.out)
    ok_count = sum(1 for row in rows if row["ok"])
    print(f"\navailable {ok_count}/{len(rows)}")
    print(f"-> {cfg.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
