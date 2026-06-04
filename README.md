# Gemini Relay Audit — 一键审计 Gemini 中转站

基于 [Gemini 中转站渠道识别方案 v1.8](../Gemini中转站渠道识别方案.md) 的自动化 Python 工具。

**零外部依赖** — 仅 Python 3.10+ 标准库即可运行。

## 快速开始

```bash
# 单 key,完整审计(约 5-10 分钟)
python audit.py \
    --base https://yunwu.ai \
    --key "sk-xxxxx" \
    --name yunwu-audit

# 多 key(自动跑跨 key sig 矩阵)
python audit.py \
    --base https://4sapi.com \
    --key "key1=sk-aaaa" \
    --key "key2=sk-bbbb" \
    --key "key3=sk-cccc" \
    --name 4sapi-audit

# 快速冒烟(只跑 Tier 1 字段指纹,约 1 分钟)
python audit.py \
    --base https://yunwu.ai \
    --key "sk-x" \
    --name quick-smoke \
    --skip-tier4 \
    --n-samples 5
```

`--base` 支持标准 Gemini 兼容中转地址,可以是 `https://domain`、`http://IP:port`
或带反向代理路径的 base URL。工具会自动去掉末尾 `/`。

## Web 可视化

本项目也提供一个零依赖 Web 控制台,用于填写 URL / key / 模型并实时查看检测过程。

```bash
# 本地启动
python -m web.server --host 127.0.0.1 --port 8080
```

打开:

```text
http://127.0.0.1:8080
```

Web 端会调用同一个 `audit.py` CLI,实时展示命令行输出,检测结束后读取
`verdict.json` 和 `report.md` 生成可视化报告。

### Web 功能

- 可视化填写 `base URL`、单个或多个 API key、主探测模型、sig 模型
- 支持配置 `n_samples`、`n_self_sig`、`timeout` 和跳过探针开关
- 像命令行一样实时展示检测输出
- 按探针阶段展示检测进度
- 检测结束后展示每个 key 的最终判定、置信度、主要证据和注意事项
- 可视化展示字段采样分布、HTTP 头信号、Tier 4 结果和跨 key sig 矩阵
- 保留 `verdict.json`、`report.md` 和 `raw/` 原始抓包产物
- 在同一页面运行手工低频探针和模型枚举,复用实时命令行输出
- 模型枚举完成后可下载 `model-enum-<runid>.json`

### Web key 输入格式

单 key:

```text
sk-xxxxx
```

多 key:

```text
key1=sk-aaaa
key2=sk-bbbb
key3=sk-cccc
```

不写名称时会自动命名为 `key1`、`key2`。

### Docker 部署

```bash
docker build -t gemini-relay-audit .
docker run --rm -p 8080:8080 -v "${PWD}/reports:/app/reports" gemini-relay-audit
```

PowerShell 示例:

```powershell
docker run --rm -p 8080:8080 -v "${PWD}\reports:/app/reports" gemini-relay-audit
```

可选鉴权:

```bash
docker run --rm -p 8080:8080 \
  -e WEB_AUTH_TOKEN="change-me" \
  -v "${PWD}/reports:/app/reports" \
  gemini-relay-audit
```

启用鉴权后访问:

```text
http://127.0.0.1:8080/?token=change-me
```

### Web 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `WEB_HOST` | `0.0.0.0` | Web 服务监听地址 |
| `WEB_PORT` | `8080` | Web 服务监听端口 |
| `WEB_REPORT_ROOT` | `reports` | Web/CLI 共享的报告输出目录 |
| `WEB_AUTH_TOKEN` | 空 | 设置后需要通过 `?token=` 或 Bearer token 访问 API |
| `WEB_QUIET` | 空 | 设为 `1` 时减少 Web 服务访问日志 |

生产或公网环境建议设置 `WEB_AUTH_TOKEN`,并将 `reports/` 挂载为持久化 volume。

## 辅助排查脚本

主流程仍然是 `audit.py`。以下脚本用于复核疑难样本或低频手工排查,尤其适合遇到
限流、"信号不足"、模型别名/静默替换疑问时使用。

