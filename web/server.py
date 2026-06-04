#!/usr/bin/env python3
"""Small standard-library web UI for running Gemini relay audits."""
from __future__ import annotations

import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = Path(__file__).resolve().parent / "static"
REPORT_ROOT = Path(os.environ.get("WEB_REPORT_ROOT", ROOT / "reports"))
HOST = os.environ.get("WEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("WEB_PORT", "8080"))
AUTH_TOKEN = os.environ.get("WEB_AUTH_TOKEN", "").strip()

DEFAULT_MODEL = "gemini-3.1-pro-preview"
DEFAULT_SIG_MODEL = "gemini-3-flash-preview"
DEFAULT_N_SAMPLES = 20
DEFAULT_N_SELF_SIG = 8
DEFAULT_TIMEOUT = 120

STAGE_ORDER = [
    "thinking_budget_zero",
    "field_sampling",
    "error_path_leak",
    "cached_contents",
    "http_headers",
    "count_tokens",
    "identity",
    "knowledge",
    "self_sig",
    "cross_sig",
]

STAGE_LABELS = {
    "thinking_budget_zero": "thinkingBudget=0",
    "field_sampling": "字段采样",
    "error_path_leak": "错误路径泄露",
    "cached_contents": "cachedContents",
    "http_headers": "HTTP 头",
    "count_tokens": "countTokens",
    "identity": "identity 自报家门",
    "knowledge": "知识截止探针",
    "self_sig": "自 sig 重复性",
    "cross_sig": "跨 key sig 矩阵",
}

RUNS: dict[str, "RunState"] = {}
RUNS_LOCK = threading.Lock()


@dataclass
class RunState:
    id: str
    request: dict[str, Any]
    status: str = "queued"
    created_at: str = field(default_factory=lambda: utc_now())
    started_at: str | None = None
    finished_at: str | None = None
    returncode: int | None = None
    report_dir: str | None = None
    error: str | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    condition: threading.Condition = field(default_factory=threading.Condition)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "returncode": self.returncode,
            "report_dir": self.report_dir,
            "error": self.error,
            "request": safe_request(self.request),
            "artifacts": {
                name: f"/api/runs/{self.id}/artifact/{name}"
                for name in self.artifacts
            },
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_run_request(payload: dict[str, Any]) -> dict[str, Any]:
    base = normalize_base_url(payload.get("base", ""))

    keys = parse_key_lines(payload.get("keys", ""))
    if not keys:
        raise ValueError("at least one API key is required")

    name = sanitize_name(str(payload.get("name", "")).strip() or "audit")
    model = str(payload.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    sig_model = str(payload.get("sig_model") or DEFAULT_SIG_MODEL).strip() or DEFAULT_SIG_MODEL

    return {
        "tool": "audit",
        "base": base,
        "keys": keys,
        "name": name,
        "model": model,
        "sig_model": sig_model,
        "n_samples": bounded_int(payload.get("n_samples"), DEFAULT_N_SAMPLES, 1, 100),
        "n_self_sig": bounded_int(payload.get("n_self_sig"), DEFAULT_N_SELF_SIG, 1, 50),
        "timeout": bounded_int(payload.get("timeout"), DEFAULT_TIMEOUT, 10, 600),
        "skip_active": bool(payload.get("skip_active", False)),
        "skip_tier4": bool(payload.get("skip_tier4", False)),
        "skip_cross_sig": bool(payload.get("skip_cross_sig", False)),
    }


def normalize_base_url(value: Any) -> str:
    base = str(value).strip().rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base must be an http or https URL")
    return base


def normalize_single_key(value: Any) -> str:
    key = str(value or "").strip()
    if not key:
        raise ValueError("API key is required")
    return key


def normalize_steps(value: Any) -> list[str]:
    allowed = {"1a", "1b", "1c", "2a", "2b", "3a", "3b"}
    if isinstance(value, list):
        raw_steps = [str(item).strip() for item in value]
    else:
        raw_steps = str(value or "").replace(",", " ").split()
    steps = [step for step in raw_steps if step]
    invalid = [step for step in steps if step not in allowed]
    if invalid:
        raise ValueError(f"unknown manual probe step(s): {', '.join(invalid)}")
    return steps


def normalize_model_lines(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_models = [str(item).strip() for item in value]
    else:
        raw_models = [line.strip() for line in str(value or "").splitlines()]
    models: list[str] = []
    seen: set[str] = set()
    for model in raw_models:
        if not model or model.startswith("#") or model in seen:
            continue
        seen.add(model)
        models.append(model)
    return models


def normalize_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def normalize_manual_probe_request(payload: dict[str, Any]) -> dict[str, Any]:
    key = normalize_single_key(payload.get("key"))
    return {
        "tool": "manual_probe",
        "base": normalize_base_url(payload.get("base", "")),
        "key": key,
        "keys": [("key", key)],
        "model": str(payload.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        "sig_model": str(payload.get("sig_model") or DEFAULT_SIG_MODEL).strip() or DEFAULT_SIG_MODEL,
        "steps": normalize_steps(payload.get("steps")),
        "gap": normalize_float(payload.get("gap"), 3.0, 0.0, 60.0),
        "timeout": bounded_int(payload.get("timeout"), DEFAULT_TIMEOUT, 10, 600),
        "retries": bounded_int(payload.get("retries"), 3, 1, 10),
        "self_sig_n": bounded_int(payload.get("self_sig_n"), 4, 1, 50),
    }


def normalize_model_enum_request(payload: dict[str, Any]) -> dict[str, Any]:
    key = normalize_single_key(payload.get("key"))
    return {
        "tool": "model_enum",
        "base": normalize_base_url(payload.get("base", "")),
        "key": key,
        "keys": [("key", key)],
        "models": normalize_model_lines(payload.get("models")),
        "gap": normalize_float(payload.get("gap"), 3.0, 0.0, 60.0),
        "timeout": bounded_int(payload.get("timeout"), 90, 10, 600),
        "retries": bounded_int(payload.get("retries"), 3, 1, 10),
    }


def parse_key_lines(value: Any) -> list[tuple[str, str]]:
    if isinstance(value, list):
        raw_items = [str(item.get("name", "")) + "=" + str(item.get("value", "")) for item in value]
    else:
        raw_items = [line.strip() for line in str(value).splitlines()]

    keys: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate((line for line in raw_items if line.strip()), start=1):
        if "=" in raw:
            name, _, key_value = raw.partition("=")
            key_name = sanitize_key_name(name.strip() or f"key{index}")
            key_value = key_value.strip()
        else:
            key_name = f"key{index}"
            key_value = raw.strip()
        if not key_value:
            raise ValueError(f"key {index} is empty")
        base_name = key_name
        suffix = 2
        while key_name in seen:
            key_name = f"{base_name}-{suffix}"
            suffix += 1
        seen.add(key_name)
        keys.append((key_name, key_value))
    return keys


def sanitize_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return cleaned[:80] or "audit"


def sanitize_key_name(value: str) -> str:
    return sanitize_name(value).replace(".", "-")


def bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def build_audit_command(request: dict[str, Any], out_root: Path) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "audit.py"),
        "--base",
        request["base"],
        "--name",
        request["name"],
        "--out-root",
        str(out_root),
        "--model",
        request["model"],
        "--sig-model",
        request["sig_model"],
        "--n-samples",
        str(request["n_samples"]),
        "--n-self-sig",
        str(request["n_self_sig"]),
        "--timeout",
        str(request["timeout"]),
    ]
    for key_name, key_value in request["keys"]:
        command.extend(["--key", f"{key_name}={key_value}"])
    if request.get("skip_active"):
        command.append("--skip-active")
    if request.get("skip_tier4"):
        command.append("--skip-tier4")
    if request.get("skip_cross_sig"):
        command.append("--skip-cross-sig")
    return command


def build_manual_probe_command(request: dict[str, Any]) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "manual_probe.py"),
        "--base",
        request["base"],
        "--key",
        request["key"],
        "--model",
        request["model"],
        "--sig-model",
        request["sig_model"],
        "--timeout",
        str(request["timeout"]),
        "--retries",
        str(request["retries"]),
        "--gap",
        str(request["gap"]),
        "--self-sig-n",
        str(request["self_sig_n"]),
    ]
    command.extend(request.get("steps") or [])
    return command


