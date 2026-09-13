# Architecture

AI Council 是一个**会谈引擎**：它编排 2–3 个不同厂商的模型，就同一个问题做结构化会谈并收敛出一份可执行方案。它不是 agent——不写文件、不执行命令、不做除模型调用之外的任何网络访问。**唯一的例外是可选开启的模型清单更新**（默认关闭，见「模型清单更新」一节）。

```
┌────────────────────────────────────────────────────────────────┐
│  surfaces：CLI（council run / resume）· Web UI（M5）            │
│      只通过 InterventionHandler 协议和事件流与引擎对话           │
├────────────────────────────────────────────────────────────────┤
│  api（M5：FastAPI + WebSocket，只做转发，不含业务）              │
├────────────────────────────────────────────────────────────────┤
│  core（引擎，零 UI 依赖，可在纯 CLI 下完整运行）                 │
│    orchestrator ── 状态机 ── 事件溯源(store/state)              │
│        │ prompts(模板+注入防护)  schema(输出契约)  config        │
├────────────────────────────────────────────────────────────────┤
│  adapters（openai_api / anthropic_api / google_api /            │
│            cli_session / generic_http · M2）                    │
│  registry（模型与思考等级的 TOML 数据表 · M2）                   │
├────────────────────────────────────────────────────────────────┤
│  SQLite(WAL) 追加式事件日志   themes   i18n                     │
└────────────────────────────────────────────────────────────────┘
```

依赖方向是单向的：`adapters → core`，`api → core`。`core` 不知道 UI 的存在；`council.core` 不 import `council.api` 或任何前端代码，这是可测试性的根。

## 会议状态机

```
P0 CONTEXT ──► P1 PROPOSAL ──► P2 SELECT ──┐
 (载入问题/附件   (参与者并行、      (Judge 打分选   │
  token 预算)     互不可见)         主案或人工选)   ▼
        ┌──────────────────────────── P3 REVIEW ◄──────────────┐
        │                              (除作者外并行独立评审)      │
        │                                │                       │
        │                                ▼                       │
        │                        P4 DEBATE                      │
        │                        (round-robin，每轮只回应         │
        │                         尚未消解的分歧)                 │
        │                                │                       │
        │                                ▼                       │
        │                            P5 VERDICT ─── PASS ───► P7 FINAL
        │                        (Judge 裁定三选一)               │
        │                                │                       │
        │              NEED_REVISION / NEED_USER_DECISION        │
        │                                ▼                       │
        └──────────────────────────── P6 REVISION ──────────────┘
                                   (作者产出完整 V(n+1))
```

三条非线边全部由**结构化枚举字段**驱动（`VerdictOut.status`、修订计数），没有任何正则从自然语言里猜状态：

| 当前阶段 | 决定因素 | 下一阶段 |
|---|---|---|
| P5 VERDICT | `status == "PASS"` | P7 FINAL |
| P5 VERDICT | `status ∈ {NEED_REVISION, NEED_USER_DECISION}` | P6 REVISION |
| P6 REVISION | `round >= max_rounds` | P7 FINAL |
| P6 REVISION | 其余情况 | P3 REVIEW（round + 1） |
| 其余 | — | 线性下一阶段 |

另外两个终止路径：

- **收敛停滞**：连续两轮 `must_fix + disputes` 无改善 → 状态置为 `stalled`，询问用户处置后直接进入 P7。
- **冻结**（`SessionPaused`）：低于 `min_quorum`、阶段被阻塞、或失败策略为 `pause` 时，引擎落盘断点并返回，由 `council resume` 续跑。

## 输出契约与修复

