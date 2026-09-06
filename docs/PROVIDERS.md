# PROVIDERS.md — 厂商参数核实表

**规则：本文件是「先查文档再写代码」的落点。** 任何模型 ID、参数名、取值范围、CLI 参数，必须能追溯到下面的官方文档链接与核实日期；查不到的进 TODO，**绝不允许凭记忆填写**。编辑注册表 `council/registry/*.toml` 时，必须同步更新本表。

最近一次整体核实：**2026-09-05**（全量刷新：新增 10 家厂商；OpenAI/Anthropic/Google/CLI 模型清单同步到当日在役世代）。

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

模型（注册表收录项，核实 2026-09-06）：`gpt-6-astra`（GPT-6 家族 2026-09-03 发布的**唯一**成员，1.05M 上下文 / 128K 输出，$10/$50）、`gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`、`gpt-5.3-codex`、`gpt-5.2`、`gpt-5.1`、`gpt-5-mini`。**不存在裸 `gpt-6`**；`gpt-5.2-chat-latest` 官方未证实仍开放，二手价格页虽列但不收录。

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

模型（核实 2026-09-05）：在役 `claude-fable-5-1`、`claude-opus-5`、`claude-sonnet-5`、`claude-haiku-5`；4.x `claude-sonnet-4-6`、`claude-sonnet-4-5`、`claude-opus-4-6`、`claude-opus-4-5`、`claude-haiku-4-5`（逐步退役中）。输出上限：Opus 5 / Opus 4.6 至 128K，其余 64K；上下文 200K。

**Claude 5 系与 budget_tokens 不兼容**：5 系改用 Adaptive thinking（嵌套 `thinking:{type:"adaptive"}`），而本适配器按 4.x 协议硬编码发送 `budget_tokens` 块——5 系模型若开启思考会返回 400。注册表中 5 系一律 `thinking = false` 且 `thinking_style = "none"`，思考交给服务端默认自适应处理。

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

模型（注册表收录项，核实 2026-09-06）：`gemini-3.8-flash`（2026-09-02 发布，1M/64K，thinkingLevel low/medium/high 默认 medium）、`gemini-3.7-flash`、`gemini-3.6-flash`、`gemini-3.5-flash`、`gemini-3.5-flash-lite`、`gemini-3-flash-preview`、`gemini-3.1-flash-lite`、`gemini-3.1-pro-preview`（Preview）、`gemini-2.5-pro`、`gemini-2.5-flash`、`gemini-2.5-flash-lite`。**世代事实：Flash 线已到 3.8，Pro 线旗舰仍是 3.1 Pro——不存在 `gemini-3.7-pro` / `gemini-3.6-pro`**；`Gemini 3.8 Flash Cyber` 为受限访问未收录。3+ 用 `thinkingLevel` 枚举，2.5 用 `thinkingBudget` 整数。

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

来源：<https://codelabs.developers.google.cn/antigravity-cli-hands-on>（`agy models` 输出直证，核实 2026-09-06）

收录（11 条）：Gemini 3.8 Flash（High/Medium/Low）、Gemini 3.7 Flash（High/Medium/Low）、Gemini 3.6 Flash（High/Medium/Low）、Gemini 3.1 Pro（High/Low）。3.8 Flash 于 2026-09-02 进入 Antigravity 并成为托管代理默认模型；3.5 Flash 与 Claude 4.6 条目已被官方清单移除，不再收录。

## 
## DeepSeek（`deepseek`，adapter = openai_api）

来源：<https://api-docs.deepseek.com>（核实 2026-09-06）

| 事实 | 取值 | 核实状态 |
|---|---|---|
| 端点 | `POST https://api.deepseek.com/v1/chat/completions`，OpenAI 兼容（另有 Anthropic 格式 `/anthropic`） | ✅ 官方 |
| 密钥 | `DEEPSEEK_API_KEY`，Bearer | ✅ 官方 |
| 模型 id | **仅三个**：`deepseek-v4-flash`（版本 DeepSeek-V4-Flash-0731）、`deepseek-v4-pro`（版本 DeepSeek-V4-Pro-0813）、`deepseek-v4-flash-vision-exp`（实验性图像输入） | ✅ 官方 |
| 上下文 | 1M / 最大输出 384K（三模型一致） | ✅ 官方 |
| 思考 | 嵌套 `thinking:{type:"enabled"}` 可开关 + 标量 `reasoning_effort`：`low` / `high`(默认) / `max`；旋钮取标量，medium 向上映射为 high、high 映射为 max | ✅ 官方 |

