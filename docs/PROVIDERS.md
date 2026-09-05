# PROVIDERS.md — 厂商参数核实表

**规则：本文件是「先查文档再写代码」的落点。** 任何模型 ID、参数名、取值范围、CLI 参数，必须能追溯到下面的官方文档链接与核实日期；查不到的进 TODO，**绝不允许凭记忆填写**。编辑注册表 `council/registry/*.toml` 时，必须同步更新本表。

最近一次整体核实：**2026-09-02**。

## OpenAI（`openai_api`，含任意 OpenAI 兼容端点）

来源：<https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create>（核实 2026-09-02）

| 事实 | 取值 | 核实状态 |
|---|---|---|
| 端点 | `POST {base_url}/chat/completions`，`Authorization: Bearer <key>` | ✅ 官方 |
| 输出上限 | `max_completion_tokens`（`max_tokens` 已弃用且与 o 系不兼容） | ✅ 官方 |
| 温度 | `temperature`，0–2 | ✅ 官方 |
| 思考等级 | `reasoning_effort`：`none` / `minimal` / `low` / `medium` / `high` / `xhigh`（仅推理模型；gpt-5.2-chat-latest 会受限） | ✅ 官方 |
| JSON 输出 | `response_format: {"type": "json_object"}`（还有 `json_schema`） | ✅ 官方 |
| 流式 | `stream: true` SSE；增量在 `choices[0].delta.content`；`stream_options: {"include_usage": true}` 返回 `usage` | ✅ 官方 |
| 用量 | `usage.prompt_tokens / completion_tokens / total_tokens` | ✅ 官方 |
| 错误 | 401 → 认证；429 → 限流；`message.refusal` → 内容拒答 | ✅ 官方 |

模型（注册表收录项）：`gpt-5.2`、`gpt-5.1`、`gpt-5.2-chat-latest`。`gpt-5.2` 上下文 400K / 输出 128K（400K/128K 由 llm-stats 与官方帮助中心模型清单交叉印证，2026-09-02）。

TODO：官方定价页逐项价格（现仅社区/聚合来源，未收录，先留空）；`gpt-5.2-pro` 可用性确认。

## Anthropic（`anthropic_api`）

来源：<https://console.anthropic.com/docs/en/build-with-claude/extended-thinking>、Messages API 参考（核实 2026-09-02）

| 事实 | 取值 | 核实状态 |
|---|---|---|
| 端点 | `POST {base_url}/v1/messages`，头 `x-api-key`、`anthropic-version: 2023-06-01` | ✅ 官方 |
| 输出上限 | `max_tokens`（**必填**） | ✅ 官方 |
| 系统提示 | 顶层 `system` 字段（不进 `messages`）；`messages` 仅 user/assistant | ✅ 官方 |
| 温度 | `temperature` | ✅ 官方 |
| 思考 | `thinking: {"type": "enabled", "budget_tokens": N}`；N ≥ 1024 且 < `max_tokens`；Claude 4.5 及更早为唯一模式；Opus 4.6 已弃用（仍接受），4.7+ 拒绝 | ✅ 官方 |
| 流式 | SSE：`content_block_delta` 中 `delta.type == "text_delta"` → `delta.text`；`thinking_delta` 为思考增量；`message_delta.usage` 给输出用量 | ✅ 官方 |
| 用量 | `usage.input_tokens / output_tokens` | ✅ 官方 |
| 拒答 | `stop_reason == "refusal"` → 内容拒答 | ✅ 官方 |
| 错误 | 401 认证；429 限流；529 过载（按限流重试） | ✅ 官方 |

模型：`claude-sonnet-4-6`、`claude-sonnet-4-5`、`claude-opus-4-6`、`claude-opus-4-5`、`claude-haiku-4-5`。输出上限：Opus 4.6 至 128K，更早模型 64K；上下文 200K（官方思考文档表述）。

TODO：`thinking: {"type": "adaptive", "effort": ...}` 的 effort 取值范围（4.6+ 推荐方式，枚举值本次未取到，接入前先补查）。

## Google Gemini（`google_api`）

来源：<https://developers.generativeai.google/api/generate-content>、<https://clouddocs-dot-devsite-v2-prod.appspot.com/gemini-enterprise-agent-platform/models/guides/gemini-3-5-flash>（核实 2026-09-02）

| 事实 | 取值 | 核实状态 |
|---|---|---|
| 端点 | `POST {base_url}/v1beta/models/{model}:streamGenerateContent?alt=sse`（非流式 `:generateContent`） | ✅ 官方 |
| 认证 | 头 `x-goog-api-key: <key>` | ✅ 官方 |
| 请求 | `contents: [{role: "user"|"model", parts: [{text}]}]`、`systemInstruction: {parts: [{text}]}`、`generationConfig: {...}` | ✅ 官方 |
| 输出 | `generationConfig.maxOutputTokens`、`temperature`；JSON：`responseMimeType: "application/json"` | ✅ 官方 |
| 思考 | Gemini 3+：`thinkingConfig: {"thinkingLevel": "MINIMAL" \| "LOW" \| "MEDIUM" \| "HIGH"}`；Gemini 2.5：`thinkingConfig: {"thinkingBudget": <int>}`。**两代参数混用会报错** | ✅ 官方 |
| 流式 | 每个 `data:` 行是一个 GenerateContentResponse；文本在 `candidates[0].content.parts[*].text`（`thought: true` 的部分为思考，需跳过） | ✅ 官方 |
| 用量 | `usageMetadata.promptTokenCount / candidatesTokenCount / thoughtsTokenCount / totalTokenCount` | ✅ 官方 |
| 拒答 | `promptFeedback.blockReason` / 空 candidates → 内容拒答 | ✅ 官方 |

