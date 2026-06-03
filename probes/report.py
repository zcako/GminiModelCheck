"""Markdown 报告生成器 — 把 raw 数据 + verdict 渲染成可读报告"""
from __future__ import annotations
from typing import Any
from urllib.parse import unquote


def render(raw: dict) -> str:
    meta = raw.get("meta", {})
    lines: list[str] = []
    lines.append(f"# Gemini 中转审计报告 — {meta.get('name', '?')}")
    lines.append("")
    lines.append(f"> 基于 [Gemini 中转站渠道识别方案 {meta.get('scheme_version', 'v1.8')}](../../Gemini中转站渠道识别方案.md)")
    lines.append(f"> 工具:gemini-relay-audit {meta.get('tool_version', '?')}")
    lines.append(f"> 时间:{meta.get('started_at', '?')}")
    lines.append("")

    # 元信息
    lines.append("## 一、概览")
    lines.append("")
    lines.append(f"- base: `{meta.get('base', '?')}`")
    lines.append(f"- model: `{meta.get('model', '?')}`")
    lines.append(f"- N 字段采样: {meta.get('n_samples', '?')}")
    lines.append(f"- N 自 sig 重复性: {meta.get('n_self_sig', '?')}")
    lines.append(f"- keys: {', '.join(meta.get('keys', []))}")
    lines.append("")

    # 一句话结论
    lines.append("## 二、最终判定")
    lines.append("")
    lines.append("| key | 标签 | 置信度 | 主要证据 |")
    lines.append("|---|---|---|---|")
    verdict = raw.get("verdict", {})
    for kname, v in (verdict.get("per_key") or {}).items():
        if not isinstance(v, dict):
            lines.append(f"| {kname} | (err: not dict) | | |")
            continue
        label = v.get("label", "?")
        conf = v.get("confidence", "?")
        evs = v.get("primary_evidence", [])
        lines.append(f"| {kname} | {label} | {conf} | {'; '.join(str(e) for e in evs[:3])} |")
    lines.append("")

    # 跨 key 拓扑(如果有)
    cross = raw.get("cross_sig_matrix")
    if cross and not cross.get("skipped"):
        lines.append("### 跨 key sig 矩阵(揭示账号拓扑)")
        lines.append("")
        keys = cross.get("keys", [])
        # 表头
        header = "| src \\ dst |" + "|".join(f" {k} " for k in keys) + "|"
        sep = "|---|" + "|".join("---" for _ in keys) + "|"
        lines.append(header)
        lines.append(sep)
        matrix = cross.get("matrix", {})
        for src in keys:
            row = [f"| **{src}** |"]
            for dst in keys:
                cell = matrix.get(src, {}).get(dst, {})
                status = cell.get("status", "?")
                emoji = "✅" if status == "PASS" else ("❌" if "FAIL" in status else "⚠️")
                row.append(f" {emoji} {status} |")
            lines.append("".join(row))
        lines.append("")
        if cross.get("insights"):
            lines.append("**拓扑判定**:")
            for ins in cross["insights"]:
                lines.append(f"- {ins}")
            lines.append("")

    # 每个 key 的详细数据
    lines.append("## 三、各 key 详细数据")
    lines.append("")
    for kname in meta.get("keys", []):
        per = raw.get("per_key", {}).get(kname, {})
        v = (verdict.get("per_key") or {}).get(kname, {})
        lines.append(f"### {kname}")
        lines.append("")

        # 字段采样
        active_data = per.get("active", {})
        fs = active_data.get("field_sampling", {})
        if fs:
            lines.append("#### A. N 次字段采样")
            lines.append("")
            uc = fs.get("upstream_count", {})
            n = fs.get("n", 0)
            err_n = fs.get("error_count", 0)
            valid_n = fs.get("valid_n", n)
            lines.append(f"- N = {n}（有效 {valid_n}，限流/错误 {err_n}）")
            denom = valid_n if valid_n else 1
            for k in ["aistudio_confirmed", "aistudio_likely", "vertex_likely", "unknown"]:
                if uc.get(k, 0) > 0:
                    pct = (uc[k] / denom * 100)
                    lines.append(f"  - `{k}`:{uc[k]}/{valid_n} ({pct:.0f}%)")
            mvc = fs.get("modelVersion_count", {})
            if mvc:
                lines.append(f"- modelVersion 分布:")
                for mv, c in sorted(mvc.items(), key=lambda x: -x[1]):
                    pct = (c / denom * 100)
                    flag = " 🚨 占位符" if str(mv).endswith("-default") else ""
                    lines.append(f"  - `{mv}`:{c}/{valid_n} ({pct:.0f}%){flag}")
            placeholder_rate = fs.get("placeholder_rate", 0)
            if placeholder_rate > 0:
                lines.append(f"- 占位符比率:{placeholder_rate:.1%}")
            lines.append("")

        # thinkingBudget=0
        tb = active_data.get("thinkingBudget_zero", {})
        if tb:
            lines.append("#### B. thinkingBudget=0 探针")
            lines.append("")
            lines.append(f"- HTTP {tb.get('status')} → `{tb.get('verdict')}`")
            if tb.get("modelVersion"):
                lines.append(f"- 返回 modelVersion:`{tb['modelVersion']}`")
            if tb.get("error_message"):
                lines.append(f"- 错误片段:`{tb['error_message']}`")
            lines.append("")

        # 错误路径泄露
        leak = active_data.get("error_path_leak", {})
        if leak:
            lines.append("#### C. 错误路径泄露探针")
            lines.append("")
            lines.append(f"- 状态码:{leak.get('status')}")
            leaks = leak.get("leaks", {})
            if leaks.get("vertex_path"):
                lines.append(f"- 🚨 Vertex 路径泄露(`projects/.../locations/...`)")
            if leaks.get("aistudio_doc_url"):
                lines.append(f"- 🚨 AI Studio doc URL 泄露(`ai.google.dev`)")
            if leaks.get("group_name"):
                lines.append(f"- 🏷️ 分组泄露:`{leaks['group_name']}`")
            lines.append(f"- 错误片段:`{leak.get('error_message', '')}`")
            lines.append("")

        # cachedContents
        cc = active_data.get("cachedContents", {})
        if cc:
            lines.append("#### D. cachedContents 路径")
            lines.append("")
            tag = "🚫 被屏蔽" if cc.get("blocked") else f"HTTP {cc.get('status')}"
            lines.append(f"- {tag}")
            lines.append(f"- 响应片段:`{cc.get('body_snippet', '')}`")
            lines.append("")

        # HTTP 头
        hh = active_data.get("http_headers", {})
        if hh:
            lines.append("#### E. HTTP 响应头")
            lines.append("")
            lines.append(f"- 中转框架:**{hh.get('framework_hint', '?')}**")
            lines.append(f"- 上游头信号:**{hh.get('upstream_hint', '?')}**")
            ih = hh.get("interesting_headers", {})
            if ih:
                lines.append("- 关键响应头:")
                for k, v in ih.items():
                    # URL 编码的值(如中文分组名)解码后更可读
                    dv = unquote(v) if isinstance(v, str) and "%" in v else v
                    extra = f"  (解码: {dv})" if dv != v else ""
                    lines.append(f"  - `{k}: {v}`{extra}")
            lines.append("")

        # countTokens
        ct = active_data.get("count_tokens", {})
        if ct:
            lines.append("#### F. countTokens schema")
            lines.append("")
            lines.append(f"- 状态:`{ct.get('verdict')}` (HTTP {ct.get('status')})")
            if ct.get("fields"):
                lines.append(f"- 字段:{ct['fields']}")
            lines.append("")

        # identity
        ident = active_data.get("identity", {})
        if ident:
            lines.append("#### G. Identity 自报家门(Tier 2,可被 system prompt 改写)")
            lines.append("")
            if ident.get("note"):
                lines.append(f"- ⚠️ {ident['note']}")
            else:
                lines.append(f"- 回答:`{ident.get('text', '')}`")
            flags = ident.get("flags", {})
            if flags.get("antigravity"):
                lines.append("- 🚨 自报 Antigravity → OAuth 套壳硬证据")
            if flags.get("openai") or flags.get("anthropic"):
                lines.append("- 🚨 自报非 Google 模型 → 完全套壳")
            lines.append("")

        # Tier 4
        t4 = per.get("tier4", {})
        knw = t4.get("knowledge", {})
        if knw:
            lines.append("#### H. 知识截止探针(Tier 4)")
            lines.append("")
            answered = knw.get("answered_n", knw.get("total"))
            lines.append(
                f"- 答对 {knw.get('pass_count')} / 作答 {answered} / 共 {knw.get('total')} 题 "
                f"→ **{knw.get('verdict')}**"
            )
            icon = {"pass": "✅", "fail": "❌", "no_answer": "⬜"}
            for r in knw.get("results", []):
                oc = r.get("outcome") or ("pass" if r.get("passed") else "fail")
                ok = icon.get(oc, "❓")
                ans = r.get("answer", "") or "(空响应/未作答)"
                lines.append(f"  - {ok} `{r['id']}`:{ans}")
            lines.append("")

        ss = t4.get("self_sig", {})
        if ss:
            lines.append("#### I. 自 sig 重复性(Tier 4)")
            lines.append("")
            lines.append(f"- N = {ss.get('n')}, PASS={ss.get('pass')}, FAIL={ss.get('fail')}, step1_fail={ss.get('step1_fail')}, sig_corrupt={ss.get('sig_corrupt')}")
            lines.append(f"- 判定:**{ss.get('verdict')}**(FAIL 率 {ss.get('fail_rate')})")
            lines.append("")

        # 综合判定
        if isinstance(v, dict) and v:
            lines.append("#### 综合判定")
            lines.append("")
            label = v.get("label", "?")
            lines.append(f"- **{label}**(置信度 {v.get('confidence', '?')})")
            ev = v.get("primary_evidence", [])
            if ev:
                lines.append("- 证据:")
                for e in ev:
                    lines.append(f"  - {e}")
            cav = v.get("caveats", [])
            if cav:
                lines.append("- 注意事项:")
                for c in cav:
                    lines.append(f"  - {c}")
            lines.append("")

    # 附录
    lines.append("## 附录:原始数据")
    lines.append("")
    lines.append("- 全部原始抓包:`raw/<keyname>/`")
    lines.append("- 机器可读结构:`verdict.json`")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("生成自 `gemini-relay-audit`(零依赖,Python 3.10+)。")

    return "\n".join(lines) + "\n"