def build_model_enum_command(request: dict[str, Any], out_root: Path, run_id: str) -> list[str]:
    out_path = out_root / f"model-enum-{run_id}.json"
    command = [
        sys.executable,
        str(ROOT / "model_enum.py"),
        "--base",
        request["base"],
        "--key",
        request["key"],
        "--out",
        str(out_path),
        "--gap",
        str(request["gap"]),
        "--timeout",
        str(request["timeout"]),
        "--retries",
        str(request["retries"]),
    ]
    for model in request.get("models") or []:
        command.extend(["--model", model])
    return command


def build_run_command(run: RunState, out_root: Path) -> list[str]:
    tool = run.request.get("tool", "audit")
    if tool == "manual_probe":
        return build_manual_probe_command(run.request)
    if tool == "model_enum":
        out_path = out_root / f"model-enum-{run.id}.json"
        run.artifacts["model-enum.json"] = str(out_path)
        return build_model_enum_command(run.request, out_root, run.id)
    return build_audit_command(run.request, out_root)


def mask_secrets(line: str, keys: list[tuple[str, str]]) -> str:
    masked = line
    for _, secret in keys:
        if secret:
            masked = masked.replace(secret, redact_secret(secret))
    return masked


def redact_secret(secret: str) -> str:
    if len(secret) <= 8:
        return "***"
    return f"{secret[:3]}...{secret[-4:]}"


