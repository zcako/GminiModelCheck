"""判定引擎 — 把 raw 数据按 §十四 决策树打成最终结论

输出每个 key 的:
- label: 一句话身份判定
- confidence: high / medium / low
- evidence: 支撑证据列表(按 Tier 标注)
- caveats: 警告/不确定项
"""
from __future__ import annotations
from typing import Any
from urllib.parse import unquote

from . import clip  # 导入 clip 函数用于文本截断


def _oauth_keyword(s: str) -> bool:
    """判断字符串(含 URL 编码)是否含 OAuth 套壳关键词。

    中转的分组名可能是中文且被 URL 编码(如 X-Routing-Group: %E4%BC%98...),
    需先解码再匹配,否则关键词匹配失效。
    """
    if not s:
        return False
    decoded = unquote(s).lower()
    raw = s.lower()
    return any(kw in decoded or kw in raw for kw in ("cli", "oauth", "antigravity"))


# 标签库(对应 §十四 速查表的判定结果)
LABELS = {
    "vertex_confirmed": "✅ 真 Vertex AI(GCP 项目直连)",
    "vertex_likely": "⚠️ Vertex 嫌疑(需主动探针交叉)",
    "aistudio_confirmed": "⚡ AI Studio(serviceTier+trafficType 同现)",
    "aistudio_likely": "⚡ AI Studio 强嫌疑",
    "oauth_cli": "🔴 Antigravity / Gemini CLI OAuth 套壳",
    "oauth_cli_strong": "🔴 OAuth 套壳(强证据交叉)",
    "mixed": "🎲 多上游池/混合渠道(同时出现 AI Studio + OAuth 信号)",
    "model_replaced": "❌ 模型被静默替换",
    "non_google_impersonation": "❌ 非 Google 套壳冒充",
    "unknown": "❓ 信号不足,需手工排查",
}


def compute(raw: dict) -> dict:
    """主入口:对每个 key 做判定,合成总结"""
    per_key: dict[str, dict] = {}
    for kname, perdata in raw.get("per_key", {}).items():
        per_key[kname] = _judge_one(kname, perdata, raw.get("cross_sig_matrix"))

    return {
        "per_key": per_key,
        "summary": _summarize(per_key),
    }


