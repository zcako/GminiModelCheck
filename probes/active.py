"""Tier 1 主动探针 — 见 §九"""
from __future__ import annotations
import time
import traceback
from pathlib import Path

from . import (
    post_generate,
    post_count_tokens,
    get_url,
    save_raw,
    classify_field_upstream,
    safe_get,
    clip,
)


def run_all(ctx: dict, kname: str, kval: str) -> dict:
    """跑全部 Tier 1 主动探针(单 key)"""
    out_dir: Path = ctx["raw_root"] / kname
    out_dir.mkdir(parents=True, exist_ok=True)
    base = ctx["base"]
    model = ctx["model"]
    quiet = ctx.get("quiet", False)

    results: dict = {}

    def step(label, fn, *args, **kw):
        if not quiet:
            print(f"  [active] {label} ...", flush=True)
        try:
            return fn(*args, **kw, out_dir=out_dir)
        except Exception as e:
            tb = traceback.format_exc()
            # 保存到文件
            err_path = out_dir / f"ERROR-{label.replace(' ', '_').replace('=', '_')}.txt"
            with open(err_path, "w", encoding="utf-8") as f:
                f.write(f"Exception in {label}\n\n{tb}")
            if not quiet:
                print(f"    ⚠️ Error (saved to {err_path.name})", flush=True)
            return {"name": label, "error": f"{type(e).__name__}: {e}", "traceback_file": str(err_path)}

    results["thinkingBudget_zero"] = step(
        "thinkingBudget=0", probe_thinking_budget_zero, base, model, kval,
    )
    results["field_sampling"] = step(
        f"N={ctx['n_samples']} 字段采样", probe_field_sampling,
        base, model, kval, ctx["n_samples"],
    )
    results["error_path_leak"] = step(
        "错误路径泄露", probe_error_path_leak, base, kval,
    )
    results["cached_contents"] = step(
        "cachedContents", probe_cached_contents, base, kval,
    )
    results["http_headers"] = step(
        "HTTP 头", probe_http_headers, base, model, kval,
    )
    results["count_tokens"] = step(
        "countTokens", probe_count_tokens, base, model, kval,
    )
    results["identity"] = step(
        "identity 自报家门", probe_identity, base, model, kval,
    )
    return results


def probe_field_sampling(
    base: str,
    model: str,
    api_key: str,
    n: int,
    out_dir: Path,
) -> dict:
    """N=20 采样,统计 trafficType / serviceTier 出现频率,识别多池/占位符

    注意:非 200 响应(限流 429/503 等)是临时网络状态而非渠道指纹,
    单独计入 error_count,不污染 upstream 分类(方案 §7)。
    """
    payload = {"contents": [{"role": "user", "parts": [{"text": "reply with: ok"}]}]}
    samples = []
    upstream_count = {"vertex_likely": 0, "aistudio_likely": 0, "aistudio_confirmed": 0, "unknown": 0}
    mv_count: dict[str, int] = {}
    error_count = 0  # 非 200 响应数(限流等),不计入指纹统计

    for i in range(1, n + 1):
        status, data, hdrs = post_generate(base, model, api_key, payload, timeout=120)
        if status != 200:
            # 限流/错误样本单独记录,不污染 upstream 分类
            error_count += 1
            samples.append({
                "i": i,
                "status": status,
                "error": clip(safe_get(data, "error", "message") or str(data), 120),
                "upstream": "error",
            })
            time.sleep(0.5)
            continue
        usage = safe_get(data, "usageMetadata") or {}
        cls = classify_field_upstream(usage)
        upstream_count[cls] = upstream_count.get(cls, 0) + 1
        mv = data.get("modelVersion", "<missing>")
        mv_count[mv] = mv_count.get(mv, 0) + 1
        samples.append({
            "i": i,
            "status": status,
            "modelVersion": mv,
            "trafficType": usage.get("trafficType"),
            "serviceTier": usage.get("serviceTier"),
            "createTime": data.get("createTime"),
            "responseId": data.get("responseId"),
            "upstream": cls,
        })
        time.sleep(0.5)  # 保守的间隔，避免触发限流

    save_raw(out_dir, "p2-samples.json", samples)
    valid_n = n - error_count
    return {
        "name": "field_sampling",
        "n": n,
        "valid_n": valid_n,
        "error_count": error_count,
        "samples": samples,
        "upstream_count": upstream_count,
        "modelVersion_count": mv_count,
        # 占位符率按有效样本数计算,避免限流稀释
        "placeholder_rate": (
            sum(c for mv, c in mv_count.items() if mv.endswith("-default")) / valid_n
            if valid_n > 0 else 0.0
        ),
    }


