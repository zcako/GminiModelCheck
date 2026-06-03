"""HTTP client + 公共工具(零依赖,基于 stdlib)"""
from __future__ import annotations
import json
import time
import urllib.request
import urllib.error
import socket
from pathlib import Path
from typing import Any


def post_generate(
    base: str,
    model: str,
    api_key: str,
    payload: dict,
    timeout: int = 120,
) -> tuple[int, dict, dict]:
    """POST <base>/v1beta/models/<model>:generateContent

    返回 (status_code, response_json_or_error_dict, response_headers_lower)
    """
    return _post_json(
        f"{base}/v1beta/models/{model}:generateContent",
        api_key,
        payload,
        timeout,
    )


def post_count_tokens(
    base: str,
    model: str,
    api_key: str,
    payload: dict,
    timeout: int = 60,
) -> tuple[int, dict, dict]:
    """POST <base>/v1beta/models/<model>:countTokens"""
    return _post_json(
        f"{base}/v1beta/models/{model}:countTokens",
        api_key,
        payload,
        timeout,
    )


def get_url(
    url: str,
    api_key: str | None = None,
    timeout: int = 30,
) -> tuple[int, str, dict]:
    """GET 任意 URL,返回 (status, body_text, headers)"""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["x-goog-api-key"] = api_key
    req = urllib.request.Request(url, headers=headers, method="GET")
    return _do_request(req, timeout)


def _post_json(
    url: str,
    api_key: str,
    payload: dict,
    timeout: int,
) -> tuple[int, dict, dict]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    status, text, hdrs = _do_request(req, timeout)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        data = {"raw": text}
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
