# 🏛️ AI Council

**多模型会谈系统** — 一次提问，多厂商 AI 独立提案 → 评审 → 辩论 → 裁定，自动收敛出一份可执行的最终方案。

不再在多个 AI 网页之间来回复制粘贴。一次提问，多模型独立提案 → 独立评审 → 分歧辩论 →
独立裁定 → 修订重审，直到通过或达到轮次上限，产出终稿、会议纪要、分歧存档与成本统计。

[![CI](https://github.com/xmwy0712/ai-council/actions/workflows/ci.yml/badge.svg)](https://github.com/xmwy0712/ai-council/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Models](https://img.shields.io/badge/models-126%20%2F%2014%20vendors-orange)](docs/PROVIDERS.md)
[![Offline tests](https://img.shields.io/badge/offline%20tests-463%20passing-brightgreen)](https://github.com/xmwy0712/ai-council/actions/workflows/ci.yml)

[English](#english) | [中文](#中文)

---

## 中文

### 它不是什么

AI Council 是一个**智囊团**，不是一个 agent。它只做两件事：文本对话，以及只读地阅读你主动提供的 `.txt` / `.md` 文件。

它**不会**创建或修改文件、不会执行命令、不会访问除模型 API 之外的任何网络（**唯一的例外是可选开启的模型清单更新**，默认关闭，见下）。任何模型的输出都不能触发文件系统写入——应用自身只会写入两处：应用数据目录（配置、会话、日志），以及你主动点击导出时选择的目标路径。

### 和其他多模型工具比

| 特性 | AI Council | 多开几个聊天窗 | Prompt 框架 |
|---|:---:|:---:|:---:|
| 并行独立提案 | ✅ | ❌ | ❌ |
| 结构化评审与辩论 | ✅ | ❌ | ❌ |
| 自动收敛出终稿 | ✅ | ❌ | ❌ |
| 分歧与决策留档 | ✅ | ❌ | ❌ |
| 本地运行、离线可测 | ✅ | ❌ | ✅ |
| Web UI + CLI | ✅ | ❌ | ❌ |

重点不在「同时问几个 AI」，而在**流程约束**：互不可见的独立提案、只提问题不重写的评审、
只回应未消解分歧的辩论，以及由独立 Judge 按固定判据裁定的收敛。

### 系统要求

- **Python** 3.11+；或直接用 Windows 免安装包（`ai-council.exe` 双击即用，无需 Python）
- **模型密钥**：11 家预设厂商，也可填任意自定义变量名；只写入本机系统钥匙串
- **网络**：仅连接模型 API。模型清单自动更新默认关闭，开启后也只访问各厂商自己的列模型接口

### 模型清单保持新鲜（可选，默认关闭）

模型会过时，但你不该为此手改 TOML。在 `council serve` 的**设置 → 模型清单更新**里打开开关，AI Council 就会问各厂商自己的「列出模型」接口当前在役什么，把注册表里还没有的 id 自动补进数据目录。

- 三条探测路径覆盖 13/14 家（OpenAI 兼容 `{base_url}/models`、Anthropic `/v1/models`、Google `v1beta/models`）；本地 CLI 的订阅没有可查询接口，不更新。
- **只补缺，不覆盖**：内置注册表里的条目带已核实的思考档位，永远优先；自动发现的条目在网页端单独成组，不会与已策展的混在一起。
- **思考档位只在拿得到可核实信号时才声明**：厂商自己的列模型接口（Anthropic 会直接报出档位），或公开目录按完全一致的 id 命中的条目。参数名与档位一律取自注册表已声明的厂商声明，绝不发明——猜错协议是硬 400。拿不到信号的模型照样可选，只是暂不提供档位。
- 不写价格——网关对别家模型的标价不是本项目愿意落笔的数字。
- 关闭状态下「除模型端点外不联网」这句承诺字面成立。

### 🚀 30 秒快速开始

**方案 A：装了 Python（推荐）**

```bash
uv pip install ai-council          # 或 pipx install ai-council
council run "我要在三个月内把日活从 1 万做到 10 万，应该怎么做？"
```

首场无需任何密钥——内置 fake 适配器会让完整八阶段流程立刻跑起来。

**方案 B：Windows、不想装 Python**

到 [Releases](https://github.com/xmwy0712/ai-council/releases) 下载，三种任选：

| 产物 | 特点 |
|---|---|
| `ai-council.exe` | 单文件，下载即运行 |
| `ai-council-win64.zip` | 解压即用，启动更快 |
| `AI-Council-Setup-<ver>.exe` | 用户级安装器，带开始菜单图标 |

未签名，首次运行可能被 SmartScreen 拦截：更多信息 → 仍要运行。

**方案 C：想先看界面**

```bash
council serve     # 浏览器打开 http://127.0.0.1:8765
```

密钥可在界面里填：**设置 → 密钥**，支持 OpenAI / Anthropic / 智谱 / 通义 / xAI /
OpenRouter 等 11 家预设或任意自定义变量名——只写系统钥匙串、绝不回显，
并标注它当前来自环境变量 / `.env` / 钥匙串 / 未配置。

`config.toml` 默认在应用数据目录（Windows `%LOCALAPPDATA%\ai-council\`），
`council run --config ./config.toml "问题"` 可指定路径；`council config-check` 校验配置。

### 💡 典型用法

| 场景 | 得到什么 |
|---|---|
| **技术方案选型** | 多份独立设计 → 评审挑刺 → 辩论收敛，避免一言堂 |
| **复杂 Bug 定位** | 前后端视角分别诊断 → 交叉质询 → 收敛到根因假设 |
| **文案 / 内容打磨** | 多稿独立产出 → 互相评审 → 合成终稿 |
| **决策前的红队** | 主动制造分歧，把风险与前提假设摊开留档 |

### 会议是怎么开的

| 阶段 | 干什么 | 并发 |
|---|---|---|
| P0 CONTEXT | 载入问题与附件，计算 token 预算 | — |
| P1 PROPOSAL | 所有参与者**互不可见**地各出一份方案 | 并行 |
| P2 SELECT | Judge 打分选主案（可切人工选择） | 单次 |
| P3 REVIEW | 除作者外全员独立评审，只提问题不重写 | 并行 |
| P4 DEBATE | 最多 `debate_rounds` 轮 round-robin，只回应未消解的分歧 | 轮内串行 |
| P5 VERDICT | Judge 裁定 `PASS` / `NEED_REVISION` / `NEED_USER_DECISION` | 单次 |
| P6 REVISION | 作者产出**完整**的下一版，不允许只给修改日志 | 单次 |
| P7 FINAL | 终稿 + 会议纪要 + 分歧存档 + 耗时与 token 统计 | — |

P3–P6 循环，直到 `PASS` 或达到 `max_rounds`。

```
          ┌──────────────┐
          │   你的问题    │
          └──────┬───────┘
                 ▼
   ┌──────────────────────────┐
   │ P1 提案  各出一份，互不可见 │  并行
   └──────────────┬───────────┘
                  ▼
   ┌──────────────────────────┐
   │ P2 选主案  Judge 打分      │
   └──────────────┬───────────┘
                  ▼
   ┌──────────────────────────┐
   │ P3 评审  只提问题，不重写   │  并行
   │ P4 辩论  只回应未消解分歧   │
   │ P5 裁定  PASS / 需修订     │  ← 循环
   │ P6 修订  产出完整下一版     │
   └──────────────┬───────────┘
                  ▼
   ┌──────────────────────────┐
   │ P7 终稿 + 纪要 + 分歧 + 成本 │
   └──────────────────────────┘
```

### 支持的厂商和模型

| 厂商 | 模型条目 | 厂商 | 模型条目 |
|---|:---:|---|:---:|
| 阿里通义 Qwen | 17 | xAI Grok | 7 |
| 智谱 GLM | 17 | Meta Model API | 5 |
| 硅基流动 SiliconFlow | 16 | Moonshot Kimi | 5 |
| Google Gemini | 11 | vLLM（自建） | 5 |
| Ollama（本地） | 11 | DeepSeek | 3 |
| OpenRouter | 10 | 本地 CLI 会话 | 3 |
| Anthropic | 9 | | |
| OpenAI | 8 | **合计** | **127 条 / 126 个唯一 id** |

`glm-5.2` 各有一条（智谱官方 API 与自建 vLLM），故条目数比唯一 id 多 1。
每个条目都对着官方文档核实过，见 [docs/PROVIDERS.md](docs/PROVIDERS.md)。

### 三条硬规则

1. **状态只认结构化字段。** 阶段流转完全由模型输出 JSON 里的枚举字段决定，不用正则从自然语言里猜。JSON 不合法时最多修复 2 次，仍失败则升级为 `NEED_USER_DECISION`——**绝不**静默降级为 `NEED_REVISION`（那会造成无意义的死循环）。
2. **一人只能出一套方案。** 禁止子方案、备选、分支。命中违规模式会自动重新提问，仍违规则拒绝该输出。不确定的地方只能写进 `assumptions` 或 `open_questions`。
3. **Judge 必须独立。** Judge 不参与提案、评审、辩论，也不改写方案正文；`judge.node_id` 与参与者重合时配置直接报错。

### 常用命令

```bash
council run "问题"                  # 新开一场
council resume <session_id>         # 从断点继续（已完成调用不重复计费）
council sessions                    # 列出可续跑的会话
council config-check                # 校验配置
```

### 状态

当前 **v1.3.0**：**463 项离线测试**（1 跳过，全部不含真实网络）、`ruff` 与 `mypy --strict`（34 个源文件）全绿。CI 见 [.github/workflows](.github/workflows)。

**里程碑 M1–M6（至 v1.0.0）**

- **M6（v1.0.0）** CI 与发布：GitHub Actions 跑 ruff/mypy、pytest 矩阵 3.11–3.13 × ubuntu/windows（全离线）、wheel/sdist 构建 + 全新安装冒烟；打 `v*` 标签自动出 Release。版本单源化（`council.__version__` → hatch dynamic）。
- **M5（v0.5.0）** Web UI（`council serve`）：FastAPI + WebSocket 实时事件流、暂停/恢复/终止与干预面板、附件走 M4 附件门、导出即下载；三套纯数据主题（可导入导出）+ zh/en 双语。
- **M4（v0.4.0）** 约束层：附件限制（默认 `.txt / .md / .json / .csv`，扩展名+内容双重校验、符号链接/伪装拒绝、超预算头尾截断）、单一方案强制门、可开关的净化管线与 `council export`。
- **M3（v0.3.0）** 韧性加固：`policy = pause` 真冻结、断网阻塞后健康探测自动续跑、独立熔断器、干预动作全覆盖。
- **M2（v0.2.0）** 真实厂商接入：OpenAI（含兼容端点）/ Anthropic / Gemini、`cli_session`（Codex / Claude Code / Antigravity，强制只读）与 `generic_http`；模型数据化在 `registry/*.toml`（核实见 [docs/PROVIDERS.md](docs/PROVIDERS.md)），密钥走 OS keyring 或 `.env`。
- **M1（v0.1.0）** 引擎骨架：八阶段状态机、事件溯源存储、错误六分类与断点续跑、干预协议、配置系统、注入防护。

**M6 之后的迭代**

- **v1.1.0** 模型注册表扩到 **14 家厂商 / 126 个模型**（127 个条目——`glm-5.2` 智谱官方与自建 vLLM 各一条；全部官方文档直证）；零代码厂商接入（`adapter` 字段声明协议）；Web 设置中心统一收口密钥 / 主题 / 语言 / 光效；会话级阵容覆盖；`GET /api/models` 模型目录端点。
- **v1.1.1** 思考档位全量注册表驱动（各厂商档位数量与命名都不同）；Claude 5 系自适应思考（`output_config.effort` 五档）；Kimi / Spark 档位官方核实后修正。
- **v1.2.0** 方正字体排版；CLI 诊断中心（`GET /api/diagnostics`）；推理模型 temperature 适配（`omit_temperature`，GPT-5.6/6 系与 Moonshot K 系传值即 400）；DeepSeek `json_object` 兜底。
- **v1.2.1 / v1.2.2** 单模型讨论可跑通；阵容只给部分节点选模型时，未被点名的演示档节点自动禁用；光效边缘残留修复；历史会话问题文本严格限长。

- **v1.3.0** 在线模型清单更新（默认关闭，见上）——含新发现模型的思考档位推断；注册表改为
  **三层优先级**：内置 > 用户手写 `registry/*.toml` > 自动发现 `registry/discovered/*.toml`，
  最后一层只补缺失 id、永不覆盖。前端：失败策略与导出格式收进设置、模型下拉新增「不选择」、
  入口页图案重做、历史会话支持按主题与日期检索。修复注册表按文件名分组导致同 provider 整体替换、
  `council serve --data-dir X` 注册表根目录错配、`pyproject.toml` 的 `extend-exclude` 含裸 `"web"`
  使 `council/web` 从未被 CI 检查过。

### 常见问题

**Q：这和开几个聊天窗口手动比，差别在哪？**
A：不是「同时问几个 AI」，而是**流程约束**：互不可见的独立提案、只看问题不重写的评审、
只回应未消解分歧的辩论、由独立 Judge 按判据裁定的收敛。你拿到的是终稿加分歧存档，不是一堆并列回答。

**Q：数据安全吗？会联网吗？**
A：本地运行，只在调用模型时出网。「除模型端点外不联网」是字面承诺——唯一例外是可选的模型清单
更新，默认关闭，且只访问各厂商自己的列模型接口。

**Q：能用本地模型吗？**
A：可以。注册表里已有 `ollama` 与 `vllm`，也能用 `generic_http` 接任意兼容端点。

**Q：适合生产环境吗？**
A：适合做关键决策辅助。463 项离线测试 + 全平台 CI 矩阵 + 语义化版本发布。它不做任何写操作，
也不会替你执行决定。

```bash
council key set OPENAI_API_KEY        # 密钥只进系统钥匙串
council run --config config.toml "你的问题"      # 可用 -f 附加 .txt/.md/.json/.csv
council serve                          # 浏览器开 UI：http://127.0.0.1:8765
```

详见 [CHANGELOG.md](CHANGELOG.md) 与 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## English

### What it is not

AI Council is a **council**, not an agent. It does two things: text conversation, and read-only ingestion of `.txt` / `.md` files you explicitly attach.

It will **not** create or modify files, run commands, or reach any network other than the model endpoints (the one exception is the **optional model-list updater**, off by default — see below). No model output can trigger a filesystem write — the app writes to exactly two places: its own data directory (config, sessions, logs) and the export path you pick by hand.

### Compared with other multi-model tools

| | AI Council | Juggling chat tabs | Prompt frameworks |
|---|:---:|:---:|:---:|
| Parallel independent proposals | ✅ | ❌ | ❌ |
| Structured review & debate | ✅ | ❌ | ❌ |
| Automatic convergence to a final plan | ✅ | ❌ | ❌ |
| Disagreements and decisions on record | ✅ | ❌ | ❌ |
| Local, offline-testable | ✅ | ❌ | ✅ |
| Web UI + CLI | ✅ | ❌ | ❌ |

The point is not asking several AIs at once — it is the **process constraints**: proposals
written blind to each other, reviews that question without rewriting, debate that only
addresses unresolved disagreement, and a convergence verdict from an independent judge.

### Requirements

- **Python** 3.11+, or the Windows no-install build (`ai-council.exe`, double-click to run, no Python needed)
- **Model keys**: eleven vendor presets or any custom variable name; written only to the local OS keyring
- **Network**: model APIs only. The model-list updater is off by default and, when on, talks only to each vendor's own listing endpoint

### Keeping the model list fresh (optional, off by default)

Models go stale; hand-editing TOML should not be the answer. Flip the switch under **Settings → Model list updates** in `council serve` and AI Council asks each vendor's own model-listing endpoint what it currently serves, then fills the gaps into your data directory.

- Three probes cover 13 of 14 providers (OpenAI-compatible `{base_url}/models`, Anthropic `/v1/models`, Google `v1beta/models`). A local CLI's subscription has no listing endpoint, so it is left alone.
- **Gap-filling only**: curated entries carry verified thinking protocols and always win; auto-discovered models get their own group in the picker and never masquerade as curated ones.
- **Thinking levels are claimed only on per-model evidence**: the vendor's own listing endpoint (Anthropic reports the levels outright) or the public catalogue matched by exact id. The knob and its levels always come from what the registry already declares — never invented, because a wrong protocol is a hard 400. Models with no signal stay selectable, they just offer no thinking control yet.
- No prices — a gateway's price for another vendor's model is not a number we write down.
- With the switch off, "no network other than model endpoints" holds literally.

### 🚀 30-second quick start

**Option A — Python installed (recommended)**

```bash
uv pip install ai-council          # or: pipx install ai-council
council run "How do I grow DAU from 10k to 100k in three months?"
```

No credentials needed for the first run: the shipped default config uses an in-process fake
adapter, so the whole eight-phase flow runs immediately.

**Option B — Windows, no Python**

Pick any of the three artifacts from the [Releases](https://github.com/xmwy0712/ai-council/releases) page:

| Artifact | Notes |
|---|---|
| `ai-council.exe` | single file, download & run |
| `ai-council-win64.zip` | unzip & run, starts faster |
| `AI-Council-Setup-<ver>.exe` | per-user installer with start-menu icons |

Unsigned, so allow it through SmartScreen if prompted.

**Option C — want to see the UI first**

```bash
council serve     # open http://127.0.0.1:8765
```

Keys can be entered in the UI under **Settings → Keys**: eleven presets (OpenAI / Anthropic /
Zhipu / Qwen / xAI / OpenRouter, …) or any custom variable name — written only to the OS
keyring, never echoed, and labelled with where it currently comes from (environment / `.env` /
keyring / unset).

`config.toml` lives in the app data directory (`%LOCALAPPDATA%\ai-council\` on Windows);
`council run --config ./config.toml "question"` points elsewhere, and `council config-check`
validates it.

### 💡 What people use it for

| Scenario | What you get |
|---|---|
| **Technical design selection** | Independent designs → reviews poke holes → debate converges, no single voice dominating |
| **Hard bug triage** | Front-end and back-end diagnose separately → cross-examined → converged root-cause hypothesis |
| **Copy and content polishing** | Independent drafts → mutual review → a single final piece |
| **Pre-decision red team** | Manufacture disagreement on purpose; risks and assumptions end up on record |

### The session

P0 context → P1 parallel independent proposals → P2 judge selects the main plan → P3 parallel independent reviews → P4 round-robin debate → P5 verdict → P6 revision → loop P3–P6 until `PASS` or `max_rounds` → P7 final report.

```
       ┌─────────────┐
       │ Your question│
       └──────┬──────┘
              ▼
 ┌──────────────────────────┐
 │ P1 Propose  blind to each other │  parallel
 └──────────────┬───────────┘
                ▼
 ┌──────────────────────────┐
 │ P2 Select   judge scores  │
 └──────────────┬───────────┘
                ▼
 ┌──────────────────────────┐
 │ P3 Review   question only │  parallel
 │ P4 Debate   unresolved gaps│
 │ P5 Verdict  PASS / revise  │  ← loop
 │ P6 Revise   whole plan again│
 └──────────────┬───────────┘
                ▼
 ┌──────────────────────────┐
 │ P7 Final + minutes + cost │
 └──────────────────────────┘
```

### Vendors and models

| Vendor | Entries | Vendor | Entries |
|---|:---:|---|:---:|
| Alibaba Qwen | 17 | xAI Grok | 7 |
| Zhipu GLM | 17 | Meta Model API | 5 |
| SiliconFlow | 16 | Moonshot Kimi | 5 |
| Google Gemini | 11 | vLLM (self-hosted) | 5 |
| Ollama (local) | 11 | DeepSeek | 3 |
| OpenRouter | 10 | Local CLI sessions | 3 |
| Anthropic | 9 | | |
| OpenAI | 8 | **Total** | **127 entries / 126 unique ids** |

`glm-5.2` is listed once for the Zhipu API and once for self-hosted vLLM, which is why the
entry count is one above the unique-id count. Every entry is proven against official docs —
see [docs/PROVIDERS.md](docs/PROVIDERS.md).

### Three hard rules

1. **State comes from structured fields only.** Phase transitions read enum fields from validated JSON. Invalid JSON gets at most two strict repair attempts, then escalates to `NEED_USER_DECISION` — never a silent fallback to `NEED_REVISION`.
2. **One plan per participant.** Sub-plans, alternatives and branches are rejected and re-prompted. Uncertainty belongs in `assumptions` or `open_questions`.
3. **The judge is independent.** It never proposes, reviews, debates or rewrites. A config that overlaps `judge.node_id` with a participant fails fast.

### Commands

```bash
council run "question"
council resume <session_id>
council sessions
council config-check
```

### Status

Currently **v1.3.0**: **463 offline tests** (1 skipped, none touching the network), `ruff` and `mypy --strict` (34 source files) green. CI lives in [`.github/workflows`](.github/workflows).

**Milestones M1–M6 (through v1.0.0)**

- **M6 (v1.0.0)** CI and release: GitHub Actions runs ruff/mypy, an offline pytest matrix 3.11–3.13 × ubuntu/windows, wheel/sdist builds plus a fresh-install smoke test; `v*` tags publish a Release. Version single-sourced (`council.__version__` → hatch dynamic).
- **M5 (v0.5.0)** the web UI (`council serve`): FastAPI + WebSocket live event stream, pause/resume/stop and an intervention panel, uploads through the M4 attachment gate, exports as plain downloads; three pure-data themes (import/export) and zh/en i18n.
- **M4 (v0.4.0)** the constraint layer: attachments (`.txt/.md/.json/.csv` by default, double content sniffing, symlink/disguise rejection, head+tail truncation), a single-plan gate, a switchable sanitising pipeline and `council export`.
- **M3 (v0.3.0)** resilience: `policy = pause` actually freezes, outage blocks heal via health probes, a standalone circuit breaker, full intervention coverage.
- **M2 (v0.2.0)** real vendors: OpenAI (plus compatible endpoints), Anthropic, Gemini, `cli_session` (Codex / Claude Code / Antigravity, read-only enforced) and `generic_http`. Models and thinking levels are pure data in `registry/*.toml` (verified in [docs/PROVIDERS.md](docs/PROVIDERS.md)); secrets live in the OS keyring or `.env`.
- **M1 (v0.1.0)** the engine: eight-phase state machine, event-sourced store, six-way error taxonomy with resume, the intervention protocol, config validation, injection defences.

**After M6**

- **v1.1.0** the registry grew to **14 vendors / 126 models** (127 entries — `glm-5.2` is listed once for the Zhipu API and once for self-hosted vLLM; every entry proven against official docs); zero-code vendor onboarding (the `adapter` field declares the protocol); a settings centre for keys, themes, language and the cursor effect; session-level roster overrides; `GET /api/models`.
- **v1.1.1** thinking levels fully registry-driven (each vendor has a different count and naming); Claude 5 adaptive thinking (`output_config.effort`, five levels); Kimi and Spark levels corrected against official docs.
- **v1.2.0** FangZheng typography; a CLI diagnostics centre (`GET /api/diagnostics`); reasoning-model temperature adaptation (`omit_temperature` — GPT-5.6/6 and Moonshot K return 400 if sent); a DeepSeek `json_object` fallback.
- **v1.2.1 / v1.2.2** single-model sessions work end to end; nodes left on the demo adapter are auto-disabled when only some members pick a real model; cursor-effect artefacts fixed; long questions truncated in the history list.

- **v1.3.0** online model-list updates (off by default, above) — including thinking-level
  inference for newly discovered models. The registry gained **three layers**: built-in >
  hand-written `registry/*.toml` > auto-discovered `registry/discovered/*.toml`, the last one
  gap-filling only and never overwriting. Front end: failure policy and export format moved
  into Settings, a persistent "no override" entry in the model picker, redrawn hub artwork,
  and topic/date search over past sessions. Fixed: two files naming the same provider replaced
  it wholesale; `council serve --data-dir X` read the registry from the default directory while
  writing the overlay into X; `pyproject.toml`'s `extend-exclude` contained a bare `"web"`, so
  `council/web` was never linted by CI.

### FAQ

**Q: How is this different from asking a few chatbots and comparing by hand?**
A: It is not "ask several AIs at once" — it is the **process constraints**: proposals written
blind to each other, reviews that question without rewriting, debate that only addresses
unresolved disagreement, and a verdict from an independent judge. You leave with a final plan
and a record of the disagreements, not a pile of parallel answers.

**Q: Is my data safe? Does it phone home?**
A: It runs locally and reaches the network only to call models. "No network other than model
endpoints" holds literally; the one exception is the optional model-list updater, off by
default, which talks only to each vendor's own listing endpoint.

**Q: Can I use local models?**
A: Yes. `ollama` and `vllm` ship in the registry, and `generic_http` accepts any compatible
endpoint.

**Q: Is it production-ready?**
A: It is suited to decision support. 463 offline tests, a full-platform CI matrix and semantic
versioning. It performs no write operations and does not act on your behalf.

```bash
council key set OPENAI_API_KEY        # secrets go to the OS keyring only
council run --config config.toml "your question"
council serve                          # browser UI at http://127.0.0.1:8765
```

See [CHANGELOG.md](CHANGELOG.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## License

MIT