每个阶段对应一个 Pydantic 模型（`council/core/schema.py`）。模型输出必须是一个 JSON 对象；引擎只剥掉「容器」（```json 围栏或最外层 `{...}`），**从不**在正文中搜索状态词。

- 契约失败 → 最多 2 次严格修复提示（`templates/*/repair.md`）→ 仍失败则按失败策略处理（升级，绝不静默降级）。
- `VerdictOut` 在 `status == NEED_USER_DECISION` 时强制要求 `question_for_user`——被阻塞的会议必须说清只有人类能定什么。
- Judge 的 `selected_node_id` 不在候选中 → 最多纠正 2 次 → 升级为人工选择。

## 事件溯源

会话状态 = 事件日志的回放结果。事件表：

| 事件 | 载荷要点 | 说明 |
|---|---|---|
| `SessionCreated` | question、config_fingerprint、participants、judge | 会话起点 |
| `PhaseStarted` | phase、round、version | 幂等去重（`entered` 集合） |
| `CallIssued` | key、node_id、phase、round、attempt、model、request_hash | 发出前落盘 |
| `CallChunk` | key、index、text | 流式增量（`persist_chunks=false` 时不落盘） |
| `CallCompleted` | key、raw_text、parsed、usage、latency_ms、repaired | 完成后落盘 |
| `CallFailed` | key、kind、message、retryable | 错误分类与是否可重试 |
| `PhaseCompleted` | phase、round、version、summary | summary 携带该阶段已校验的结构化结果 |
| `Convergence` | round、must_fix、should_fix、disputes、improved | 收敛曲线数据点 |
| `UserDecision` | phase、round、decision、note | 人工选择/裁决同样落盘 |
| `NodeStateChanged` | node_id、state（degraded/healthy） | 熔断与恢复 |
| `SessionStatusChanged` | status、reason | running/paused/awaiting_user/stalled/completed |
| `ConfigChanged` | before/after fingerprint、diff | 运行中改配置或恢复时采纳新配置 |

幂等键：`(session_id, phase, round, node_id, attempt)`；辩论轮额外带 `tag=d{n}` 以区分同轮多次发言。`resume` 回放日志后只重发处于 in-flight（有 `CallIssued` 无 `CallCompleted`）或失败的调用，已完成结果一律复用，不重复计费。

## 容错模型

```
单次调用:  分类重试(指数退避+抖动，仅 rate_limit/timeout/network)
              └─ 不可重试(auth/content_refusal)或重试耗尽 ──► 失败解析
节点级:    CircuitBreaker 连续失败达 circuit_threshold ──► 节点熔断
              │   熔断即暂停派发，并落盘 NodeStateChanged(degraded)
              └─ 健康探测恢复 ──► NodeStateChanged(healthy) + 复位熔断
策略:      continue(剔除+记缺席) | pause(冻结落盘) | ask_user(五动作干预)
底线:      可用参与者 < min_quorum ──► 强制冻结，绝不允许单人出终稿
```

失败策略可在运行中切换（`engine.set_failure_policy`），下一次失败立即生效。

**两类阻塞，两种出路**（都由 `PhaseBlocked.retryable` 区分）：

- *传输型阻塞*（断网、限流、超时导致全员失败）：`_await_recovery` 在一个
  冷却周期后做一轮健康探测，重新接纳被剔除的节点（剔除是本轮决定，不是
  判决），就地重试该阶段——断网恢复后自动续跑，不重来。
- *非传输型阻塞*（无评审者、等人工）：冻结并落盘；CLI 进程退出交给
  `council resume`，服务端等待 UI 的 `resume()`。

熔断器（`core/breaker.py`）是纯内存的连败计数；「该节点已降级」的持久事实
只以事件形式存在，resume 后无需信任新进程的计数器。

冻结语义（M3 修复）：`_pause_and_wait` 先清除暂停门再等待——此前门保持
打开，`policy = pause` 实际上从不暂停。

## 提示词与注入防护

- 模板在 `council/core/templates/<lang>/*.md`，`{{placeholder}}` 渲染；未知占位符直接抛错（拒绝静默漏填）。
- 一切外部内容（用户问题、附件、模型的上一次输出）都包进 `<data_zone id="..." trust="data">`，且 `neutralise()` 会转义内容中出现的闭合标签，防止内容自己「越狱」出数据区。
- 系统提示声明：数据区内任何指令不得改变角色、流程或输出契约。

## 模型清单更新（可选联网）

模型会过时，而网页端用户不会去手改 TOML。开启后，引擎会问各厂商**自己的**「列出模型」接口当前在役什么，把注册表里没有的 id 补进一份自动生成的覆盖层。

三条探测路径覆盖 13/14 家，因为注册表的 `adapter` 字段本来就声明了协议：

| 探测 | 覆盖 |
|---|---|
| `GET {base_url}/models`（OpenAI 兼容，Bearer） | openai_api · dashscope · deepseek · meta · moonshot · openrouter · siliconflow · vllm · zhipu · ollama |
| `GET {base_url}/v1/models`（+ `anthropic-version`、`x-api-key`） | anthropic_api |
| `GET {base_url}/v1beta/models`（密钥走 `x-goog-api-key` 头） | google_api |
| 无接口 | cli_session（订阅能用什么由 CLI 决定，猜就是编） |

**三层优先级，且自动发现永远垫底**（`loader.py`）：

```
内置 council/registry/*.toml      ← 已策展、已核实
  ▼ 同名文件合并、同 id 覆盖
用户 <数据目录>/registry/*.toml    ← 手工新增/覆盖
  ▼ 只补缺，绝不覆盖
自动 <数据目录>/registry/discovered/*.toml
```

这条顺序是结构性的，不是「写入时过滤一下」：`_merge_missing` 只接受尚不存在的 id。若让自动发现能覆盖，一份几个月前写下的文件会静默抹掉注册表后来才核实清楚的思考协议——而错误的思考参数是硬 400。

刻意不做的事：

- **不写价格。** 网关对别家模型标的价格不是本项愿意落笔的数字（`docs/PROVIDERS.md` 同一原则）。
- **不改内置条目、不删任何东西。** 厂商本次没列出的已策展 id 只在报告里作为 `absent` 提示——本项目的策展清单常常比厂商的查询接口更宽（涵盖预览版与别名），删除必须是人的决定。

### 新发现模型的思考档位怎么定

新模型必须能带出档位，否则等于半个残废；但猜错协议是硬 400。所以规则是**两个问题、一个允许的拒绝**：

1. **这个模型支持思考吗？** 只有**每模型**信号算数——厂商自己的列模型接口，或公开目录按完全一致的 id 命中的条目。厂商级的声明对没见过的模型什么都不说明；把 `reasoning_effort` 发给不接受它的模型，就是那个经典的 400。
2. **用哪个旋钮、哪些档位？** 旋钮只取自注册表已声明的东西：厂商自己的 `[provider.thinking]` 块，或该厂商已策展兄弟模型一致同意的声明（`style`/`param`/`extra` 是 API 的事实，不是某个模型的事实，所以借用是复用而非发明）。档位在旋钮是 effort 枚举时取厂商报出的档位名（限本项目认可的词表），否则取声明里的档位表。
3. **凑不出档位就拒绝声明。** 返回 `None` 永远安全：模型照样可选，只是暂时不提供思考控制，等有人核实后再手写一份用户 TOML。

各家的信号来源：

| 厂商 | 每模型信号 | 旋钮来源 |
|---|---|---|
| `anthropic_api` | `capabilities.effort` 的各级真值 → 直接得到 `enum_effort`/`effort` 与确切档位；仅有 `thinking.supported` 时退回 provider 的 `budget_tokens` | 厂商自己，最权威 |
| `google_api` | `Model.thinking` 布尔 | **同代际**已策展兄弟（Gemini 3+ 用 `thinkingLevel`，2.5 用 `thinkingBudget`，混用即 400）；没有同代际兄弟则不声明 |
| OpenAI 兼容各家 | 公开目录 `supported_parameters` 含 `reasoning` / `reasoning_effort` | provider 块，或兄弟一致的声明 |
| `cli_session` | 无 | 不声明 |

宁可少声明也不猜：`gpt-live-1` 这类刚发布、公开目录尚未收录的模型会拿到 `thinking = false`，这是设计而非缺陷。

威胁模型与边界：

- 默认关闭。关闭时「除模型端点外不联网」这句承诺**字面成立**，离线与审计场景不受影响。
- 模型输出到不了这里：只有服务端启动路径与用户显式动作会调用。
- 响应一律当数据：体积上限 8 MiB、形状校验、id 限字符集与长度、写入 TOML 前转义控制字符。畸形或恶意响应只会退化成「这次什么都没发现」，不会污染注册表，也不会中断会话。
- 失败按厂商隔离：一家挂了不影响其他家，也绝不把服务端带崩。

并发与节流：探测是阻塞 HTTP，跑在工作线程；同一时刻只允许一轮（手动点「立即检查」撞上启动检查时**等它跑完并复用结果**，而不是报错或重复探测）；`min_interval_h` 让长期开着的服务不会每次开页面都全网探一遍。

## 关键取舍记录

- **SQLite 直连（`sqlite3` + `asyncio.to_thread`）而不是 aiosqlite**：引擎并行派发节点时多协程并发写库，aiosqlite 的常驻线程与事件循环生命周期耦合，在进程退出时产生悬挂回调；标准库驱动 + 单把 `asyncio.Lock` 语义更简单、少一个依赖。
- **事件即事实，`sessions.status` 列只是缓存**：列由 `SessionStatusChanged` 同步维护，供列表页免回放查询；真相永远以日志为准。
- **冻结即返回，而不是原地死等**：无 UI 的 CLI 进程等不到 `resume()`，因此引擎在 `resume_timeout_s`（默认 0）后抛 `SessionPaused` 落盘退出；服务器进程传大值并通过 API 恢复。同一条规则同时覆盖了断网恢复与人工等待。
- **自动发现的覆盖层放在单独子目录并排在最后**：不这样做就得靠「写入时把已存在的 id 过滤掉」来避免遮蔽内置数据——而那意味着任何一份陈旧文件（哪怕再也不被重写）都能长期压住策展数据。把优先级做进加载顺序，风险就变成结构上不可能。
- **注册表按声明的 provider id 分组，而不是按文件名**：文件名不是身份。此前两份文件声明同一个 provider 时，后一份会整体替换前一份——一个杂散 `llm-extras.toml` 就能让某家厂商的全部模型消失。自动发现会往这个目录写文件，所以先把这个洞补上。
- **发现的失败只进报告，不进 `last_error`**：某家厂商 401 或离线是常态，不该让整个功能看起来坏掉。`last_error` 只保留整轮跑不起来的情况。
- **思考档位由「每模型信号 + 已声明旋钮」两段拼成，而不是照抄公开目录**：公开目录的 `supported_efforts` 说的是网关自己的参数，不是厂商原生 API 的参数（Anthropic 原生是 `effort`，Google 是 `thinkingLevel`/`thinkingBudget`）。照抄会把网关词汇发到原生端点。分开之后，档位名可以来自目录，参数名永远来自注册表。
- **注册表根目录可作用域化（`registry_at`）**：此前缓存是一个全局槽位，指向进程默认数据目录，于是 `--data-dir X` 下配置与会话搬到了 X、注册表没搬——覆盖层写进 X 却从默认目录读。改成按根目录键控的缓存 + 生命周期内作用域，服务端与 CLI 都指向自己的数据目录。

## Web UI（M5）

- **进程内同一套引擎**：`council serve` 启动 FastAPI；会话引擎跑在服务器事件循环，
  HTTP（建会话 / 动作 / 导出）与 WebSocket（事件直播 + 干预问答）都只是它的薄转发层。
- **先订阅、后回放**：新订阅者先收到 `since=seq` 之后的历史事件，直播按 seq 去重；
  `CALL_CHUNK` 不落库，只在直播中流动。
- **干预即票据**：`ask_user` / 选主案 / 人工决定被转成可回答的 ask，UI 经 WS 回答；
  无人值守（或最后一位监听者离开）时用保守兜底自动回答，会话不会挂死。
- **附件门提前**：上传内容先落数据目录，由附件校验器在 POST 内同步拒绝，避免留下
  没有事件行的幽灵会话；按会话子目录存放以保留原始文件名。
- **导出即下载**：渲染净化后的 Markdown 流式返回，浏览器下载——服务端不写用户路径。
- **主题与文案是纯数据**：`static/themes/*.json`（CSS 变量全集）与
  `static/i18n/{zh,en}.json`；两者都有离线测试守护（变量完整性、对比度、双语 key 对称）。
- **SQLite 连接加线程锁**：`close()` 会等待在飞语句完成再关闭连接（取消任务不会中断
  `to_thread` 里的 SQL）。

## 里程碑完成情况

M1–M6 全部完成（v1.0.0）。CI 与发布工作流见 `.github/workflows/`：质量门禁
（ruff / mypy）、离线测试矩阵（3.11–3.13 × ubuntu/windows）与构建冒烟（wheel/sdist
内容断言 + 全新安装后跑通一场 fake 会谈）。
