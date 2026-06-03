# Gemini Relay Audit 修复方案

> 基于 2026-06-02 代码审查
> 对照方案版本: v1.8

---

## P0 — 严重bug（立即修复）

### Fix 1: identity probe 死代码bug

**文件**: `probes/active.py`  
**位置**: 行 312-313  
**问题**: `if False` 导致代码永远不执行

```python
# ❌ 当前代码（错误）
def probe_identity(...):
    text = ""
    parts = safe_get(data, "candidates", 0, "content", "parts") if False else None
    cands = safe_get(data, "candidates") or []
    if cands and isinstance(cands, list):
        ps = safe_get(cands[0], "content", "parts") or []
        for p in ps:
            if isinstance(p, dict) and "text" in p:
                text += p["text"]
```

```python
# ✅ 修复后
def probe_identity(...):
    text = ""
    cands = safe_get(data, "candidates") or []
    if cands and isinstance(cands, list):
        ps = safe_get(cands[0], "content", "parts") or []
        for p in ps:
            if isinstance(p, dict) and "text" in p:
                text += p["text"]
```

**操作**: 删除带 `if False` 的死代码行。

---

### Fix 2: countTokens 端点污染检测

**文件**: `probes/active.py`  
**位置**: `probe_count_tokens` 函数（行 259-291）  
**问题**: 
1. 没有检测中转把 countTokens 转发到 generateContent 的情况
2. 没有真正检查 `totalBillableCharacters` 字段

```python
# ❌ 当前代码（不完整）
def probe_count_tokens(...) -> dict:
    payload = {"contents": [{"parts": [{"text": "hello world"}]}]}
    status, data, hdrs = post_count_tokens(base, model, api_key, payload, timeout=30)
    save_raw(out_dir, "p6-countTokens.json", {"status": status, "body": data})

    if status != 200:
        return {
            "name": "count_tokens",
            "status": status,
            "verdict": f"failed_{status}",
            "blocked": True,
        }

    has_billable = "totalBillableCharacters" in (data or {})
    has_total = "totalTokens" in (data or {})
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
        "fields": list((data or {}).keys()) if isinstance(data, dict) else [],
    }
```

```python
# ✅ 修复后
def probe_count_tokens(...) -> dict:
    """countTokens schema 差异:Vertex 有 totalBillableCharacters,AI Studio 没有
    
    注意：部分中转会把 countTokens 转发到 generateContent (方案 §五 已知坑点)，
    导致返回完整的 candidates 数组而非简单的 token 统计。
    """
    payload = {"contents": [{"parts": [{"text": "hello world"}]}]}
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
```

**操作**: 替换整个函数。

---

## P1 — 高优先级（影响判定准确性）

### Fix 3: 统一报告标签

**文件**: `probes/report.py`  
**位置**: 行 5-14  
**问题**: `LABEL_CN` 与 `verdict.py` 的 `LABELS` 键名不匹配

```python
# ❌ 当前代码
LABEL_CN = {
    "vertex_confirmed": "✅ Vertex(强证据)",
    "vertex_likely": "⚡ Vertex 嫌疑",
    "aistudio_confirmed": "✅ AI Studio(强证据)",
    "aistudio_likely": "⚡ AI Studio 嫌疑",
    "oauth_shell": "🔴 OAuth 套壳(Antigravity / Gemini CLI)",  # ❌ verdict.py 没有这个key
    "model_replaced": "🔴 模型被静默替换",
    "non_google_shell": "🔴 非 Google 套壳",  # ❌ verdict.py 没有这个key
    "unknown": "❓ 未确定",
}
```

```python
# ✅ 修复后（与 verdict.py:14-25 对齐）
# 直接导入，避免重复定义
from .verdict import LABELS

# 如果需要简化版，可以这样：
def _short_label(label: str) -> str:
    """从 verdict.LABELS 提取 emoji 前缀的简短标签"""
    return label.split("(")[0].strip() if "(" in label else label
```

**或者更简单的方案**：在 `render` 函数中直接使用 `v.get("label")`，不需要 LABEL_CN。

**操作**: 删除 `LABEL_CN`，直接使用 `verdict.LABELS` 或 `v["label"]`。

---

### Fix 4: 知识探针显式禁用 thinking

**文件**: `probes/tier4.py`  
**位置**: `probe_knowledge` 函数（行 70-98）  
**问题**: 没有禁用 thinking，导致响应慢且浪费 token