def _judge_one(kname: str, perdata: dict, cross_matrix: dict | None) -> dict:
    """对单个 key 做判定。"""
    active = perdata.get("active", {}) or {}
    tier4 = perdata.get("tier4", {}) or {}

    evidence: list[dict] = []
    caveats: list[str] = []
    flags: dict[str, bool] = {}

    # ---------- Tier 4 优先 ----------
    knowledge = tier4.get("knowledge") or {}
    if knowledge:
        kv = knowledge.get("verdict", "")
        kp = knowledge.get("pass_count", 0)
        kf = knowledge.get("fail_count", 0)
        ka = knowledge.get("answered_n", 0)
        kt = knowledge.get("total", 3)
        if kv == "real_3x":
            evidence.append({
                "tier": 4,
                "name": "知识截止探针",
                "fact": f"{kp}/{ka} 答对 2024 年事件(共问 {kt} 题)",
                "implication": "模型本体真 3.x(权重未替换)",
            })
            flags["model_real_3x"] = True
        elif kv == "model_replaced_or_old":
            evidence.append({
                "tier": 4,
                "name": "知识截止探针",
                "fact": f"作答 {ka}/{kt} 题,{kf} 题答错且 0 题答对",
                "implication": "模型可能被静默替换或为旧版权重",
            })
            flags["model_replaced_suspect"] = True
        else:  # inconclusive：全部未作答(限流/安全过滤/空响应)
            evidence.append({
                "tier": 4,
                "name": "知识截止探针",
                "fact": f"作答 {ka}/{kt} 题(其余空响应/限流)",
                "implication": "无有效作答,无法判定模型本体(不计入对错)",
            })
            caveats.append(
                "⚠️ 知识探针全部空响应(可能限流/安全过滤),模型本体真伪未知,"
                "切勿据此判定模型被替换"
            )

    self_sig = tier4.get("self_sig") or {}
    if self_sig:
        n = self_sig.get("n", 0)
        sc = self_sig.get("sig_corrupt", 0)
        sf = self_sig.get("step1_fail", 0)
        passes = self_sig.get("pass", 0)
        if sc > 0:
            evidence.append({
                "tier": 4,
                "name": "自 sig 重复性",
                "fact": f"N={n}, FAIL(corrupt)={sc}",
                "implication": "多上游池(同一 key 后接多 GCP 项目)",
            })
            flags["multi_pool"] = True
        elif passes >= n - sf and sf <= 1:
            evidence.append({
                "tier": 4,
                "name": "自 sig 重复性",
                "fact": f"N={n}, PASS={passes}",
                "implication": "单上游池 / 真 Google 后端",
            })
            flags["sig_pass"] = True

    # ---------- Tier 1/2 主动探针 ----------
    tb0 = active.get("thinkingBudget_zero") or {}
    if tb0:
        signal = tb0.get("signal")
        capability = tb0.get("capability")
        status = tb0.get("status")
        fact = f"{status} {signal or ''}".strip()
        implication = tb0.get("note") or "thinkingBudget=0 probe recorded."
        evidence.append({
            "tier": 2,
            "name": "thinkingBudget=0",
            "fact": fact,
            "implication": implication,
        })
        if tb0.get("hard_oauth_evidence"):
            flags["thinking_unexpected_accept"] = True
        if tb0.get("oauth_suspect"):
            flags["thinking_oauth_suspect"] = True
        if tb0.get("latency_warning"):
            flags["thinking_timeout"] = True
        if signal in {"strict_reject_expected", "strict_reject"}:
            flags["strict_400"] = True
        if signal == "rewritten_to_nothinking":
            flags["thinking_rewritten"] = True
        if capability in {"supports_zero", "flash_compat"} and status == 200:
            flags["thinking_zero_allowed"] = True

    # 错误路径泄露
    err = active.get("error_path_leak") or {}
    leaks = err.get("leaks", {})
    if leaks.get("vertex_path"):
        evidence.append({
            "tier": 2,
            "name": "错误路径泄露",
            "fact": "错误信息含 projects/.../locations/.../publishers/google/...",
            "implication": "100% Vertex(GCP 路径硬证据)",
        })
        flags["vertex_path_confirmed"] = True
    if leaks.get("aistudio_doc_url"):
        evidence.append({
            "tier": 2,
            "name": "错误路径泄露",
            "fact": "错误信息含 ai.google.dev",
            "implication": "100% AI Studio",
        })
        flags["aistudio_url_confirmed"] = True
    if leaks.get("vertex_doc_masked"):
        evidence.append({
            "tier": 2,
            "name": "错误路径泄露",
            "fact": f"错误信息含 {leaks.get('masked_segments', 6)} 段 ***",
            "implication": "中转屏蔽了 cloud.google.com/vertex-ai 关键字（方案 §1.4），间接证明上游是 Vertex",
        })
        flags["vertex_doc_masked"] = True
    group_leak = leaks.get("group_name")
    if group_leak:
        evidence.append({
            "tier": 1,
            "name": "分组名泄露",
            "fact": f"分组名 = {group_leak}",
            "implication": "中转 distributor 内部分组结构暴露",
        })
        if _oauth_keyword(group_leak):
            flags["group_oauth_hint"] = True

    # HTTP 头
    hdr = active.get("http_headers") or {}
    interesting = hdr.get("interesting_headers", {})

    # Server-Timing GFE 识别（方案 §9.4）
    if "server-timing" in interesting:
        st = interesting["server-timing"]
        if "gfet4t7" in st or "gfe" in st.lower():
            evidence.append({
                "tier": 2,
                "name": "Server-Timing GFE",
                "fact": f"Server-Timing: {clip(st, 60)}",
                "implication": "真 Google Frontend (GFE) 链路，可与 OAuth 套壳区分（方案 §9.4）",
            })
            flags["google_gfe_confirmed"] = True

    if "x-routing-group" in interesting:
        rg = interesting["x-routing-group"]
        rg_decoded = unquote(rg)
        is_oauth = _oauth_keyword(rg)
        evidence.append({
            "tier": 1,
            "name": "X-Routing-Group 头",
            "fact": f"X-Routing-Group: {rg_decoded}",
            "implication": "中转主动声明分组" + (" → OAuth 套壳" if is_oauth else ""),
        })
        if is_oauth:
            flags["routing_group_oauth"] = True
    if "x-gemini-service-tier" in interesting:
        evidence.append({
            "tier": 1,
            "name": "X-Gemini-Service-Tier 头",
            "fact": f"X-Gemini-Service-Tier: {interesting['x-gemini-service-tier']}",
            "implication": "强 AI Studio 嫌疑(中转字段透传)",
        })
        flags["aistudio_header"] = True
    if "x-vertex-ai-version" in interesting:
        evidence.append({
            "tier": 2,
            "name": "X-Vertex-AI-* 头",
            "fact": f"x-vertex-ai-*: {interesting['x-vertex-ai-version']}",
            "implication": "100% Vertex(中转极少伪造此头)",
        })
        flags["vertex_header_confirmed"] = True

    # 字段采样
    fs = active.get("field_sampling") or {}
    upstream_count = fs.get("upstream_count", {})
    if fs:
        # 用有效样本数(valid_n)判定,排除限流/错误样本的稀释(方案 §7)
        valid_n = fs.get("valid_n", fs.get("n", 0)) or 1
        err_n = fs.get("error_count", 0)
        v_likely = upstream_count.get("vertex_likely", 0)
        a_conf = upstream_count.get("aistudio_confirmed", 0)
        a_likely = upstream_count.get("aistudio_likely", 0)
        unknown = upstream_count.get("unknown", 0)
        fact = f"vertex_likely={v_likely}, aistudio_likely={a_likely}, aistudio_confirmed={a_conf}, unknown={unknown}"
        if err_n:
            fact += f" (另有 {err_n} 次限流/错误,已排除)"
        evidence.append({
            "tier": 1,
            "name": f"N={valid_n} 有效字段采样",
            "fact": fact,
            "implication": _interpret_field_sampling(v_likely, a_likely, a_conf, unknown, valid_n),
        })
        if err_n >= valid_n and err_n > 0:
            caveats.append(
                f"⚠️ {err_n}/{valid_n + err_n} 次采样被限流/报错,有效样本不足,判定置信度受限"
            )
        if a_conf > 0:
            flags["aistudio_confirmed_field"] = True
        if a_likely > v_likely:
            flags["aistudio_likely_majority"] = True
        ph = fs.get("placeholder_rate", 0)
        if ph > 0:
            evidence.append({
                "tier": 1,
                "name": "modelVersion 占位符率",
                "fact": f"{int(ph*100)}% 返回 *-default",
                "implication": "中转改字符串嫌疑(需配合知识探针验证模型本体)",
            })
            flags["placeholder_seen"] = True

    # countTokens
    ct = active.get("count_tokens") or {}
    if ct.get("verdict") == "vertex_likely":
        evidence.append({
            "tier": 1,
            "name": "countTokens schema",
            "fact": "返回含 totalBillableCharacters",
            "implication": "Vertex 嫌疑",
        })
    elif ct.get("verdict") == "aistudio_likely":
        evidence.append({
            "tier": 1,
            "name": "countTokens schema",
            "fact": "无 totalBillableCharacters,仅 totalTokens",
            "implication": "AI Studio 嫌疑",
        })
    elif ct.get("blocked"):
        caveats.append(f"countTokens 端点不可用(status={ct.get('status')})— 中转可能屏蔽")

    # identity
    ident = active.get("identity") or {}
    iflags = ident.get("flags", {})
    if iflags.get("antigravity"):
        evidence.append({
            "tier": 2,
            "name": "Identity 自报",
            "fact": '响应含 "Antigravity"',
            "implication": "🚨 Antigravity OAuth 上游硬证据(自报家门)",
        })
        flags["antigravity_self_report"] = True
    if iflags.get("openai") or iflags.get("anthropic"):
        evidence.append({
            "tier": 2,
            "name": "Identity 自报",
            "fact": "自报 OpenAI/Anthropic",
            "implication": "❌ 非 Google 套壳冒充",
        })
        flags["non_google"] = True

    # 跨 key 矩阵的洞察
    if cross_matrix and not cross_matrix.get("skipped"):
        for insight in cross_matrix.get("insights", []):
            if kname in insight:
                evidence.append({
                    "tier": 4,
                    "name": "跨 key sig 矩阵",
                    "fact": insight,
                    "implication": "账号拓扑分析",
                })

    # ---------- 占位符 + 知识探针交叉判定（方案 §1.9, §13.3）----------
    if flags.get("placeholder_seen") and flags.get("model_real_3x"):
        caveats.append(
            "⚠️ modelVersion 返回占位符（*-default），但知识探针证明模型本体真实 — "
            "仅中转改字符串，不是模型替换（方案 §1.9, §13.3 修正）"
        )

    # ---------- 决策 ----------
    label_key, confidence = _decide(flags)

    # ---------- 限流降级:有效数据严重不足时,不下置信结论 ----------
    fs_check = active.get("field_sampling") or {}
    valid_n = fs_check.get("valid_n", fs_check.get("n", 0))
    error_n = fs_check.get("error_count", 0)
    knw_check = tier4.get("knowledge") or {}
    knw_inconclusive = knw_check.get("verdict") == "inconclusive"
    # 字段采样有效样本 <=1 且大量限流 + 知识探针也无结论 → 数据不足
    severe_throttle = (
        error_n >= 3
        and valid_n <= 1
        and (knw_inconclusive or not knw_check)
    )
    if severe_throttle and label_key not in (
        "oauth_cli_strong", "non_google_impersonation", "vertex_confirmed",
        "aistudio_confirmed", "model_replaced",
    ):
        confidence = "low"
        caveats.insert(0, (
            f"🔁 严重限流(有效样本仅 {valid_n},限流 {error_n} 次)+ Tier4 无结论,"
            f"当前判定基于极少数据,**建议换时段重测**后再采信"
        ))

    return {
        "label_key": label_key,
        "label": LABELS.get(label_key, "未知"),
        "confidence": confidence,
        "flags": flags,
        "evidence": evidence,
        "primary_evidence": [e["fact"] for e in evidence[-3:]],
        "caveats": caveats,
    }