模型：`gemini-3.5-flash`（GA，1,048,576 上下文 / 65,536 输出，thinking 等级 MINIMAL/LOW/MEDIUM/HIGH，默认 MEDIUM）、`gemini-3.1-pro-preview`（Preview，LOW/MEDIUM/HIGH）、`gemini-2.5-pro`、`gemini-2.5-flash`（后两者 thinkingBudget）。

TODO：官方定价逐项（同上）；`gemini-3.6-flash` 是否已开放 API（2026-07 社区文章提及，官方 API 页未确认）。

## Codex CLI（`cli_session` profile: `codex`）

来源：OpenAI Codex CLI 文档（mintlify.wiki/openai/codex/cli/exec，核实 2026-09-02）

| 事实 | 取值 |
|---|---|
| 非交互 | `codex exec [OPTIONS] [PROMPT]`；`PROMPT` 为 `-` 时从 stdin 读 |
| 只读 | `--sandbox read-only`（exec 默认即只读，仍显式传入） |
| 其余我们用的参数 | `--ephemeral`（不留会话）、`-C <dir>`（工作目录）、`--skip-git-repo-check`、`-m <model>`、`-o <file>`（`--output-last-message`）、`--json`（JSONL 事件流） |
| 输出 | 最终答复在 **stdout**；进度在 stderr；退出码 0/1 |
| 登录态 | 复用本地已授权（OAuth）；key 也可用 `CODEX_API_KEY`（仅 exec） |

## Claude Code（`cli_session` profile: `claude`）

来源：<https://code.claude.com/docs/ja/headless>、claudecn.com Headless 模式（核实 2026-09-02）

| 事实 | 取值 |
|---|---|
| 非交互 | `claude -p "<prompt>"` |
| 只读/禁工具 | `--disallowedTools "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch"`；或 `--allowedTools "Read,Glob,Grep"`；`--permission-mode plan` 为只读分析模式 |
| 输出 | `--output-format json` → `{"type":"result","result":..., "usage":..., "cost_usd":..., "session_id":...}` |
| 注意 | **不要**加 `--bare`：它会跳过 OAuth/keychain 登录态，导致必须有 `ANTHROPIC_API_KEY` |

TODO：官方是否有直接控制思考预算的 CLI 参数（本次未取到，先不加）。

## Google Antigravity（`cli_session` profile: `agy`）

来源：<https://antigravity.google/docs/cli/headless>、<https://antigravity.google/docs/cli/modes>、Google Codelabs（核实 2026-09-02）

| 事实 | 取值 |
|---|---|
| 非交互 | `agy -p "<prompt>"`（`--print`/`--prompt` 同义）；答复 stdout，诊断 stderr |
| 只读 | `--mode=plan`：分析模式，写操作需批准且 headless 下默认拒绝 |
| 输出 | `--output-format json` → `{status, response, usage: {input_tokens, output_tokens, thinking_tokens, total_tokens}, duration_seconds, ...}` |
| 模型 | `--model "<slug>"`（如 `Gemini 3.5 Flash (High)`）；`agy models` 可列 |
| 思考 | `--effort`（启动时选推理档，与模型 slug 组合） |
| 登录态 | 复用本机 OS keyring 已登录态；未登录 headless 直接报错退出 |

收录 slug（Codelabs 实测列表）：`Gemini 3.5 Flash (Low|Medium|High)`、`Gemini 3.1 Pro (Low|High)`、`Claude Sonnet 4.6 (Thinking)`、`Claude Opus 4.6 (Thinking)`、`GPT-OSS 120B (Medium)`。

TODO：`strict` 权限模式（零信任只读）的 CLI 开关确认（文档仅见于权限页描述）。

## 统一错误分类映射（各适配器共用）

| HTTP / 现象 | ErrorKind | 可重试 |
|---|---|---|
| 401 / 403 / 未登录 | `auth` | 否 |
| 429 / 529（Anthropic 过载） | `rate_limit` | 是 |
| 408 / 客户端超时 / 空闲无输出 | `timeout` | 是 |
| 连接错误 / 502 / 503 | `network` | 是 |
| stop_reason=refusal / blockReason | `content_refusal` | 否 |
| 400 / 404（模型不存在等） | `contract`（配置问题，需人工介入） | 否 |
| 其他 | `unknown` | 否 |