这些辅助工具既可直接命令行运行,也可在 Web 控制台的"手工探针"和"模型枚举"页签中运行。
Web 端会对日志里的 key 做脱敏,但仍建议使用临时测试 key。

### 手工低频探针

`manual_probe.py` 按决策树拆成低频单步探针,支持只跑指定步骤并设置步骤间隔:

```bash
# 跑全部步骤,每步间隔 3 秒
python manual_probe.py \
  --base https://relay.example.com \
  --key "sk-xxxxx" \
  --gap 3

# 只跑指定步骤
python manual_probe.py \
  --base http://127.0.0.1:8088 \
  --key "sk-xxxxx" \
  --model gemini-3.1-pro-preview \
  1a 1b 2a \
  --gap 5
```

步骤号由脚本内 `STEPS` 定义。常用步骤:

| 步骤 | 内容 |
|---|---|
| `1a` | generateContent baseline,抓响应头、`modelVersion`、`trafficType`、`serviceTier` |
| `1b` | 错误路径泄露,抓分组名、GCP 路径、`ai.google.dev`、屏蔽痕迹 |
| `1c` | countTokens schema,识别 Vertex 字段或端点污染 |
| `2a` | `thinkingBudget=0`,识别严格拒绝、OAuth 原样接受、`-nothinking` 改写 |
| `2b` | identity 自报家门 |
| `3a` | 知识截止探针 |
| `3b` | 自 thoughtSignature 回灌 |

脚本不会保存 key,但终端历史和 shell 进程列表可能短暂暴露命令行参数。敏感环境可使用临时
测试 key,并避免把完整命令粘贴到公开 issue 或报告中。

### 模型枚举指纹

`model_enum.py` 用真实 `generateContent` 调用逐个枚举模型,记录实际 `modelVersion`、
`trafficType`、`serviceTier` 等字段,输出:

```bash
python model_enum.py \
  --base https://relay.example.com \
  --key "sk-xxxxx" \
  --model gemini-3.1-pro-preview \
  --model gemini-3-flash-preview \
  --out reports/model-enum.json
```

也可以从文件读取模型列表:

```bash
python model_enum.py \
  --base https://relay.example.com \
  --key "sk-xxxxx" \
  --models-file models.txt \
  --gap 3
```

```text
reports/model-enum.json
```

注意: `GET /v1beta/models` 的列表不等于真实可调用模型。模型上架、别名和静默替换都具有
时效性,`model-enum.json` 只能视为本次实测快照,不应写死为长期固定指纹。

## 输出

```
reports/<name>-<timestamp>/
├── verdict.json   # 机器可读的完整数据 + 判定结果
├── report.md      # 人读的审计报告
└── raw/<keyname>/ # 每个探针的原始响应抓包
    ├── p1-thinkingBudget0.json
    ├── p2-samples.json
    ├── p3-error-leak.json
    ├── p4-cachedContents.txt
    ├── p5-headers.json
    ├── p6-countTokens.json
    ├── p7-identity.json
    ├── knowledge.json
    ├── self-sig.json
    └── cross-sig.json (多 key 时)
```

## 探针列表

| 探针 | Tier | 成本(单 key) | 识别内容 |
|---|---|---|---|
| thinkingBudget=0 | 2 | 1 次调用 | Vertex 一定 400;OAuth 套壳接受 |
| N 字段采样 | 1 | N 次调用(默认 20) | trafficType/serviceTier 分布,占位符率 |
| 错误路径泄露 | 2 | 1 次调用 | 分组名泄露,Vertex/AI Studio 路径残留 |
| cachedContents | 1 | 1 次调用 | 端点屏蔽 vs 转发 |
| HTTP 响应头 | 1 | 1 次调用 | X-Routing-Group / X-Gemini-Service-Tier / NewAPI 版本 |
| countTokens schema | 2 | 1 次调用 | totalBillableCharacters → Vertex |
| identity 自报家门 | 2 | 1 次调用 | Antigravity / 非 Google 套壳识别 |
| 知识截止探针 | 4 | 3 次调用 | 模型权重真实性(2024 大选/奥运/iPhone16) |
| 自 sig 重复性 | 4 | 2×N 次调用 | 多上游池量化 |
| 跨 key sig 矩阵 | 4 | K×K 次调用 | 账号拓扑揭示 |
| 手工低频探针 | 辅助 | 按步骤 | 低频复核限流/role 校验/OAuth 指纹 |
| 模型枚举指纹 | 辅助 | 按模型数 | 逐模型验证真实 modelVersion 与字段指纹 |