旧 `deepseek-chat` / `deepseek-reasoner` 及带日期的快照 id 均已下线，不收录。

## 
## Moonshot Kimi（`moonshot`，adapter = openai_api）

来源：<https://platform.moonshot.cn/docs/pricing/chat>（核实 2026-09-05）

思考开关为嵌套对象 `thinking:{type}` —— 旋钮只支持标量，故不做档位翻译（`thinking_style = "none"`）。模型：`kimi-k3`、`kimi-k3-thinking`、`kimi-k2.7`、`kimi-k2.6`、`kimi-k2.5`、`kimi-k2-turbo`、`kimi-k1.6`。K2 全系已下线（官方定价页已移除）。

## 智谱 GLM（`zhipu`，adapter = openai_api）

来源：<https://docs.bigmodel.cn/cn/guide/develop/http/usage>（核实 2026-09-05）

思考开关为嵌套对象 `thinking:{type}` —— 同上不做档位翻译。模型（17 个，核实 2026-09-06）：`glm-5.3`（2026-08-14，后训练强化，思考不可关闭、档位 low/high/max）、`glm-5.3-flash`（2026-08-26，320B-A18B 原生多模态，1M 上下文 / 128K 输出，MIT 开源）、`glm-5.2`、`glm-5.1`、`glm-5`、`glm-5-air`、`glm-5-flash`、`glm-4.7`、`glm-4.6`、`glm-4.6v`、`glm-4.5`、`glm-4.5-air`、`glm-4.5-flash`、`glm-4.5-x`、`glm-4.5-airx`、`glm-4.5v`、`glm-4-long`。

## 阿里通义 Qwen（`dashscope`，adapter = openai_api）

来源：<https://help.aliyun.com/zh/model-studio/models>、<https://help.aliyun.com/zh/model-studio/deep-thinking>（核实 2026-09-05）

| 事实 | 取值 | 核实状态 |
|---|---|---|
| 端点 | `https://dashscope.aliyuncs.com/compatible-mode/v1`（兼容模式） | ✅ 官方 |
| 密钥 | `DASHSCOPE_API_KEY` | ✅ 官方 |
| 思考 | 标量 `thinking_budget`（整数 token），且必须伴生 `enable_thinking: true` —— 用 `[provider.thinking.extra]` 声明 | ✅ 官方 |

模型（17 个）：`qwen3.8-max/plus/turbo/omni`、`qwen3.7-max/plus/coder-plus/vl-max`、`qwen3.6-max/plus`、`qwen3.5-max/plus`、`qwen3-max/plus/turbo`、`qwq-plus`、`qwen-deep-research`。

## xAI Grok（`xai`，adapter = openai_api）

来源：<https://docs.x.ai/docs/models>、<https://docs.x.ai/docs/api-reference>（核实 2026-09-05）

思考：`reasoning_effort`（low/medium/high）。模型：`grok-4.6`、`grok-4.6-fast`、`grok-4.5`、`grok-4.5-fast`、`grok-4.5-mini`、`grok-image-2`、`grok-multi-agent`。`grok-4` 已下线。

## OpenRouter（`openrouter`，adapter = openai_api）

来源：<https://openrouter.ai/models>（核实 2026-09-05）

网关思考旋钮为嵌套对象 `reasoning:{effort}` —— 不做档位翻译。**只收录在模型页逐条核对过的 slug**（`openai/gpt-6-astra`、`anthropic/claude-opus-5`、`anthropic/claude-sonnet-5`、`google/gemini-3.8-flash`（官方 slug 页直证）、`deepseek/deepseek-v4`、`moonshotai/kimi-k3`、`z-ai/glm-5.2`、`qwen/qwen3.8-max`、`x-ai/grok-4.6`、`meta-llama/llama-5-maverick`）；二手来源拼出的 slug 一律不收录。

## 硅基流动 SiliconFlow（`siliconflow`，adapter = openai_api）

来源：<https://docs.siliconflow.cn/cn/userguide/introduction>、<https://siliconflow.cn/models>（核实 2026-09-05）

思考：标量 `thinking_budget`（部分模型需伴生 `enable_thinking`）。收录 Qwen / DeepSeek / GLM / Kimi 开源权重与 Llama、BGE-M3 共 16 条（含 `Pro/` 前缀加速条目与 embedding 一条）。

## Ollama（`ollama`，adapter = openai_api，本地）

来源：<https://docs.ollama.com/api/openai>、<https://ollama.com/library>（核实 2026-09-05）