```python
# ❌ 当前代码
def probe_knowledge(...) -> dict:
    """对模型问 3 个 cutoff 后事件——不传 thinkingConfig,让模型自己决定"""
    results = []
    for q in KNOWLEDGE_QUESTIONS:
        payload = {"contents": [{"parts": [{"text": q["q"]}]}]}
        status, data, _ = post_generate(base, model, api_key, payload, timeout=90)
```

```python
# ✅ 修复后
def probe_knowledge(...) -> dict:
    """对模型问 3 个 cutoff 后事件——显式禁用 thinking 以加速响应
    
    方案 §12.A：问题文本已包含"不要联网搜索"，但实际操作应该通过
    generationConfig 禁用 thinking，否则：
    - 响应慢 15-30s
    - 浪费大量 thinking tokens
    - 可能影响直接回答的准确性
    """
    results = []
    for q in KNOWLEDGE_QUESTIONS:
        payload = {
            "contents": [{"parts": [{"text": q["q"]}]}],
            "generationConfig": {
                "thinkingConfig": {"thinkingBudget": -1},  # 禁用 thinking
                "maxOutputTokens": 200,  # 短答案足够
            },
        }
        status, data, _ = post_generate(base, model, api_key, payload, timeout=60)  # timeout 也可以减少
```

**操作**: 修改 payload 结构，添加 generationConfig。

---

### Fix 5: 占位符 + 知识探针交叉判定

**文件**: `probes/verdict.py`  
**位置**: `_decide` 函数（行 298-369）  
**问题**: 没有充分利用占位符 + 知识探针的交叉信号

```python
# ✅ 在 _decide 函数开头添加（在 "模型被替换" 判定之后）
def _decide(flags: dict[str, bool]) -> tuple[str, str]:
    """..."""
    
    # --- 套壳冒充优先级最高 ---
    if flags.get("non_google"):
        return "non_google_impersonation", "high"

    # --- 模型被替换(Tier 4 知识探针) + 占位符交叉验证 ---
    if flags.get("model_replaced_suspect") and not flags.get("model_real_3x"):
        # 如果同时有占位符，这是静默替换的强证据
        if flags.get("placeholder_seen"):
            return "model_replaced", "high"  # 硬证据
        return "model_replaced", "high"
    
    # 占位符但模型本体真实 → 仅中转改字符串，不是模型替换
    # 这种情况不拉黑，但需要在 caveats 中标注（在 _judge_one 中处理）
```

同时在 `_judge_one` 中添加 caveat：

```python
# 在 _judge_one 函数的 evidence 收集完成后，决策前添加：
if flags.get("placeholder_seen") and flags.get("model_real_3x"):
    caveats.append(
        "⚠️ modelVersion 返回占位符（*-default），但知识探针证明模型本体真实 — "
        "仅中转改字符串，不是模型替换（方案 §1.9, §13.3 修正）"
    )
```

---

### Fix 6: 错误信息6段***识别

**文件**: `probes/active.py`  
**位置**: `probe_error_path_leak` 函数（行 146-177）  
**问题**: 没有识别方案 §1.4 提到的 6 段 `***` 模式

```python
# ✅ 在 leaks 检测部分添加
def probe_error_path_leak(...) -> dict:
    """..."""
    err_msg = safe_get(data, "error", "message") or str(data)
    leaks = {}
    
    if "projects/" in err_msg and "/locations/" in err_msg:
        leaks["vertex_path"] = True
    if "ai.google.dev" in err_msg or "for API version v1beta" in err_msg:
        leaks["aistudio_doc_url"] = True
    
    # 新增：6段 *** 识别（方案 §1.4）
    if "***" in err_msg:
        # 统计连续的 *** 段落数（用 / 分隔）
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
```

然后在 `verdict.py` 的 `_judge_one` 中添加对应的 evidence：

```python
# 在错误路径泄露 section 添加
if leaks.get("vertex_doc_masked"):
    evidence.append({
        "tier": 2,
        "name": "错误路径泄露",
        "fact": f"错误信息含 {leaks.get('masked_segments', 6)} 段 ***",
        "implication": "中转屏蔽了 cloud.google.com/vertex-ai 关键字（方案 §1.4），间接证明上游是 Vertex",
    })
    flags["vertex_doc_masked"] = True
```

---

## P2 — 中优先级（改善体验）

### Fix 7: 利用 server-timing: gfet4t7

**文件**: `probes/verdict.py`  
**位置**: `_judge_one` 函数，HTTP 头 section（行 163-190）  

