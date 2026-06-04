"""Tier 4 强证据探针 — 见 §十二

- 12.A 知识截止时间
- 12.B 跨 key sig 回灌矩阵
- 12.C 自 sig 重复性
"""
from __future__ import annotations
import time
from pathlib import Path

from . import post_generate, save_raw, safe_get, clip


def run_self(ctx: dict, kname: str, kval: str) -> dict:
    """跑某个 key 的 Tier 4 单 key 探针(知识 + 自 sig)"""
    out_dir: Path = ctx["raw_root"] / kname
    out_dir.mkdir(parents=True, exist_ok=True)
    base = ctx["base"]
    model = ctx["model"]          # 字段采样/知识探针用
    sig_model = ctx.get("sig_model", "gemini-3-flash-preview")  # sig 回灌专用
    quiet = ctx.get("quiet", False)

    results: dict = {}
    if not quiet:
        print(f"  [tier4] 知识截止探针 ...", flush=True)
    results["knowledge"] = probe_knowledge(base, model, kval, out_dir)
    if not quiet:
        print(f"  [tier4] 自 sig 重复性 N={ctx['n_self_sig']} (sig_model={sig_model}) ...", flush=True)
    results["self_sig"] = probe_self_sig(base, sig_model, kval, out_dir, n=ctx["n_self_sig"])
    return results


def run_cross_sig_matrix(ctx: dict) -> dict:
    """跑跨 key sig 回灌矩阵"""
    out_dir: Path = ctx["raw_root"] / "_cross"
    out_dir.mkdir(parents=True, exist_ok=True)
    keys_dict = {name: val for name, val in ctx["keys"]}
    sig_model = ctx.get("sig_model", "gemini-3-flash-preview")
    return probe_cross_sig(ctx["base"], sig_model, keys_dict, out_dir)


# 题库:每题包含问题、判定关键词(全小写)、断言"模型应该能答对"
KNOWLEDGE_QUESTIONS = [
    {
        "id": "election2024",
        "q": "In one short sentence, without using web search or tools: who won the 2024 United States presidential election?",
        "must_contain_any": ["trump"],
        "explain": "2024-11 美国大选,Gemini 3.x (cutoff Jan 2025) 应能答出 Trump",
    },
    {
        "id": "olympics2024",
        "q": "In one short sentence, without web search: which country won the most gold medals at the 2024 Paris Summer Olympics?",
        "must_contain_any": ["united states", "china", "u.s.", "us "],
        "explain": "2024-08 巴黎奥运,US 和中国并列金牌榜首",
    },
    {
        "id": "iphone16",
        "q": "In one short sentence, without web search: what model name did Apple announce as their flagship phone in September 2024?",
        "must_contain_any": ["iphone 16"],
        "explain": "2024-09 iPhone 16 Pro/Pro Max",
    },
]


def probe_knowledge(
    base: str,
    model: str,
    api_key: str,
    out_dir: Path,
) -> dict:
    """对模型问 3 个 cutoff 后事件,验证模型本体是否真 3.x(方案 §12.A)

    关键:必须区分「答错」和「未作答」:
    - 未作答(限流/安全过滤/思考饥饿导致空文本) → 不能算答错,否则误判模型被替换
    - 给足 maxOutputTokens,避免动态思考(thinkingBudget=-1)吃光配额导致空答案
    """
    results = []
    for q in KNOWLEDGE_QUESTIONS:
        payload = {
            "contents": [{"role": "user", "parts": [{"text": q["q"]}]}],
            "generationConfig": {
                # -1 = 动态思考(模型自决);给足输出预算,避免 thinking 吃光配额
                "thinkingConfig": {"thinkingBudget": -1},
                "maxOutputTokens": 2048,
            },
        }
        status, data, _ = post_generate(base, model, api_key, payload, timeout=90, out_dir=out_dir)
        text = ""
        for p in safe_get(data, "candidates", 0, "content", "parts") or []:
            if isinstance(p, dict) and "text" in p:
                text += p["text"]
        text_low = text.lower()
        answered = bool(text.strip()) and status == 200
        if not answered:
            # 未作答:限流/安全过滤/空响应,标注但不计入对错
            outcome = "no_answer"
            passed = False
        elif any(k in text_low for k in q["must_contain_any"]):
            outcome = "pass"
            passed = True
        else:
            outcome = "fail"
            passed = False
        results.append({
            "id": q["id"],
            "status": status,
            "outcome": outcome,
            "passed": passed,
            "answer": clip(text, 200),
            "modelVersion": data.get("modelVersion") if isinstance(data, dict) else None,
        })
        time.sleep(0.3)
    save_raw(out_dir, "knowledge.json", results)

    answered_n = sum(1 for r in results if r["outcome"] != "no_answer")
    pass_count = sum(1 for r in results if r["outcome"] == "pass")
    fail_count = sum(1 for r in results if r["outcome"] == "fail")

    # 判定只基于「有作答」的题目,避免限流/空响应误判
    if answered_n == 0:
        verdict = "inconclusive"  # 全部未作答,无法判定
    elif fail_count >= 1 and pass_count == 0:
        verdict = "model_replaced_or_old"  # 答了但全错
    elif pass_count >= 1:
        verdict = "real_3x"  # 至少答对一题 cutoff 后事件
    else:
        verdict = "inconclusive"

    return {
        "name": "knowledge",
        "results": results,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "answered_n": answered_n,
        "total": len(results),
        "verdict": verdict,
    }