def classify_cli_line(line: str) -> dict[str, Any]:
    text = line.strip()
    event: dict[str, Any] = {"kind": "log"}
    if text.startswith("==========") and text.endswith("=========="):
        label = text.strip("= ").strip()
        if "cross-key" in label:
            return stage_event("cross_sig")
        return {"kind": "key", "key": label}
    if "[active]" in text:
        return stage_event(stage_from_active_label(text))
    if "[tier4]" in text:
        if "知识截止" in text:
            return stage_event("knowledge")
        if "自 sig" in text:
            return stage_event("self_sig")
    if "cross-key sig matrix" in text:
        return stage_event("cross_sig")
    if text.startswith("[OK]") and "->" in text:
        event["kind"] = "artifact"
    elif "[ERROR]" in text or "Error" in text or "Exception" in text:
        event["kind"] = "error"
    return event


def stage_from_active_label(text: str) -> str:
    if "thinkingBudget=0" in text:
        return "thinking_budget_zero"
    if "字段采样" in text:
        return "field_sampling"
    if "错误路径泄露" in text:
        return "error_path_leak"
    if "cachedContents" in text:
        return "cached_contents"
    if "HTTP 头" in text:
        return "http_headers"
    if "countTokens" in text:
        return "count_tokens"
    if "identity" in text:
        return "identity"
    return "unknown"


def stage_event(stage: str) -> dict[str, Any]:
    return {"kind": "stage", "stage": stage, "label": STAGE_LABELS.get(stage, stage)}


def safe_request(request: dict[str, Any]) -> dict[str, Any]:
    safe = dict(request)
    safe["keys"] = [(name, redact_secret(value)) for name, value in request.get("keys", [])]
    return safe


def append_event(run: RunState, event: dict[str, Any]) -> None:
    event = dict(event)
    event.setdefault("ts", utc_now())
    with run.condition:
        run.events.append(event)
        run.condition.notify_all()