```python
# ✅ 在 HTTP 头判定部分添加
def _judge_one(...):
    # ...
    # HTTP 头
    hdr = active.get("http_headers") or {}
    interesting = hdr.get("interesting_headers", {})
    
    # 新增：GFE 识别
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
        # ... 现有代码
```

然后在 `_decide` 中可以用这个信号：

```python
# 在 Vertex 硬证据判定部分
if flags.get("vertex_path_confirmed") or flags.get("vertex_header_confirmed"):
    return "vertex_confirmed", "high"

# 如果有 GFE + 其他 Vertex 弱信号，可以升级置信度
# （可选，看实际需要）
```

---

### Fix 8: 字段采样 sleep 时间调整

**文件**: `probes/active.py`  
**位置**: `probe_field_sampling` 函数（行 90）

```python
# ❌ 当前
time.sleep(0.2)

# ✅ 修复
time.sleep(0.5)  # 更保守，避免触发限流
```

**可选**：添加 `--fast` 模式支持用户自定义 sleep 时间。

---

### Fix 9: thinkingBudget=0 探针的 timeout 提示

**文件**: `probes/active.py`  
**位置**: `probe_thinking_budget_zero` 函数（行 103-143）

```python
# ✅ 增加 timeout 处理和提示
def probe_thinking_budget_zero(...) -> dict:
    """thinkingBudget=0:Vertex 一定 400,AI Studio 通常拒绝。
    
    OAuth 套壳可能：
    1. 接受并返回 200（改写为 -nothinking 别名）
    2. 触发完整推理，延迟 15-60s（方案 §9.4b）
    """
    payload = {
        "contents": [{"parts": [{"text": "reply with: ok"}]}],
        "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}},
    }
    # timeout 从 120 改为 90 或保持 120（够用）
    status, data, hdrs = post_generate(base, model, api_key, payload, timeout=90)
    save_raw(out_dir, "p1-thinkingBudget0.json", {"status": status, "headers": hdrs, "body": data})

    if status == 400:
        verdict = "vertex_or_strict"
        signal = "rejected_400"
    elif status == 200:
        mv = data.get("modelVersion", "")
        if "nothinking" in mv:
            verdict = "rewritten_to_nothinking"
            signal = "rewritten"
        else:
            verdict = "accepted_as_is"
            signal = "accepted"
    elif status == -1:
        verdict = "timeout"
        signal = "timeout"
        # 新增：timeout 提示
        data = data or {}
        data["note"] = "OAuth 套壳上游可能触发完整推理导致超时（方案 §9.4b）"
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
        "note": data.get("note") if isinstance(data, dict) else None,
    }
```

---

## P3 — 低优先级（代码质量改善）

### Fix 10: 错误处理改进

**文件**: `audit.py`  
**位置**: `active.run_all` 函数的 step helper（行 27-33）

```python
# ❌ 当前（吞掉所有异常）
def step(label, fn, *args, **kw):
    if not quiet:
        print(f"  [active] {label} ...", flush=True)
    try:
        return fn(*args, **kw, out_dir=out_dir)
    except Exception as e:
        return {"name": label, "error": f"{type(e).__name__}: {e}"}
```

```python
# ✅ 改进（记录 traceback）
import traceback

def step(label, fn, *args, **kw):
    if not quiet:
        print(f"  [active] {label} ...", flush=True)
    try:
        return fn(*args, **kw, out_dir=out_dir)
    except Exception as e:
        tb = traceback.format_exc()
        # 保存到文件
        err_path = out_dir / f"ERROR-{label.replace(' ', '_')}.txt"
        with open(err_path, "w", encoding="utf-8") as f:
            f.write(f"Exception in {label}\n\n{tb}")
        if not quiet:
            print(f"    ⚠️ Error (saved to {err_path.name})", flush=True)
        return {"name": label, "error": f"{type(e).__name__}: {e}", "traceback_file": str(err_path)}
```

---

### Fix 11: 类型提示改进

**文件**: 多个文件  
**建议**: 添加返回类型的 TypedDict 定义

```python
# 在 probes/__init__.py 或新建 probes/types.py
from typing import TypedDict, Literal

class ProbeResult(TypedDict, total=False):
    name: str
    status: int
    verdict: str
    error: str
    note: str

class FieldSampleResult(ProbeResult):
    n: int
    samples: list[dict]
    upstream_count: dict[str, int]
    modelVersion_count: dict[str, int]
    placeholder_rate: float

# 然后在各探针函数签名中使用
def probe_field_sampling(...) -> FieldSampleResult:
    ...
```