def _get_sig_parts(base: str, model: str, key: str, prompt: str, out_dir: Path | None = None) -> tuple[int, list]:
    """第一轮拿 thoughtSignature"""
    status, data, _ = post_generate(
        base, model, key,
        {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"thinkingConfig": {"includeThoughts": True, "thinkingBudget": 1024}},
        },
        timeout=120,
        out_dir=out_dir,
    )
    if status != 200:
        return status, []
    parts = safe_get(data, "candidates", 0, "content", "parts") or []
    return status, parts


def _replay_sig(base: str, model: str, key: str, prompt: str, parts: list, follow_up: str, out_dir: Path | None = None) -> tuple[int, dict]:
    """第二轮回灌 sig"""
    return post_generate(
        base, model, key,
        {
            "contents": [
                {"role": "user", "parts": [{"text": prompt}]},
                {"role": "model", "parts": parts},
                {"role": "user", "parts": [{"text": follow_up}]},
            ],
            "generationConfig": {"thinkingConfig": {"includeThoughts": True, "thinkingBudget": 1024}},
        },
        timeout=120,
        out_dir=out_dir,
    )[:2]


def probe_self_sig(
    base: str,
    model: str,
    api_key: str,
    out_dir: Path,
    n: int = 8,
) -> dict:
    """N 次"拿 sig + 立即回灌自己",统计 PASS/FAIL,识别多池

    model 应使用保证返回 thoughtSignature 的模型(如 gemini-3-flash-preview)。
    gemini-3.1-pro-preview 在某些中转配置下不返回 sig。
    """
    PROMPT = "Think step by step: 7 times 8 = ?"
    FOLLOW = "Now multiply that by 2"

    results = []
    pass_count = 0
    fail_count = 0
    sig_corrupt = 0
    step1_fail = 0
    no_sig = 0

    for i in range(1, n + 1):
        s1, parts = _get_sig_parts(base, model, api_key, PROMPT, out_dir=out_dir)
        if s1 != 200:
            results.append({"i": i, "status": "step1_fail", "step1_status": s1})
            step1_fail += 1
            continue
        if not parts or not any(isinstance(p, dict) and "thoughtSignature" in p for p in parts):
            results.append({"i": i, "status": "no_sig", "step1_status": s1})
            no_sig += 1
            continue
        s2, body = _replay_sig(base, model, api_key, PROMPT, parts, FOLLOW, out_dir=out_dir)
        if s2 == 200:
            pass_count += 1
            results.append({"i": i, "status": "PASS"})
        else:
            err = safe_get(body, "error", "message") or ""
            if "Corrupted thought signature" in err or "corrupted" in err.lower():
                sig_corrupt += 1
            fail_count += 1
            results.append({
                "i": i,
                "status": f"FAIL({s2})",
                "error": clip(err, 100),
            })
        time.sleep(0.3)

    save_raw(out_dir, "self-sig.json", results)
    fail_rate = fail_count / max(n, 1)
    if no_sig > 0 and fail_count == 0 and step1_fail == 0:
        pool_verdict = "no_sig_returned"
    elif fail_count == 0 and step1_fail == 0:
        pool_verdict = "single_pool"
    elif sig_corrupt > 0:
        pool_verdict = "multi_pool"
    elif step1_fail > 0:
        pool_verdict = "unstable_network"
    else:
        pool_verdict = "unknown"
    return {
        "name": "self_sig",
        "n": n,
        "pass": pass_count,
        "fail": fail_count,
        "step1_fail": step1_fail,
        "no_sig": no_sig,
        "sig_corrupt": sig_corrupt,
        "fail_rate": round(fail_rate, 3),
        "verdict": pool_verdict,
    }


def probe_cross_sig(
    base: str,
    model: str,
    keys: dict[str, str],
    out_dir: Path,
) -> dict:
    """跨 key sig 回灌矩阵 — 揭示中转的多账号拓扑

    keys: {name: api_key} 至少 2 个
    """
    if len(keys) < 2:
        return {"name": "cross_sig", "skipped": True, "reason": "need >=2 keys"}

    PROMPT = "Think step by step: 7 times 8 = ?"
    FOLLOW = "Now multiply that by 2"

    # 先各自拿一轮 sig
    sigs = {}
    for name, key in keys.items():
        s, parts = _get_sig_parts(base, model, key, PROMPT, out_dir=out_dir)
        if s == 200 and parts:
            sigs[name] = parts
        else:
            sigs[name] = None
        time.sleep(0.3)

    # 矩阵:src -> dst
    matrix: dict[str, dict[str, dict]] = {}
    for src, parts in sigs.items():
        matrix[src] = {}
        if parts is None:
            for dst in keys:
                matrix[src][dst] = {"status": "src_no_sig"}
            continue
        for dst, dst_key in keys.items():
            s, body = _replay_sig(base, model, dst_key, PROMPT, parts, FOLLOW, out_dir=out_dir)
            err = safe_get(body, "error", "message") if s != 200 else ""
            matrix[src][dst] = {
                "status": "PASS" if s == 200 else f"FAIL({s})",
                "error": clip(err, 80) if err else "",
            }
            time.sleep(0.3)

    save_raw(out_dir, "cross-sig.json", matrix)

    # 简单拓扑分析
    insights = []
    for dst in keys:
        col = [matrix[src][dst]["status"] for src in keys if matrix[src][dst]["status"] != "src_no_sig"]
        fails = sum(1 for s in col if "FAIL" in s)
        if fails == len(col) - 1:
            insights.append(f"{dst} 是独立账号(其它来源 sig 都 FAIL)")
        elif fails == 0:
            insights.append(f"{dst} 接受所有来源 sig(共享代理池或被中转转发)")

    return {
        "name": "cross_sig",
        "keys": list(keys.keys()),
        "matrix": matrix,
        "insights": insights,
    }
