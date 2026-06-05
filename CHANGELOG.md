# Changelog

## [1.1.5] - 2026-06-05

### 判定规则

- 对齐 `Gemini 中转站渠道识别方案 v1.9`。
- `thinkingBudget=0` 改为模型能力感知判读:
  - 2.5 Flash / Flash-Lite 接受 0 是官方行为,不再作为 OAuth 证据。
  - Pro / 明确不支持关闭 thinking 的路由异常 200,才作为 OAuth 或中转改写嫌疑。
  - OAuth 结论需要结合 `X-Routing-Group`、identity、系统提示污染或 sig 回灌等独立证据。

### 工具脚本

- 新增共享分类器 `probes/thinking.py`。
- `manual_probe.py`、完整审计 `probes/active.py` / `probes/verdict.py`、以及
  `model_source_probe.py` 均复用 v1.9 规则。
- `model_source_probe.py` 报告新增 `thinkingBudget=0` 与 `countTokens` 端点污染摘要,原始
  `.http` 请求/响应包保留不变。

## [Unreleased] - 2026-06-04

### 文档补充

- 补充 Web/CLI base URL 说明,明确支持 `http://IP:port` 形态。
- 补充 `manual_probe.py` 手工低频探针用法,用于复核限流、role 校验和 OAuth 指纹。
- 补充 `model_enum.py` 模型枚举指纹说明,强调模型枚举结果是实测快照,不可当作长期固定指纹。
- 补充"信号不足"排查路径:区分限流、role 校验和数据不足,并指向低频手工探针。
- 补充敏感信息提示:辅助脚本中的真实 key、`reports/` 和 `model-enum.json` 公开前必须脱敏。

### 工具脚本

- `manual_probe.py` 改为通过 `--base`、`--key`、`--model`、`--sig-model` 等参数运行,
  删除硬编码真实凭据,并补齐 role 校验、`thinkingBudget=0`、`-nothinking` 和响应头指纹输出。
- `model_enum.py` 改为可安全导入的 CLI 工具,支持 `--model`、`--models-file`、`--out`、
  `--gap`、`--timeout` 和 429 重试,输出别名和 `-nothinking` 标记。

### Web

- Web 控制台新增"手工探针"和"模型枚举"页签,可从页面运行辅助脚本并实时查看 stdout 日志。
- Web 后端新增 `/api/tools/manual-probe` 与 `/api/tools/model-enum` 接口,复用现有 run 状态和
  SSE 日志流。
- 模型枚举 Web 任务会输出 `reports/model-enum-<runid>.json`,并在结果区提供下载链接。

### 行为说明

- `thinkingBudget=0 -> 200 accepted_as_is` 的判定语义会影响历史结论对比。旧版
  `verdict.json` 不应直接与新版判定横向比较。

## [1.1.4] - 2026-06-03

### 云雾 yunwu.ai「优质gemini」分组实测发现的缺陷

- **X-Routing-Group / 分组名 URL 编码未解码**: 中文分组名(如 `优质gemini`)在响应头里是
  URL 编码(`%E4%BC%98...`),OAuth 关键词匹配 `cli`/`oauth` 直接对编码串匹配会失效。
  新增 `_oauth_keyword()` 先 `unquote` 解码再匹配,报告也显示解码后的可读分组名。
- **严重限流时仍给 medium 判定**: 当字段采样有效样本 ≤1、限流 ≥3 次、且 Tier4 知识探针
  inconclusive 时,判定降级为 `low` 并在 caveats 置顶"🔁 建议换时段重测"。
  避免基于极少数据给出误导性结论。

## [1.1.3] - 2026-06-02

### 知识探针三态判定（4sapi 测试发现的致命误判）

- **空响应被误判为"答错"→ 模型被替换**: 限流或 `maxOutputTokens` 太小(思考饥饿)
  导致答案为空时,旧逻辑按 `passed=False` 当答错,把真 3.x 模型误判为
  "❌模型被静默替换 high"。修复:引入三态 `outcome`(pass/fail/no_answer);
  判定只看有作答的题,全 no_answer → `inconclusive`;`maxOutputTokens` 200→2048。

## [1.1.2] - 2026-06-02

