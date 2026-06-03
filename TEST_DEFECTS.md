# 4sapi 三 Key 实测缺陷报告（v1.1.0 → v1.1.3）

> 测试对象: 4sapi.com 三个 key（key1=Gemini综合分组 / key2=gemini-cli / key3=Gemini-官方）
> 测试日期: 2026-06-02
> 测试方式: 用 gemini-relay-audit 工具实跑，多轮迭代修复

---

## 一句话总结

实测用 4sapi 三 key 跑工具，**连续发现 5 类缺陷**，全部修复。其中 2 个是会导致**完全错误结论**的判定 bug（一个崩溃、一个把正常渠道误判为"模型被替换"）。这正说明：方案文档再完善，工具不实跑就发现不了这些问题。

---

## 发现的缺陷清单

### 🔴 缺陷 1：clip 未导入导致程序崩溃（v1.1.1 修复）

- **现象**: 跑到判定阶段 `NameError: name 'clip' is not defined`，整个程序崩溃，无输出。
- **根因**: 上一轮 Fix 7（Server-Timing GFE 识别）在 `verdict.py` 用了 `clip()`，但该模块没导入它。典型的"加功能时漏依赖"回归。
- **修复**: `verdict.py` 顶部加 `from . import clip`。
- **教训**: 加功能后必须实跑一遍，纯静态 review 抓不到跨模块的符号缺失。

### 🟠 缺陷 2：Windows GBK 终端 emoji 崩溃（v1.1.1 修复）

- **现象**: 报告已生成，但最后打印结论时 `UnicodeEncodeError: 'gbk' codec can't encode '⚡'`，退出码非 0。
- **根因**: 标签含 emoji（⚡🔴✅❓），Windows 默认 GBK 终端无法编码。
- **修复**: `audit.py` 打印结论处加 `try/except UnicodeEncodeError`，回退到 ASCII。
- **备注**: 不影响报告文件（UTF-8 写入正常），只影响终端回显。

### 🔴 缺陷 3：thinkingBudget=0 信号权重不足导致 key3 误判（v1.1.1 修复）

- **现象**: key3 字段采样 100% trafficType + thinkingBudget=0 返回 200（被接受），却被判为"❓ 信号不足 (low)"。
- **根因**: `thinking_accepted` 只在已有 OAuth 信号时才加分；单独出现时被忽略。但"thinkingBudget=0 被接受"本身就是强信号——Vertex 一定 400，接受 = 非 Vertex。
- **修复**: `thinking_accepted and not strict_400` 时计入 AI Studio 信号。
- **效果**: key3 从"信号不足"→"⚡ AI Studio 强嫌疑 (medium)"，正确。

### 🟠 缺陷 4：限流(429/503)样本污染字段统计（v1.1.2 修复）

- **现象**: 同一 key 两次测试结果差异巨大——第二次 key1 因 429 限流，80% 样本落入 `unknown`，判定被拉低。
- **根因**: 字段采样不区分 200 和非 200，限流的空响应被当作"上游字段被洗"计入 `unknown`。但限流是**临时网络状态**，不是渠道指纹。
- **修复**:
  - 非 200 响应单独计入 `error_count`，不进 `upstream_count`。
  - 判定与占位符率改用 `valid_n`（有效样本数）计算。
  - 有效样本不足时在 caveats 警告。

### 🔴 缺陷 5：知识探针空响应被误判为"答错"→ 模型被替换（v1.1.3 修复）⭐ 最严重

- **现象**: key1 知识探针 3 题全是**空响应**，key3 是 2 空 + 1 对，两者都被判为"❌ 模型被静默替换 (high置信)"。
- **双重根因**:
  1. **思考饥饿**: Fix 4 设的 `maxOutputTokens:200` 太小，`thinkingBudget:-1`（动态思考）把 token 配额吃光，答案文本为空。
  2. **空响应=答错**: 空文本被 `passed=False` 当作"答错"，触发"模型被替换"误判。
- **修复**:
  - `maxOutputTokens` 200 → 2048，给思考留足空间。
  - 引入三态 `outcome`: `pass` / `fail` / `no_answer`。
  - 判定只基于**有作答**的题目；全部 `no_answer` → `inconclusive`（无法判定），不再误判为"被替换"。
- **离线验证**:
  | 场景 | 旧判定 | 新判定 |
  |------|--------|--------|
  | 全空响应 | ❌模型被替换 high | ✅ inconclusive |
  | 2空+1对 | ❌模型被替换 high | ✅ real_3x |
  | 真全对 | real_3x | real_3x ✓ |
  | 真答错 | model_replaced | model_replaced ✓ |

---

## 更深层的观察：单次快照不可靠

三 key 在间隔 ~20 分钟的多轮测试中，**同一 key 的字段分布、限流状态、路由节点都在变**：

- key1：第一次 80% serviceTier（AI Studio），第二次大面积 429 限流
- key2：占位符率在 40%~60% 间波动
- key3：modelVersion 节点在 rc.10 / v2026-05-20-dev 间切换

**结论**: 4sapi 是高度动态的多池路由。工具应：
1. 默认 N≥10 才下置信结论（当前 README 已建议，但默认值偏小）
2. 把"限流率""节点多样性"作为独立维度报告
3. 不被单次快照带偏（缺陷 4、5 都是单次快照误导的典型）

---

## 修复后的版本

| 版本 | 修复内容 |
|------|---------|
| v1.1.0 | 初始 12 项静态 review 修复 |
| v1.1.1 | clip 崩溃、Unicode、thinking 权重、GFE 置信度 |
| v1.1.2 | 限流样本隔离、identity 空响应标注 |
| v1.1.3 | 知识探针三态判定（空响应不再误判模型替换）⭐ |

---

## 仍存在的局限（非 bug）

1. **countTokens 探针对 4sapi 全部失效**——三 key 都把 countTokens 转发到 generateContent（方案 §五已记录）。工具已正确识别为 `endpoint_polluted`，但这条探针在此中转无效。
2. **限流影响 Tier 4**——key 被限流时知识探针/sig 探针都拿不到数据，只能标 inconclusive，需换时段重测。
3. **延迟分布探针（§8.2）仍未实现**——无法量化跳数。