**注意**：这是可选的，Python 3.10+ 对 TypedDict 支持较好。

---

### Fix 12: README 补充

**文件**: `README.md`  
**建议内容**:

```markdown
## 如何解读报告

### 判定标签说明

| 标签 | 含义 | 是否可用 |
|------|------|----------|
| ✅ 真 Vertex AI | GCP 项目直连，企业级 | ✅ 推荐（敏感数据） |
| ⚡ AI Studio 强嫌疑 | Google 个人开发者 API | ✅ 可用（非敏感） |
| 🔴 OAuth 套壳 | Antigravity / Gemini CLI | ⚠️ 不稳定 |
| ❌ 模型被静默替换 | 权重不是声称的版本 | ❌ 拉黑 |

### 常见问题

**Q: countTokens 显示 `endpoint_polluted` 是什么意思？**  
A: 中转把 countTokens 转发到了 generateContent，这是已知问题（方案 §五）。不影响其他探针。

**Q: 自 sig 重复性显示 `no_sig_returned`？**  
A: 模型没有返回 thoughtSignature，尝试：
- 换用 `--sig-model gemini-3-flash-preview`
- 检查中转是否支持 3.x 系列模型

**Q: 知识探针超时？**  
A: 增加 `--timeout 180` 或检查网络连接。

### 选型决策树

```
有 Vertex 硬证据？
├─ 是 → ✅ 用于生产
└─ 否 → 有 AI Studio 证据？
    ├─ 是 + 知识探针通过 → ✅ 可用于非敏感场景
    ├─ 是 + 多池 → ⚠️ 不稳定，仅测试用
    └─ OAuth 套壳 → ❌ 不推荐
```

## 已知限制

- 无法区分 AI Studio 的 free / standard tier（仅知道有 serviceTier 字段）
- countTokens 在部分中转上被转发到 generateContent，探针失效
- 延迟分布探针（§8.2）未实现，无法精确测量跳数
```

---

## 修复执行清单

### 立即执行（P0）

- [ ] Fix 1: 删除 `probes/active.py:312` 的 `if False` 行
- [ ] Fix 2: 替换 `probes/active.py` 的 `probe_count_tokens` 函数

### 高优先级（P1）

- [ ] Fix 3: 统一 `probes/report.py` 的标签定义
- [ ] Fix 4: 修改 `probes/tier4.py` 的 `probe_knowledge` 添加 thinkingConfig
- [ ] Fix 5: 修改 `probes/verdict.py` 的 `_decide` 和 `_judge_one` 添加占位符交叉判定
- [ ] Fix 6: 修改 `probes/active.py` 和 `probes/verdict.py` 添加6段***识别

### 中优先级（P2）

- [ ] Fix 7: 修改 `probes/verdict.py` 添加 GFE 识别
- [ ] Fix 8: 修改 `probes/active.py:90` sleep 时间
- [ ] Fix 9: 修改 `probes/active.py` thinkingBudget=0 超时提示

### 低优先级（P3）

- [ ] Fix 10: 改进错误处理
- [ ] Fix 11: 添加类型提示
- [ ] Fix 12: 补充 README

---

## 测试建议

修复后运行以下测试：

```bash
# 1. 基础冒烟测试（Fix 1, 2）
python audit.py --base https://4sapi.com --key sk-xxx --name test-fixes --skip-tier4 --n-samples 3

# 2. 完整测试（所有 fixes）
python audit.py --base https://4sapi.com --key sk-xxx --name full-test --n-samples 10 --n-self-sig 5

# 3. 多 key 测试（Fix 5 跨 key 矩阵）
python audit.py --base https://4sapi.com --key key1=sk-aaa --key key2=sk-bbb --name multi-key-test

# 4. 检查报告质量
# - verdict.json 中 countTokens 应显示 endpoint_polluted 或正确的 vertex_likely/aistudio_likely
# - identity 文本应正确提取
# - 占位符 + 知识探针的交叉判定应在 caveats 中体现
```

---

## 版本更新建议

修复完成后更新版本号：

- `audit.py:183` `tool_version` → `1.0.1` (P0 修复)
- `audit.py:183` `tool_version` → `1.1.0` (P0+P1 修复)

---

生成时间: 2026-06-02  
审查基准: Gemini中转站渠道识别方案 v1.8