## 额外指纹与已知坑点

- **强制 role 校验**: 部分中转要求 `contents[].role` 显式存在。缺 role 时可能返回 400,
  进而让主动探针看起来像"信号不足"。新版探针 payload 已补 `role: user`。
- **`thinkingBudget=0` 返回 200**: 这是非 Vertex 的强信号。若同时没有 AI Studio 字段证据,
  应优先怀疑 OAuth/CLI 套壳,不要简单归入 AI Studio。
- **`-nothinking` / 422**: 中转可能把禁用 thinking 的请求改写到 `-nothinking` 别名,
  或在不支持别名时返回 422。该现象需要结合字段采样、响应头和手工探针复核。
- **`x-accel-buffering: no`**: 常见于反向代理/流式转发链路,只能作为链路形态提示,
  不能单独证明上游渠道。

## 命令行参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--base` | (必选) | 中转 base URL |
| `--key` | (必选) | API key,可重复多次 |
| `--name` | (必选) | 审计名字(用于输出目录) |
| `--out-root` | `reports` | 输出根目录 |
| `--model` | `gemini-3.1-pro-preview` | 主探测模型 |
| `--sig-model` | `gemini-3-flash-preview` | sig 回灌专用模型(需稳定返回 thoughtSignature) |
| `--n-samples` | 20 | 字段采样 N |
| `--n-self-sig` | 8 | 自 sig 重复性 N |
| `--timeout` | 120 | 单次 HTTP 超时秒数 |
| `--skip-active` | false | 跳过主动探针 |
| `--skip-tier4` | false | 跳过 Tier 4 探针(快速冒烟) |
| `--skip-cross-sig` | false | 跳过跨 key sig 矩阵 |
| `--quiet` | false | 减少输出 |

## 判定逻辑

遵循方案 v1.8 §十四 的标准化决策树。

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

**Q: 判定为"信号不足"怎么办？**
A: 先看 `verdict.json` 里的字段采样有效数和错误数。如果是限流,降低 `--n-samples`、
增加 `--timeout` 或换时段重测。如果大量 400 与 role 校验有关,确认使用的是新版探针;
仍无法判断时用 `manual_probe.py` 低频跑关键步骤复核。

**Q: 为什么模型枚举结果和模型列表不一致？**
A: 中转的模型列表可能是静态配置或别名映射,不代表真实可调用上游。以
`model_enum.py` 的真实调用结果和返回的 `modelVersion` 作为本次快照参考。

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
- 判定逻辑会随实测修正演进。发生判定语义变更后,旧版 `verdict.json` 不应与新版结论
  直接横向对比。
- `reports/` 和 `reports/model-enum.json` 可能包含渠道、响应头、responseId、thoughtSignature
  等敏感信息,公开前需要脱敏。

## 项目结构

```
gemini-relay-audit/
├── audit.py              # 主入口(CLI + 编排)
├── Dockerfile            # Web 端容器部署入口
├── README.md             # 本文件
├── manual_probe.py       # 可选:手工低频决策树探针
├── model_enum.py         # 可选:全模型真实调用枚举
├── probes/
│   ├── __init__.py       # HTTP 客户端 + 公共工具
│   ├── active.py         # Tier 1 主动探针
│   ├── tier4.py          # Tier 4 强证据探针
│   ├── verdict.py        # 判定引擎
│   └── report.py         # Markdown 报告生成
├── web/
│   ├── server.py         # 标准库 Web 服务 + CLI 任务封装
│   └── static/           # 原生 HTML/CSS/JS 可视化界面
└── tests/
    └── test_web_server.py
```