### 实测发现的缺陷修复（4sapi 三 key 测试）

- **限流污染判定**: 字段采样中非 200 响应（429/503 限流）单独计入 `error_count`，
  不再污染 `upstream_count` 分类。判定改用 `valid_n`（有效样本数）计算比率，
  避免限流稀释指纹。当有效样本不足时在 caveats 中警告。
- **identity 空响应**: 探针返回非 200 或空文本时显式标注原因（限流/安全过滤），
  不再静默显示空白回答。
- **报告增强**: 字段采样段显示「有效 N / 限流 N」拆分，占位符率按有效样本计算。

## [1.1.1] - 2026-06-02

### 实测发现的崩溃 bug 修复

- **P0 崩溃**: `verdict.py` 使用 `clip()` 但未导入，导致判定阶段
  `NameError` 崩溃（Fix 7 引入的回归）。已添加 `from . import clip`。
- **Windows Unicode**: `audit.py` 打印 emoji 标签在 GBK 终端崩溃，
  添加 `UnicodeEncodeError` 回退到 ASCII。
- **thinkingBudget=0 信号权重**: `thinking_accepted`（非 400）现计入 AI Studio 信号，
  修复 key3 从「信号不足」误判为正确的「AI Studio 强嫌疑」。
- **GFE 置信度**: `server-timing: gfet4t7`（真 Google GFE）确认时，
  AI Studio 判定可升级为 high 置信度。

## [1.1.0] - 2026-06-02

### 修复的严重问题 (P0)

- **Fix 1**: 删除 `probe_identity` 中的死代码 (`if False`)，修复文本提取逻辑
- **Fix 2**: 修复 `probe_count_tokens` 端点污染检测
  - 新增检测：中转把 countTokens 转发到 generateContent
  - 正确检查 `totalBillableCharacters` 字段存在性
  - 返回更详细的字段信息

### 高优先级改进 (P1)

- **Fix 3**: 统一报告标签定义
  - 删除 `report.py` 中的重复 `LABEL_CN`
  - 直接使用 `verdict.py` 的 `LABELS`
- **Fix 4**: 知识探针显式禁用 thinking
  - 添加 `thinkingConfig: {thinkingBudget: -1}`
  - 添加 `maxOutputTokens: 200`
  - 减少 timeout 从 90s 到 60s
- **Fix 5**: 占位符 + 知识探针交叉判定
  - 在 `_judge_one` 中添加 caveat 提示
  - 在 `_decide` 中强化模型替换判定
- **Fix 6**: 添加6段***识别（方案 §1.4）
  - 在 `probe_error_path_leak` 中统计 `***` 段数
  - 在 `verdict.py` 中添加对应证据判定

### 中优先级改进 (P2)

- **Fix 7**: 利用 `server-timing: gfet4t7` 判定
  - 识别真 Google Frontend (GFE) 链路
  - 可与 OAuth 套壳区分
- **Fix 8**: 字段采样 sleep 时间从 0.2s 增加到 0.5s
  - 避免高频触发限流
- **Fix 9**: `thinkingBudget=0` 超时提示改进
  - 添加 OAuth 套壳延迟说明
  - timeout 从 120s 调整为 90s

### 低优先级改进 (P3)

- **Fix 10**: 改进错误处理
  - 添加 traceback 导出到文件
  - 保留完整错误上下文便于调试
- **Fix 11**: 更新版本号到 1.1.0
- **Fix 12**: 补充 README 文档
  - 添加判定标签说明
  - 添加常见问题 FAQ
  - 添加选型决策树
  - 添加已知限制说明

### 技术债务

- 延迟分布探针（§8.2 Tier 3）仍未实现
- 类型提示可以进一步加强（TypedDict）
- 仅一边上线的模型探针（§9.5）仍为可选未实现

---

## [1.0.0] - 2026-06-02

### 初始版本

- 实现完整的 Tier 1 主动探针（§九）
- 实现完整的 Tier 4 强证据探针（§十二）
- 实现判定引擎（§十四 决策树）
- 实现 Markdown 报告生成
- 零依赖设计（仅 Python 3.10+ 标准库）
- 支持多 key 跨 sig 矩阵分析
