# AI Council

**让 2–3 个不同厂商的 AI 就同一个问题做结构化会谈，并自动收敛出一份可执行的最终方案。**

不再在多个 AI 网页之间来回复制粘贴：一次提问，多模型独立提案 → 独立评审 → 分歧辩论 → 独立裁定 → 修订重审，直到通过或达到轮次上限，产出终稿、会议纪要、分歧存档与成本统计。

[English](#english) | [中文](#中文)

---

## 中文

### 它不是什么

AI Council 是一个**智囊团**，不是一个 agent。它只做两件事：文本对话，以及只读地阅读你主动提供的 `.txt` / `.md` 文件。

它**不会**创建或修改文件、不会执行命令、不会访问除模型 API 之外的任何网络（**唯一的例外是可选开启的模型清单更新**，默认关闭，见下）。任何模型的输出都不能触发文件系统写入——应用自身只会写入两处：应用数据目录（配置、会话、日志），以及你主动点击导出时选择的目标路径。

### 模型清单保持新鲜（可选，默认关闭）

模型会过时，但你不该为此手改 TOML。在 `council serve` 的**设置 → 模型清单更新**里打开开关，AI Council 就会问各厂商自己的「列出模型」接口当前在役什么，把注册表里还没有的 id 自动补进数据目录。

- 三条探测路径覆盖 13/14 家（OpenAI 兼容 `{base_url}/models`、Anthropic `/v1/models`、Google `v1beta/models`）；本地 CLI 的订阅没有可查询接口，不更新。
- **只补缺，不覆盖**：内置注册表里的条目带已核实的思考档位，永远优先；自动发现的条目在网页端单独成组，不会与已策展的混在一起。
- **思考档位只在拿得到可核实信号时才声明**：厂商自己的列模型接口（Anthropic 会直接报出档位），或公开目录按完全一致的 id 命中的条目。参数名与档位一律取自注册表已声明的厂商声明，绝不发明——猜错协议是硬 400。拿不到信号的模型照样可选，只是暂不提供档位。
- 不写价格——网关对别家模型的标价不是本项目愿意落笔的数字。
- 关闭状态下「除模型端点外不联网」这句承诺字面成立。

### 快速开始

```bash
# 1. 安装
uv pip install ai-council          # 或 pipx install ai-council
# 不想装 Python？到 GitHub Releases 下载 ai-council.exe（Windows 单文件 ~20MB）
#   首次运行如被 SmartScreen 拦截：更多信息 → 仍要运行（本地工具，未签名）。

# 2. 零配置跑通第一场（内置 fake 适配器，不需要任何密钥）
council run "我要在三个月内把日活从 1 万做到 10 万，应该怎么做？"

# 3. 配置真实模型
council config-check               # 校验配置，报错可读
```

`config.toml` 默认位于应用数据目录（Windows `%LOCALAPPDATA%\ai-council\`）：
`council run --config ./config.toml "问题"` 可指定任意路径。

零命令行配置密钥：`council serve` 打开界面后点「设置 → 密钥」，填写 OpenAI /
Anthropic / 智谱 / 通义 / xAI / OpenRouter 等 11 家预设或任意自定义变量名即可——
只写入本机系统钥匙串、绝不回显，并标注它当前来自环境变量 / `.env` / 钥匙串 / 未配置。

Windows 免 Python 安装：单文件 `ai-council.exe`（下载即用）、`ai-council-win64.zip`
（解压即用、启动快）或 `AI-Council-Setup-<ver>.exe`（安装器，开始菜单图标），
均从 GitHub Releases 获取。

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

**未发布**（见 [CHANGELOG.md](CHANGELOG.md) 的 `[Unreleased]`）

- **在线模型清单更新**（默认关闭，见上）——含新发现模型的思考档位推断。
- 注册表改为**三层优先级**：内置 > 用户手写 `registry/*.toml` > 自动发现 `registry/discovered/*.toml`，最后一层只补缺失 id、永不覆盖。
- 修复：注册表按文件名分组导致两份文件声明同一 provider 时整体替换；`council serve --data-dir X` 下注册表根目录错配（覆盖层写进 X 却从默认目录读）；`pyproject.toml` 的 `extend-exclude` 含裸 `"web"`，使 `council/web` 从未被 CI 的 `ruff check` 检查过。

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

### Keeping the model list fresh (optional, off by default)

Models go stale; hand-editing TOML should not be the answer. Flip the switch under **Settings → Model list updates** in `council serve` and AI Council asks each vendor's own model-listing endpoint what it currently serves, then fills the gaps into your data directory.

- Three probes cover 13 of 14 providers (OpenAI-compatible `{base_url}/models`, Anthropic `/v1/models`, Google `v1beta/models`). A local CLI's subscription has no listing endpoint, so it is left alone.
- **Gap-filling only**: curated entries carry verified thinking protocols and always win; auto-discovered models get their own group in the picker and never masquerade as curated ones.
- **Thinking levels are claimed only on per-model evidence**: the vendor's own listing endpoint (Anthropic reports the levels outright) or the public catalogue matched by exact id. The knob and its levels always come from what the registry already declares — never invented, because a wrong protocol is a hard 400. Models with no signal stay selectable, they just offer no thinking control yet.
- No prices — a gateway's price for another vendor's model is not a number we write down.
- With the switch off, "no network other than model endpoints" holds literally.

### Quick start

```bash
uv pip install ai-council          # or: pipx install ai-council
# No Python installed? Grab ai-council.exe from the GitHub Release
# (single-file Windows build, ~20MB; unsigned, so allow it through SmartScreen).
council run "How do I grow DAU from 10k to 100k in three months?"
```

The first run needs no credentials: the shipped default config uses a deterministic in-process fake adapter, so the whole eight-phase flow is exercisable immediately.

Zero-CLI key setup: run `council serve`, open **Settings → Keys** and paste a key for any of the eleven presets (OpenAI / Anthropic / Zhipu / Qwen / xAI / OpenRouter, …) or any custom variable name — it is written only to the local OS keyring, never echoed, and labelled with where it currently comes from (environment / `.env` / keyring / unset).

No Python installed? Grab one of the three Windows artifacts from the GitHub Release: `ai-council.exe` (single file, download & run), `ai-council-win64.zip` (unzip & run, starts fast) or `AI-Council-Setup-<ver>.exe` (per-user installer with start-menu icons).

### The session

P0 context → P1 parallel independent proposals → P2 judge selects the main plan → P3 parallel independent reviews → P4 round-robin debate → P5 verdict → P6 revision → loop P3–P6 until `PASS` or `max_rounds` → P7 final report.

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

**Unreleased** (see the `[Unreleased]` section of [CHANGELOG.md](CHANGELOG.md))

- **Online model-list updates** (off by default, above) — including thinking-level inference for newly discovered models.
- The registry gained **three layers**: built-in > hand-written `registry/*.toml` > auto-discovered `registry/discovered/*.toml`, the last one gap-filling only and never overwriting.
- Fixed: two files naming the same provider replaced it wholesale; `council serve --data-dir X` read the registry from the default directory while writing the overlay into X; `pyproject.toml`'s `extend-exclude` contained a bare `"web"`, so `council/web` was never linted by CI.

```bash
council key set OPENAI_API_KEY        # secrets go to the OS keyring only
council run --config config.toml "your question"
council serve                          # browser UI at http://127.0.0.1:8765
```

See [CHANGELOG.md](CHANGELOG.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## License

MIT