| 事实 | 取值 | 核实状态 |
|---|---|---|
| 端点 | `http://localhost:11434/v1`（OpenAI 兼容） | ✅ 官方 |
| 密钥 | **不需要**：`secret_required = false`，适配器缺密钥时不发 Authorization | ✅ 官方 |
| 思考 | 原生 `think` 参数（`true/false/low/medium/high`）→ `enum_effort` 直接映射 | ✅ 官方 |

模型：`qwen3.8:32b/14b`、`qwen3:32b/14b`、`deepseek-v4`、`gpt-oss:120b/20b`、`glm-5.2`、`llama5`、`mistral-large`、`phi5`。

## vLLM（`vllm`，adapter = openai_api，自建）

来源：<https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html>（核实 2026-09-05）

自建服务通常无密钥（`secret_required = false`）。思考开关走 `chat_template_kwargs:{enable_thinking}`（嵌套）—— 不做档位翻译。收录 `qwen3.8-32b`、`qwen3-32b`、`deepseek-v4`、`glm-5.2`、`llama-5-70b`；自建环境以实际部署的模型为准。

## Meta Model API（`meta`，adapter = openai_api）

来源：<https://dev.meta.ai>（核实 2026-09-05）

**端点迁移**：开发者端点已从 `api.llama.com` 迁移到 `https://api.meta.ai/v1`（本机 WebFetch 直读确认），密钥变量由 `LLAMA_API_KEY` 改为 `MODEL_API_KEY`。模型：`muse-spark-1.3`、`muse-spark-1.2`、`muse-spark-1.1`、`muse-spark-1.3-contributor`、`muse-spark-1.0`。

## 思考档位对照（核实 2026-09-06）

**档位是数据不是代码**：每家、甚至每个模型可用的档位都不同，一律由
`council/registry/*.toml` 声明（可逐模型覆盖），网页端下拉框与后端校验
都按注册表动态生成，不再假定 low/medium/high 三档。

| 厂商 / 模型 | 参数 | 可用档位 | 备注 |
|---|---|---|---|
| `gpt-6-astra` | `reasoning_effort` | low / medium / high / **xhigh** / **max** | 发 `none` 或 `minimal` 返回 400 |
| `gpt-5.6-sol/terra/luna` | `reasoning_effort` | none / low / medium / high / xhigh | `max` 仅 Responses API 可用 |
| `gpt-5.3-codex` / `gpt-5.2` / `gpt-5.1` / `gpt-5-mini` | `reasoning_effort` | none / low / medium / high | 不支持 xhigh / max；`minimal` 仅初代 GPT-5 |
| Claude 5 系（Opus/Sonnet/Haiku 5、Fable 5.1） | `output_config.effort` + `thinking:{type:"adaptive"}` | low / medium / high / xhigh / max | 4.7+ 收到 `budget_tokens` 直接 400 |
| Claude 4.x | `thinking.budget_tokens` | low(2048) / medium(8192) / high(32768) | 官方约束：≥1024 且 < max_tokens |
| Gemini 3 Flash（含 3.8/3.7/3.6/3.5/Flash-Lite/3.1-Flash-Lite） | `thinkingLevel` | **MINIMAL** / LOW / MEDIUM / HIGH | Pro 系列无 MINIMAL |
| Gemini 3.1 Pro | `thinkingLevel` | LOW / MEDIUM / HIGH | 默认 HIGH |
| Gemini 2.5 系 | `thinkingBudget` | 整数 token（低 512/1024 ~ 高 8192/16384） | 与 thinkingLevel 不可混用 |
| DeepSeek V4 系 | `reasoning_effort` | **low / high / max**（无 medium） | 默认 high；另有嵌套 `thinking` 可开关 |
| GLM-5.3 / 5.3-Flash | `reasoning_effort` | **low / high / max** | 默认 max，思考不可关闭 |
| GLM-5.2 | `reasoning_effort` | none / minimal / low / medium / high / xhigh / max | medium/low→high、xhigh→max、minimal/none 放弃思考 |
| GLM-5.1 / 5 / 4.x | 嵌套 `thinking` | 不可调（档位只读） | 官方未提供标量档位 |
| Grok 4.5 / 4.6 | `reasoning_effort` | low / medium / high | 默认 high，推理不可关闭 |
| 通义 Qwen（DashScope） | `thinking_budget` + `enable_thinking` | 整数 token 档位 | 伴生字段经 `thinking.extra` 发送 |
| Ollama | `think` | low / medium / high（及 true/false） | 原生参数直通 |

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