def create_run(payload: dict[str, Any], tool: str = "audit") -> RunState:
    if tool == "manual_probe":
        request = normalize_manual_probe_request(payload)
    elif tool == "model_enum":
        request = normalize_model_enum_request(payload)
    else:
        request = normalize_run_request(payload)
    run = RunState(id=uuid.uuid4().hex[:12], request=request)
    with RUNS_LOCK:
        RUNS[run.id] = run
    thread = threading.Thread(target=execute_run, args=(run,), name=f"audit-run-{run.id}", daemon=True)
    thread.start()
    return run


def execute_run(run: RunState) -> None:
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    command = build_run_command(run, REPORT_ROOT)
    run.status = "running"
    run.started_at = utc_now()
    append_event(run, {
        "kind": "status",
        "status": "running",
        "line": "Audit started.",
        "request": safe_request(run.request),
    })
    append_event(run, {
        "kind": "command",
        "line": mask_command(command, run.request["keys"]),
    })

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = mask_secrets(raw_line.rstrip("\r\n"), run.request["keys"])
            event = classify_cli_line(line)
            event["line"] = line
            maybe_capture_report_dir(run, line)
            append_event(run, event)
        run.returncode = process.wait()
        if run.report_dir is None:
            run.report_dir = locate_latest_report_dir(run.request["name"])
        run.status = "completed" if run.returncode == 0 else "failed"
    except Exception as exc:  # pragma: no cover - exercised by manual runtime behavior
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"
        append_event(run, {"kind": "error", "line": run.error})
    finally:
        run.finished_at = utc_now()
        append_event(run, {
            "kind": "status",
            "status": run.status,
            "returncode": run.returncode,
            "report_dir": run.report_dir,
            "line": f"Audit {run.status}.",
        })


def mask_command(command: list[str], keys: list[tuple[str, str]]) -> str:
    return " ".join(quote_arg(mask_secrets(part, keys)) for part in command)