def probe_thinking_budget_zero(
    base: str,
    model: str,
    api_key: str,
    out_dir: Path,
) -> dict:
    """thinkingBudget=0:Vertex 一定 400,AI Studio 通常拒绝。

    OAuth 套壳可能：
    1. 接受并返回 200（改写为 -nothinking 别名）
    2. 触发完整推理，延迟 15-60s（方案 §9.4b）
    """
    payload = {
        "contents": [{"role": "user", "parts": [{"text": "reply with: ok"}]}],
        "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}},
    }
    status, data, hdrs = post_generate(base, model, api_key, payload, timeout=90)
    save_raw(out_dir, "p1-thinkingBudget0.json", {"status": status, "headers": hdrs, "body": data})

    note = None
    if status == 400:
        verdict = "vertex_or_strict"  # Vertex 严格拒绝
        signal = "rejected_400"
    elif status == 200:
        # 接受了 -- 可能是 AI Studio 也可能是 OAuth 套壳
        mv = data.get("modelVersion", "")
        if "nothinking" in mv:
            verdict = "rewritten_to_nothinking"  # 中转把它改成了别名
            signal = "rewritten"
        else:
            verdict = "accepted_as_is"
            signal = "accepted"
    elif status == -1:
        verdict = "timeout"
        signal = "timeout"
        note = "OAuth 套壳上游可能触发完整推理导致超时（方案 §9.4b）"
    else:
        verdict = f"other_status_{status}"
        signal = f"status_{status}"

    return {
        "name": "thinkingBudget_zero",
        "status": status,
        "verdict": verdict,
        "signal": signal,
        "modelVersion": data.get("modelVersion") if isinstance(data, dict) else None,
        "error_message": clip(safe_get(data, "error", "message") or "", 200),
        "note": note,
    }


def probe_error_path_leak(
    base: str,
    api_key: str,
    out_dir: Path,
) -> dict:
    """请求一个不存在的模型,捕获错误信息里的内部分组/上游路径"""
    payload = {"contents": [{"role": "user", "parts": [{"text": "x"}]}]}
    status, data, hdrs = post_generate(
        base, "gemini-not-real-9.9", api_key, payload, timeout=30,
    )
    save_raw(out_dir, "p3-error-leak.json", {"status": status, "headers": hdrs, "body": data})

    err_msg = safe_get(data, "error", "message") or str(data)
    leaks = {}
    if "projects/" in err_msg and "/locations/" in err_msg:
        leaks["vertex_path"] = True
    if "ai.google.dev" in err_msg or "for API version v1beta" in err_msg:
        leaks["aistudio_doc_url"] = True

    # 6段 *** 识别（方案 §1.4）
    if "***" in err_msg:
        # 统计被 *** 替换的 URL 段落数（用 / 分隔）
        # 例如: https://***.com/***/***/***/***/***
        parts = err_msg.split("/")
        star_count = sum(1 for p in parts if p.strip() == "***")
        if star_count >= 6:
            leaks["vertex_doc_masked"] = True  # 中转屏蔽了 cloud.google.com/vertex-ai 关键字
            leaks["masked_segments"] = star_count

    # 中转分组泄露
    import re
    m = re.search(r"under group (\S+?) \(distributor\)", err_msg)
    if not m:
        m = re.search(r"分组 (\S+?) 下", err_msg)
    if m:
        leaks["group_name"] = m.group(1)

    return {
        "name": "error_path_leak",
        "status": status,
        "error_message": clip(err_msg, 400),
        "leaks": leaks,
    }


def probe_cached_contents(
    base: str,
    api_key: str,
    out_dir: Path,
) -> dict:
    """GET /v1beta/cachedContents — Vertex 路径行为 vs 中转屏蔽"""
    status, body, hdrs = get_url(
        f"{base}/v1beta/cachedContents", api_key, timeout=30,
    )
    save_raw(out_dir, "p4-cachedContents.txt", body)
    return {
        "name": "cachedContents",
        "status": status,
        "body_snippet": clip(body, 200),
        "blocked": status == 404,
    }


def probe_http_headers(
    base: str,
    model: str,
    api_key: str,
    out_dir: Path,
) -> dict:
    """完整抓包响应头,识别 X-Gemini-Service-Tier / X-Routing-Group / X-New-Api-Version 等"""
    payload = {"contents": [{"role": "user", "parts": [{"text": "ok"}]}]}
    status, data, hdrs = post_generate(base, model, api_key, payload, timeout=60)
    save_raw(out_dir, "p5-headers.json", {"status": status, "headers": hdrs})

    interesting = {}
    for key in [
        "x-gemini-service-tier",
        "x-routing-group",
        "x-routing-priority",
        "x-routing-source",
        "x-new-api-version",
        "x-shellapi-request-id",
        "x-oneapi-request-id",
        "x-vertex-ai-version",
        "server-timing",
        "server",
        "alt-svc",
        "via",
    ]:
        if key in hdrs:
            interesting[key] = hdrs[key]

    return {
        "name": "http_headers",
        "status": status,
        "interesting_headers": interesting,
        "framework_hint": _classify_framework(hdrs),
        "upstream_hint": _classify_upstream_from_headers(hdrs),
    }


