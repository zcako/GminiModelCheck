"""HTTP client + 公共工具(零依赖,基于 stdlib)"""
from __future__ import annotations
import json
import time
import urllib.request
import urllib.error
import socket
from pathlib import Path
from typing import Any
from datetime import datetime, timezone


def save_http_packet(
    out_dir: Path | None,
    url: str,
    method: str,
    req_headers: dict,
    req_body: Any,
    status_code: int,
    res_headers: dict,
    res_body: Any,
) -> None:
    if not out_dir:
        return
    packet_dir = out_dir / "http_packets"
    packet_dir.mkdir(parents=True, exist_ok=True)
    
    safe_req_headers = {}
    for k, v in req_headers.items():
        if k.lower() in ("x-goog-api-key", "authorization"):
            if len(v) > 8:
                safe_req_headers[k] = f"{v[:3]}...{v[-4:]}"
            else:
                safe_req_headers[k] = "***"
        else:
            safe_req_headers[k] = v

    packet = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request": {
            "url": url,
            "method": method,
            "headers": safe_req_headers,
            "body": req_body,
        },
        "response": {
            "status_code": status_code,
            "headers": res_headers,
            "body": res_body,
        }
    }
    
    import random
    rand_id = f"{int(time.time() * 1000) % 1000000:06d}-{random.randint(1000, 9999)}"
    packet_path = packet_dir / f"http-{rand_id}.json"
    try:
        with open(packet_path, "w", encoding="utf-8") as f:
            json.dump(packet, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Warning] Failed to save HTTP packet: {e}")


def post_generate(
    base: str,
    model: str,
    api_key: str,
    payload: dict,
    timeout: int = 120,
    out_dir: Path | None = None,
) -> tuple[int, dict, dict]:
    """POST <base>/v1beta/models/<model>:generateContent

    返回 (status_code, response_json_or_error_dict, response_headers_lower)
    """
    return _post_json(
        f"{base}/v1beta/models/{model}:generateContent",
        api_key,
        payload,
        timeout,
        out_dir=out_dir,
    )


def post_count_tokens(
    base: str,
    model: str,
    api_key: str,
    payload: dict,
    timeout: int = 60,
    out_dir: Path | None = None,
) -> tuple[int, dict, dict]:
    """POST <base>/v1beta/models/<model>:countTokens"""
    return _post_json(
        f"{base}/v1beta/models/{model}:countTokens",
        api_key,
        payload,
        timeout,
        out_dir=out_dir,
    )


def get_url(
    url: str,
    api_key: str | None = None,
    timeout: int = 30,
    out_dir: Path | None = None,
) -> tuple[int, str, dict]:
    """GET 任意 URL,返回 (status, body_text, headers)"""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["x-goog-api-key"] = api_key
    req = urllib.request.Request(url, headers=headers, method="GET")
    status, text, hdrs = _do_request(req, timeout)
    if out_dir:
        try:
            res_body = json.loads(text)
        except Exception:
            res_body = text
        save_http_packet(
            out_dir=out_dir,
            url=url,
            method="GET",
            req_headers=headers,
            req_body=None,
            status_code=status,
            res_headers=hdrs,
            res_body=res_body,
        )
    return status, text, hdrs


def _post_json(
    url: str,
    api_key: str,
    payload: dict,
    timeout: int,
    out_dir: Path | None = None,
) -> tuple[int, dict, dict]:
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    status, text, hdrs = _do_request(req, timeout)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        data = {"raw": text}
    if out_dir:
        save_http_packet(
            out_dir=out_dir,
            url=url,
            method="POST",
            req_headers=headers,
            req_body=payload,
            status_code=status,
            res_headers=hdrs,
            res_body=data,
        )
    return status, data, hdrs


def _do_request(
    req: urllib.request.Request,
    timeout: int,
) -> tuple[int, str, dict]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            hdrs = {k.lower(): v for k, v in r.getheaders()}
            return r.status, body, hdrs
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        hdrs = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
        return e.code, body, hdrs
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        # 用 -1 表示 connect/read timeout
        msg = str(e)
        return -1, json.dumps({"error": "timeout_or_network", "detail": msg}), {}


def save_raw(out_dir: Path, name: str, content: Any) -> Path:
    """保存一个原始抓包到 out_dir/name"""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    if isinstance(content, (dict, list)):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(content, f, ensure_ascii=False, indent=2)
    elif isinstance(content, bytes):
        with open(path, "wb") as f:
            f.write(content)
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(content))
    return path


def classify_field_upstream(usage: dict) -> str:
    """按 v1.5 修订规则:
    - 同时有 trafficType + serviceTier  -> 'aistudio_confirmed'
    - 仅 serviceTier                    -> 'aistudio_likely'
    - 仅 trafficType                    -> 'vertex_likely'
    - 都没有                            -> 'unknown'
    """
    if not isinstance(usage, dict):
        return "unknown"
    tt = usage.get("trafficType")
    st = usage.get("serviceTier")
    has_tt = bool(tt) and tt not in ("", None)
    has_st = bool(st) and st not in ("", None)
    if has_tt and has_st:
        return "aistudio_confirmed"
    if has_st:
        return "aistudio_likely"
    if has_tt:
        return "vertex_likely"
    return "unknown"


def safe_get(d: dict | None, *keys, default=None):
    """嵌套字典/列表安全取值。int key 表示 list 下标"""
    cur: Any = d
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k)
            if cur is None:
                return default
        elif isinstance(cur, list) and isinstance(k, int):
            if 0 <= k < len(cur):
                cur = cur[k]
            else:
                return default
        else:
            return default
    return cur


def clip(s: str, n: int = 200) -> str:
    if not s:
        return ""
    s = s.replace("\n", " ").replace("\r", " ")
    return s[:n] + ("..." if len(s) > n else "")
