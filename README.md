# AI Council

**让 2–5 个不同厂商的 AI 就同一个问题做结构化会谈，并自动收敛出一份可执行的最终方案。**

不再在多个 AI 网页之间来回复制粘贴：一次提问，多模型独立提案 → 独立评审 → 分歧辩论 → 独立裁定 → 修订重审，直到通过或达到轮次上限，产出终稿、会议纪要、分歧存档与成本统计。

[English](#english) | [中文](#中文)

---

## 中文

### 它不是什么

AI Council 是一个**智囊团**，不是一个 agent。它只做两件事：文本对话，以及只读地阅读你主动提供的 `.txt` / `.md` 文件。

它**不会**创建或修改文件、不会执行命令、不会访问除模型 API 之外的任何网络。任何模型的输出都不能触发文件系统写入——应用自身只会写入两处：应用数据目录（配置、会话、日志），以及你主动点击导出时选择的目标路径。

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

零命令行配置密钥：`council serve` 打开界面后点右上角「密钥」，填写 OpenAI /
Anthropic / Gemini 或自定义变量名即可——只写入本机系统钥匙串、绝不回显。

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

**M6 已完成（v1.0.0）**：CI（GitHub Actions：ruff/mypy、pytest 矩阵 3.11–3.13 × ubuntu/windows 全离线、wheel/sdist 构建 + 全新安装冒烟）与打包发布工作流（打 `v*` 标签自动出 Release）；版本单源化（`council.__version__` → hatch dynamic），wheel 内静态资源有内容断言守护。200 项离线测试、`ruff` 与 `mypy --strict` 全绿。

**M5 已完成（v0.5.0）**：Web UI（`council serve`）——FastAPI + WebSocket 实时事件流、暂停/恢复/终止与干预面板、附件上传走 M4 附件门、导出即下载；浅色/深色/高对比三套纯数据主题（可导入导出）+ zh/en 双语（key 一致性离线测试守护）。197 项离线测试、`ruff` 与 `mypy --strict` 全绿。

**M4 已完成（v0.4.0）**：约束层——附件限制（默认 `.txt / .md / .json / .csv`，按你的要求放开了机器可读文件；扩展名+内容双重校验、符号链接/伪装拒绝、超预算头部+尾部截断）、单一方案强制门（并列方案自动纠正，拒不改则按失败策略处理）、可开关的净化管线（golden 测试、代码块全程保护）与 `council export` 净化导出。185 项离线测试、`ruff` 与 `mypy --strict` 全绿。

**M3 已完成（v0.3.0）**：韧性加固——`policy = pause` 真冻结（修复）、断网阻塞后健康探测自动续跑、独立熔断器、干预动作全覆盖。

**M2 已完成（v0.2.0）**：真实厂商接入——OpenAI（含兼容端点）/ Anthropic / Gemini、`cli_session`（Codex / Claude Code / Antigravity，强制只读）与 `generic_http`；模型数据化在 `registry/*.toml`（核实见 [docs/PROVIDERS.md](docs/PROVIDERS.md)），密钥走 OS keyring 或 `.env`。

```bash
council key set OPENAI_API_KEY        # 密钥只进系统钥匙串
council run --config config.toml "你的问题"      # 可用 -f 附加 .txt/.md/.json/.csv
council serve                          # 浏览器开 UI：http://127.0.0.1:8765
```

M1–M6 全部完成（v1.0.0）。CI 见 [.github/workflows](.github/workflows)。详见 [CHANGELOG.md](CHANGELOG.md) 与 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## English

### What it is not

AI Council is a **council**, not an agent. It does two things: text conversation, and read-only ingestion of `.txt` / `.md` files you explicitly attach.

It will **not** create or modify files, run commands, or reach any network other than the model endpoints. No model output can trigger a filesystem write — the app writes to exactly two places: its own data directory (config, sessions, logs) and the export path you pick by hand.

### Quick start

```bash
uv pip install ai-council          # or: pipx install ai-council
# No Python installed? Grab ai-council.exe from the GitHub Release
# (single-file Windows build, ~20MB; unsigned, so allow it through SmartScreen).
council run "How do I grow DAU from 10k to 100k in three months?"
```

The first run needs no credentials: the shipped default config uses a deterministic in-process fake adapter, so the whole eight-phase flow is exercisable immediately.

Zero-CLI key setup: run `council serve`, open the **Keys** panel in the top bar and paste your OpenAI / Anthropic / Gemini (or any custom) key — it is written only to the local OS keyring and never echoed.

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

**M6 shipped (v1.0.0)**: CI (GitHub Actions: ruff/mypy, a fully offline pytest matrix 3.11–3.13 × ubuntu/windows, wheel/sdist builds plus a fresh-install smoke test) and a release workflow (`v*` tags publish a GitHub Release). The version is single-sourced (`council.__version__` → hatch dynamic) and the wheel's bundled web assets are asserted in CI. 200 offline tests, `ruff` and `mypy --strict` green.

**M5 shipped (v0.5.0)**: a web UI (`council serve`) — FastAPI + WebSocket live event stream, pause/resume/stop and an intervention panel, uploads that pass the M4 attachment gate, exports as plain downloads; three pure-data themes (light/dark/high-contrast, import/export supported) and zh/en i18n (key symmetry guarded by offline tests). 197 offline tests, `ruff` and `mypy --strict` green.

**M4 shipped (v0.4.0)**: the constraint layer — attachments (`.txt/.md/.json/.csv` by default, machine-readable files now allowed as you asked; double content sniffing, symlink/disguise rejection, head+tail truncation over budget), a single-plan gate (auto-corrects titled alternatives, persistent offenders hit the failure policy), a switchable sanitising pipeline (golden-tested, code always protected) and `council export` for cleaned Markdown. 185 offline tests, `ruff` and `mypy --strict` green.

**M3 shipped (v0.3.0)**: resilience hardening — `policy = pause` actually freezes (fixed), outage blocks heal via health probes and retry in place, a standalone circuit breaker, and full coverage for WAIT / SWITCH_ADAPTER interventions and mid-flight policy switches. 148 offline tests, `ruff` and `mypy --strict` green.

**M2 shipped (v0.2.0)**: real vendors on top of the M1 engine — OpenAI (plus any compatible endpoint), Anthropic and Gemini APIs, `cli_session` wrapping locally-authenticated Codex / Claude Code / Antigravity (read-only enforced), and `generic_http` templates. Models and thinking levels are pure data in `registry/*.toml` (every parameter verified in [docs/PROVIDERS.md](docs/PROVIDERS.md)); secrets live in the OS keyring or `.env`. 134 offline tests, `ruff` and `mypy --strict` green.

```bash
council key set OPENAI_API_KEY        # secrets go to the OS keyring only
council run --config config.toml "your question"
```

All milestones shipped as of v1.0.0. CI lives in [`.github/workflows`](.github/workflows).

---

## License

MIT