def _interpret_field_sampling(v_likely: int, a_likely: int, a_conf: int, unknown: int, n: int) -> str:
    bits = []
    if a_conf > 0:
        bits.append(f"{a_conf} 次同时返回 trafficType+serviceTier(100% AI Studio)")
    if a_likely > 0:
        bits.append(f"{a_likely} 次仅 serviceTier(强 AI Studio 嫌疑)")
    if v_likely > 0:
        bits.append(f"{v_likely} 次仅 trafficType(弱 Vertex 嫌疑,AI Studio 也可能)")
    if unknown > n // 2:
        bits.append(f"{unknown} 次两字段都缺,可能字段被洗或上游异常")
    return "; ".join(bits) if bits else "无明显信号"


def _decide(flags: dict[str, bool]) -> tuple[str, str]:
    """优先级(v1.9 修正):
    1. 非 Google 套壳冒充
    2. 模型被替换(Tier 4 知识探针)
    3. OAuth + AI Studio 信号共存 → 混合上游(多池)
    4. OAuth 套壳信号(含 AI Studio 硬证据时降级)
    5. Vertex 硬证据
    6. AI Studio 硬证据 / 强嫌疑
    7. 多池
    """

    # --- 套壳冒充优先级最高 ---
    if flags.get("non_google"):
        return "non_google_impersonation", "high"

    # --- 模型被替换(Tier 4 知识探针) + 占位符交叉验证 ---
    if flags.get("model_replaced_suspect") and not flags.get("model_real_3x"):
        # 如果同时有占位符，这是静默替换的强证据（方案 §1.9）
        if flags.get("placeholder_seen"):
            return "model_replaced", "high"  # 硬证据
        return "model_replaced", "high"

    # --- 计算 OAuth 信号强度 ---
    oauth_signals = 0
    if flags.get("antigravity_self_report"):
        oauth_signals += 1  # Tier 2 hard
    if flags.get("routing_group_oauth"):
        oauth_signals += 1  # Tier 1 hard
    if flags.get("group_oauth_hint"):
        oauth_signals += 1  # Tier 1 soft
    # v1.9: only unexpected accepts on requires-thinking routes count here.
    # Flash / Flash-Lite accepting thinkingBudget=0 is official behavior.
    if flags.get("thinking_unexpected_accept"):
        oauth_signals += 1
    # 改写为 -nothinking 别名:中转层做的,AI Studio/OAuth 都可能 → 仅辅助
    if flags.get("thinking_rewritten") and oauth_signals >= 1:
        oauth_signals += 1
    if flags.get("thinking_timeout") and oauth_signals >= 1:
        oauth_signals += 1  # 高延迟 OAuth 网络特征（§9.4b）,需配合其它信号

    # --- 计算 AI Studio 信号强度 ---
    # 注意:Flash / Flash-Lite 接受 thinkingBudget=0 是官方行为,
    # 不计入 AI Studio 或 OAuth 分数。
    ai_signals = 0
    if flags.get("aistudio_confirmed_field"):
        ai_signals += 2  # Tier 1 hard
    if flags.get("aistudio_url_confirmed"):
        ai_signals += 2  # Tier 2 hard
    if flags.get("aistudio_header"):
        ai_signals += 1  # Tier 1 soft
    if flags.get("aistudio_likely_majority"):
        ai_signals += 1  # Tier 1 soft

    # --- 关键修正:AI Studio + OAuth 同时出现 = 多池 ---
    if ai_signals >= 1 and oauth_signals >= 1:
        # 同时有 AI Studio 和 OAuth 信号 → 多上游池
        return "mixed", "high"

    # --- OAuth 套壳 ---
    if oauth_signals >= 2:
        return "oauth_cli_strong", "high"
    if oauth_signals == 1:
        return "oauth_cli", "medium"

    # --- Vertex 硬证据 ---
    if flags.get("vertex_path_confirmed") or flags.get("vertex_header_confirmed"):
        return "vertex_confirmed", "high"

    # --- AI Studio 硬证据 ---
    if flags.get("aistudio_url_confirmed") or flags.get("aistudio_confirmed_field"):
        # GFE 确认提升置信度（真 Google 后端）
        if flags.get("google_gfe_confirmed"):
            return "aistudio_confirmed", "high"
        return "aistudio_confirmed", "high"

    # --- AI Studio 强嫌疑 ---
    if ai_signals >= 2:
        # 多个信号 + GFE 确认可提升置信度
        if flags.get("google_gfe_confirmed"):
            return "aistudio_likely", "high"
        return "aistudio_likely", "medium"
    if ai_signals >= 1:
        return "aistudio_likely", "medium"

    # --- 多池(sig corrupt 等) ---
    if flags.get("multi_pool"):
        return "mixed", "medium"

    return "unknown", "low"


def _summarize(per_key: dict) -> dict:
    counts: dict[str, int] = {}
    for v in per_key.values():
        lk = v.get("label_key", "unknown")
        counts[lk] = counts.get(lk, 0) + 1
    return {"label_distribution": counts}