def _classify_framework(hdrs: dict) -> str:
    v = hdrs.get("x-new-api-version", "")
    if v.startswith("v1.0.0-rc."):
        return f"NewAPI {v} (老节点)"
    if "dev" in v:
        return f"NewAPI {v} (新节点)"
    if hdrs.get("x-shellapi-request-id"):
        return "ShellAPI"
    if hdrs.get("x-oneapi-request-id"):
        return "OneAPI/NewAPI"
    return "unknown"


def _classify_upstream_from_headers(hdrs: dict) -> str:
    if hdrs.get("x-routing-group"):
        return f"routing_group={hdrs['x-routing-group']}"
    if hdrs.get("x-gemini-service-tier"):
        return "aistudio_strong (X-Gemini-Service-Tier 透传)"
    if hdrs.get("x-vertex-ai-version"):
        return "vertex_strong"
    return "no_upstream_signal"


def probe_count_tokens(
    base: str,
    model: str,
    api_key: str,
    out_dir: Path,
) -> dict:
    """countTokens schema 差异:Vertex 有 totalBillableCharacters,AI Studio 没有

    注意：部分中转会把 countTokens 转发到 generateContent (方案 §五 已知坑点)，
    导致返回完整的 candidates 数组而非简单的 token 统计。
    """
    payload = {"contents": [{"role": "user", "parts": [{"text": "hello world"}]}]}
    status, data, hdrs = post_count_tokens(base, model, api_key, payload, timeout=30)
    save_raw(out_dir, "p6-countTokens.json", {"status": status, "body": data})

    if status != 200:
        return {
            "name": "count_tokens",
            "status": status,
            "verdict": f"failed_{status}",
            "blocked": True,
        }

    if not isinstance(data, dict):
        return {
            "name": "count_tokens",
            "status": status,
            "verdict": "invalid_response",
            "note": "响应不是 JSON 对象",
        }

    # 检测端点污染：如果返回 candidates 说明被转发到 generateContent
    if "candidates" in data:
        return {
            "name": "count_tokens",
            "status": status,
            "verdict": "endpoint_polluted",
            "note": "中转把 countTokens 转发到 generateContent，探针失效（方案 §五 已知坑点）",
            "fields": list(data.keys()),
        }

    # 正常 countTokens 响应判定
    has_billable = "totalBillableCharacters" in data
    has_total = "totalTokens" in data

    if has_billable:
        verdict = "vertex_likely"
    elif has_total:
        verdict = "aistudio_likely"
    else:
        verdict = "unknown"

    return {
        "name": "count_tokens",
        "status": status,
        "verdict": verdict,
        "has_totalBillableCharacters": has_billable,
        "has_totalTokens": has_total,
        "fields": list(data.keys()),
    }


def probe_identity(
    base: str,
    model: str,
    api_key: str,
    out_dir: Path,
) -> dict:
    """身份探针:让模型自报家门(Tier 2,会被 system prompt 改写)"""
    payload = {
        "contents": [{
            "role": "user",
            "parts": [{
                "text": "In one short sentence: which AI model are you, and what is your knowledge cutoff date?"
            }]
        }],
    }
    status, data, hdrs = post_generate(base, model, api_key, payload, timeout=90)
    save_raw(out_dir, "p7-identity.json", {"status": status, "body": data})

    # 非 200(限流等)或空响应:标注原因,不当作有效身份信号
    if status != 200:
        return {
            "name": "identity",
            "status": status,
            "text": "",
            "flags": {},
            "note": f"探针失败(HTTP {status}),可能限流;身份信号不可用",
        }

    text = ""
    cands = safe_get(data, "candidates") or []
    if cands and isinstance(cands, list):
        ps = safe_get(cands[0], "content", "parts") or []
        for p in ps:
            if isinstance(p, dict) and "text" in p:
                text += p["text"]

    if not text.strip():
        return {
            "name": "identity",
            "status": status,
            "text": "",
            "flags": {},
            "note": "模型返回空响应(可能被安全过滤或限流);身份信号不可用",
        }

    flags = {}
    low = text.lower()
    if "antigravity" in low:
        flags["antigravity"] = True
    if "gemini" in low:
        flags["gemini"] = True
    if "chatgpt" in low or "openai" in low:
        flags["openai"] = True
    if "claude" in low or "anthropic" in low:
        flags["anthropic"] = True

    return {
        "name": "identity",
        "status": status,
        "text": clip(text, 300),
        "flags": flags,
    }