def quote_arg(value: str) -> str:
    if re.search(r"\s", value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def maybe_capture_report_dir(run: RunState, line: str) -> None:
    if "->" not in line or ("verdict.json" not in line and "report.md" not in line):
        return
    target = line.split("->", 1)[1].strip()
    path = Path(target)
    if not path.is_absolute():
        path = ROOT / path
    run.report_dir = str(path.parent)


def locate_latest_report_dir(name: str) -> str | None:
    if not REPORT_ROOT.exists():
        return None
    candidates = [p for p in REPORT_ROOT.glob(f"{name}-*") if p.is_dir()]
    if not candidates:
        return None
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    return str(latest)


def load_report(run: RunState) -> dict[str, Any]:
    report_dir = Path(run.report_dir) if run.report_dir else None
    if report_dir is None or not report_dir.exists():
        located = locate_latest_report_dir(run.request["name"])
        if located:
            report_dir = Path(located)
            run.report_dir = located
    if report_dir is None:
        raise FileNotFoundError("report directory is not available yet")

    verdict_path = report_dir / "verdict.json"
    markdown_path = report_dir / "report.md"
    raw = json.loads(verdict_path.read_text(encoding="utf-8")) if verdict_path.exists() else None
    markdown = markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else ""
    return {
        "run": run.public(),
        "report_dir": str(report_dir),
        "raw": raw,
        "markdown": markdown,
        "artifacts": {
            "verdict_json": f"/api/runs/{run.id}/artifact/verdict.json",
            "report_md": f"/api/runs/{run.id}/artifact/report.md",
        },
    }


class AuditWebHandler(BaseHTTPRequestHandler):
    server_version = "GeminiRelayAuditWeb/1.0"

    def do_GET(self) -> None:
        try:
            self.route_get()
        except Exception as exc:  # pragma: no cover - defensive request handling
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        try:
            self.route_post()
        except Exception as exc:  # pragma: no cover - defensive request handling
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def route_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self.send_file(STATIC_ROOT / "index.html")
            return
        if path.startswith("/static/"):
            target = (STATIC_ROOT / path.removeprefix("/static/")).resolve()
            if not str(target).startswith(str(STATIC_ROOT.resolve())):
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            self.send_file(target)
            return
        if path == "/api/runs":
            if not self.authorized():
                return
            with RUNS_LOCK:
                runs = [run.public() for run in RUNS.values()]
            self.send_json({"runs": runs})
            return

        parts = [part for part in path.split("/") if part]
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "runs":
            if not self.authorized():
                return
            run = get_run_or_404(self, parts[2])
            if run is None:
                return
            if len(parts) == 3:
                self.send_json(run.public())
                return
            if len(parts) == 4 and parts[3] == "events":
                self.send_events(run)
                return
            if len(parts) == 4 and parts[3] == "report":
                try:
                    self.send_json(load_report(run))
                except FileNotFoundError as exc:
                    self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            if len(parts) == 5 and parts[3] == "artifact":
                self.send_artifact(run, parts[4])
                return
        self.send_error(HTTPStatus.NOT_FOUND)

    def route_post(self) -> None:
        if not self.authorized():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/tools/manual-probe":
            self.create_tool_run("manual_probe")
            return
        if parsed.path == "/api/tools/model-enum":
            self.create_tool_run("model_enum")
            return
        if parsed.path != "/api/runs":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        payload = self.read_json()
        try:
            run = create_run(payload)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json(run.public(), HTTPStatus.CREATED)

    def create_tool_run(self, tool: str) -> None:
        payload = self.read_json()
        try:
            run = create_run(payload, tool=tool)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json(run.public(), HTTPStatus.CREATED)

    def read_json(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0") or "0")
        if size <= 0:
            return {}
        body = self.rfile.read(size).decode("utf-8")
        return json.loads(body)

    def authorized(self) -> bool:
        if not AUTH_TOKEN:
            return True
        header = self.headers.get("Authorization", "")
        token = self.headers.get("X-Web-Token", "")
        query_token = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        if header == f"Bearer {AUTH_TOKEN}" or token == AUTH_TOKEN or query_token == AUTH_TOKEN:
            return True
        self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        return False

    def send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        content_type, _ = mimetypes.guess_type(str(path))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_events(self, run: RunState) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        index = 0
        while True:
            with run.condition:
                while index >= len(run.events) and run.status not in {"completed", "failed"}:
                    run.condition.wait(timeout=15)
                    if index >= len(run.events):
                        self.write_sse({"kind": "heartbeat", "ts": utc_now()})
                while index < len(run.events):
                    event = run.events[index]
                    index += 1
                    self.write_sse(event)
            if run.status in {"completed", "failed"} and index >= len(run.events):
                self.write_sse({"kind": "done", "status": run.status, "ts": utc_now()})
                break

    def write_sse(self, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    def send_artifact(self, run: RunState, artifact_name: str) -> None:
        if artifact_name in run.artifacts:
            self.send_file(Path(run.artifacts[artifact_name]))
            return
        if artifact_name not in {"verdict.json", "report.md"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not run.report_dir:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        path = Path(run.report_dir) / artifact_name
        self.send_file(path)

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("WEB_QUIET") == "1":
            return
        super().log_message(fmt, *args)


def get_run_or_404(handler: AuditWebHandler, run_id: str) -> RunState | None:
    with RUNS_LOCK:
        run = RUNS.get(run_id)
    if run is None:
        handler.send_error(HTTPStatus.NOT_FOUND)
        return None
    return run


def main() -> int:
    args = parse_cli_args()
    address = (args["host"], args["port"])
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer(address, AuditWebHandler)
    print(f"Gemini relay audit web UI listening on http://{address[0]}:{address[1]}")
    print(f"Reports root: {REPORT_ROOT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        httpd.server_close()
    return 0


def parse_cli_args() -> dict[str, Any]:
    host = HOST
    port = PORT
    argv = iter(sys.argv[1:])
    for arg in argv:
        if arg == "--host":
            host = next(argv)
        elif arg == "--port":
            port = int(next(argv))
        elif arg in {"-h", "--help"}:
            print("Usage: python -m web.server [--host 0.0.0.0] [--port 8080]")
            raise SystemExit(0)
    return {"host": host, "port": port}


if __name__ == "__main__":
    raise SystemExit(main())
