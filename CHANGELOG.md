# Changelog

本项目的所有显著变更都记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.3.0] - 2026-09-12

在线模型清单更新（含新发现模型的思考档位推断）；以及做这个功能时暴露出来的注册表缺陷与
`CHANGELOG` 本身的一处错位。

### Added

- **在线模型清单更新（默认关闭）**：网页「设置 → 模型清单更新」一键开关。开启后向各
  厂商**自己的**「列出模型」接口查询当前在役模型，把注册表里还没有的 id 写入
  `<数据目录>/registry/discovered/<provider>.toml`。三条探测路径覆盖 13/14 家
  （OpenAI 兼容 `GET {base_url}/models`、Anthropic `/v1/models`、Google
  `v1beta/models`），`cli_session` 的订阅没有可查询接口，不更新。
  启动时按 `min_interval_h`（默认 24h）节流检查；新增 `GET /api/updates`、
  `POST /api/updates/enabled`、`POST /api/updates/refresh` 三个端点。
  界面开关写数据目录的 `updates.json` 并优先于 `config.toml`——只用网页的用户没有
  config.toml 可改，正是这个功能要服务的人。
- **非对话模型过滤**：列模型接口会把 embedding / whisper / tts / 图像 / 审核 / 实时，
  以及补全时代的遗留模型（`davinci-002`、`gpt-3.5-turbo-instruct`）与带日期的快照
  （`gpt-4-0613`、`claude-3-5-sonnet-20241022`）一并倒出来。按内置规则剔除，并可用
  `exclude_patterns` 追加；保留 `-preview` / `-vl` / `:free` 等可用名。结果按创建时间
  倒序写入，新模型排在选取列表最前。
- **自动发现条目单独成组**：`/api/models` 与 `ModelSpec` 新增 `discovered` 标记，
  阵容编辑器把它们放在「厂商（自动发现）」分组里，不与已策展条目混淆。
- **新发现模型带出思考档位**：新模型不给档位等于半个残废，但猜错协议是硬 400，所以规则是
  「**每模型信号**决定是否声明，**注册表已声明的旋钮**决定怎么发」。
  - Anthropic `/v1/models` 的 `capabilities.effort` 是厂商直接报出档位真值 → 得到
    `enum_effort` / `param = "effort"` 与确切档位；仅有 `thinking.supported` 时退回继承
    provider 的 `budget_tokens`。
  - Google 只给 `Model.thinking` 布尔值 → 旋钮取自**同代际**已策展兄弟（Gemini 3+ 用
    `thinkingLevel`，2.5 用 `thinkingBudget`，混用即 400）；没有同代际兄弟则不声明。
  - OpenAI 兼容各家：公开目录 `supported_parameters` 含 `reasoning` / `reasoning_effort`
    作为信号，旋钮取 provider 块或兄弟一致的声明。
  - 档位名只接受 `none`/`minimal`/`low`/`medium`/`high`/`xhigh`/`max`，表外名字一律丢弃。
  - **凑不出非空档位表就拒绝声明**（`thinking = false`），模型仍可选。刚发布、公开目录
    尚未收录的模型（实测如 OpenAI `gpt-live-1`）会走这条路径——这是设计而非缺陷。
- `ThinkingPlan` 与 `CatalogEntry` 两个数据类型；报告与 `/api/models` 均带出档位信息。

### Changed

- **注册表变为三层，优先级写进加载顺序**：内置 `council/registry/*.toml` > 用户手写
  `<数据目录>/registry/*.toml` > 自动发现 `<数据目录>/registry/discovered/*.toml`，
  最后者**只补缺失 id**，永不覆盖。这是本次最重要的设计取舍：若让自动发现能覆盖，
  一份几个月前写下的文件会静默抹掉注册表后来才核实清楚的思考协议，而错误的思考参数
  是硬 400。做成结构性顺序而非「写入时过滤一遍」，意味着陈旧文件在结构上就不可能遮蔽
  策展数据。新增 `Registry.load(include_discovered=False)` 供发现流程取「已策展基线」。
- **新增 `[updates]` 配置段**：`enabled`（默认 false）· `check_on_start` ·
  `min_interval_h` · `catalog_url` · `timeout_s` · `exclude_patterns`。
- **`fingerprint()` 排除 `[updates]`**：刷新模型清单不改变会话如何审议，纳入它会让每个
  暂停中的会话在升级后都误报「配置已变」。
- **`ruff` / `mypy` 覆盖面修正**：`council/registry` 纳入 `mypy --strict`（33 个源文件）。
  `pyproject.toml` 的 `extend-exclude` 原先含裸 `"web"`，而该选项匹配任意层级的同名
  目录——`council/web`（FastAPI 应用所在包）因此在 CI 的 `ruff check` 中从未被检查过。
  改排除 `council/web/static`。
- **注册表根目录可作用域化**：`get_registry()` 此前缓存于单一全局槽位、固定指向进程默认
  数据目录。`--data-dir X` 下配置与会话搬到了 X、注册表没搬，于是自动发现的覆盖层写进 X
  却从默认目录读——模型出现在报告里但永远选不中。现改为按根目录键控的缓存，并新增
  `registry_at()` / `use_registry_root()`：服务端在生命周期内指向自己的数据目录（退出即
  还原，避免测试间互相污染），CLI 每个命令解析出 `data` 后同样指向。
- 静态资源缓存参数升至 `1.2.2.2`。

### Fixed

- **每次页面加载把 `/api/sessions` 请求了两次**：`boot()` 显式调 `refreshList()`，紧接着
  `showView("new")` 内部又调一次。现在由 `showView` 统一负责刷新，`boot()` 只负责进视图；
  用静态断言钉住这个分工（注释里提到该调用不算数，只查真实语句）。
- **标签页图标缺失**：`index.html` 未声明任何 icon，浏览器每次加载都去猜 `/favicon.ico`
  并得到 404。新增 `favicon.svg`（蓝底三枚圆点 = 参与者上限 3）并在 `<head>` 声明，
  `tests/test_web_assets.py` 顺带校验引用的资源确实存在。

- **两份文件声明同一 provider 时会整体替换**：注册表此前按**文件名**分组，因此一份文件
  名不同的 TOML（例如 `llm-extras.toml` 里写 `id = "openai_api"`）会整体取代该厂商，
  内置与已发现的全部模型一并消失，而不是叠加。现按声明的 provider id 分组；无
  `[provider] id` 的文件仍会给出可读报错，不会静默消失。
- **手动刷新撞上启动检查返回 409**：现在等待在跑的那一轮结束并复用其写下的结果，
  既不报错也不重复探测——点「立即检查」是合理动作，不该被惩罚。
- **`council serve --data-dir X` 的注册表错配**：见上「注册表根目录可作用域化」。
- `sorted(package.iterdir())` 的 `Traversable` 不可排序类型错误。
- `council/web` 中 5 处 `try/except/pass` 改为 `contextlib.suppress`、
  `asyncio.TimeoutError` 改为内建 `TimeoutError`，并移除 4 条无效的
  `# noqa: BLE001`（BLE001 并未启用，这些指示从未生效）。
- `CHANGELOG.md` 中错位的 `[Unreleased]` 段落（夹在 1.1.0 与 1.0.0 之间，内容实为已发布
  的密钥面板与 Windows 打包说明）并入 `1.1.0` 后删除；补齐各版本对比链接。

### Security

- 探测一律把响应当数据：体积上限 8 MiB、形状校验、id 限字符集与长度、写入 TOML 前
  转义控制字符。畸形或恶意响应只退化为「本次无发现」，不会污染注册表也不会中断会话。
- Google 密钥走 `x-goog-api-key` 头而非 `?key=` 查询参数，避免密钥进入日志与代理记录。
- 失败信息只出现密钥**变量名**，绝不回显值——`tests/test_updates_api.py` 用金丝雀值
  扫描 API 响应、状态文件与全部生成文件来钉住这条。
- 不写价格：网关对别家模型的标价不是本项目愿意落笔的数字（与 `docs/PROVIDERS.md`
  同一原则）。
- 不变量不变：密钥只进系统钥匙串 / `.env`；模型输出仍不能触发任何文件写入，也到不了
  发现流程。

### 测试

- 新增 84 项全离线用例，合计 **463 项**（1 跳过）：
  - `tests/test_discovery.py`（54 项）：三条探测路径与分页、过滤矩阵（含保留项）、
    恶意 id 与显示名注入、只补缺写入、覆盖层无法遮蔽策展数据、手写文件仍可覆盖内置、
    厂商失败隔离、公开目录歧义时拒绝猜测、状态文件容错。
  - `tests/test_discovery_thinking.py`（15 项）：Anthropic effort 真值 /
    `budget_tokens` 降级 / 无 capabilities 时拒绝；Google 同代际取旋钮、无同代际兄弟
    拒绝；公开目录信号与档位、表外档位名丢弃；无信号拒绝、provider 无声明且无兄弟拒绝、
    兄弟声明分歧拒绝；`thinking` 与 `extra` 的 TOML 往返。
  - `tests/test_updates_api.py`（13 项）：开关优先级与持久化、未开启时拒绝刷新、
    覆盖层落盘**且能被同一数据目录读回**、根目录作用域进出还原、启动检查与
    `min_interval_h` 节流、密钥金丝雀。
  - `tests/test_registry.py`（+2 项）：同 provider 多文件合并、无 id 文件报错。
- `ruff`、`ruff format --check`、`mypy --strict`（33 个源文件）全绿。

## [1.2.2] - 2026-09-08

补丁版：右侧光效色块错乱重做修复、历史会话超长问题限长。

### Fixed

- **鼠标移到页面右侧不再色块错乱**（重做）：撤销 v1.2.1 限制恒星活动范围的
  做法，改为行星轨道靠近画布边缘时自动收缩（下限 0.32）——恒星（鼠标）可
  自由移动到任何位置，行星永不越出画布被硬裁；行星靠近卡片时在 26px 缓冲带
  内平滑淡出，配合卡片遮挡擦除不再产生半圆硬切残片（此前鼠标移到卡片/视口
  右缘时，缝隙里露出被切开的彩色弧段）。
- **历史会话超长问题严格限长**：问题文本截到 32 字符并以 `…` 代替（悬停
  显示完整原文）；修复 flex 子项 `min-width:auto` 撑开导致省略号失效的问题，
  长提示词不再溢出历史会话区域。
- 项目版本升至 `1.2.2`。

## [1.2.1] - 2026-09-08

补丁版：光效边缘残留与阵容部分选择修复。

### Fixed

- **鼠标移到页面最右缘不再留下彩色痕迹**：恒星锚点原先可以贴到视口边缘，
  行星绕它公转（最大轨道 82px）时右半圈被画布裁掉，只剩贴边一竖条被切开的
  彩色弧段。现在恒星锚点限制在安全区（距边缘 ≥100px），行星轨道永不越出画布。
- **roster 只给部分节点选模型时，未选节点不再混入讨论**：只要本场对任一参与者
  显式选了真实模型，未被点名且仍停留在 fake 演示档的节点会被自动禁用——它们的
  0ms 模板假方案不再混入提案与「选主案」弹窗候选；active_nodes 与 quorum 随
  实际参与者收缩。
- **单模型讨论可跑通**：只选一个模型时（其余节点被禁用），P3 评审阶段在
  无第二评审者时记录空评审直接进入裁定，不再把会话冻在「等待恢复」；
  全局参与者下限由 2 放宽到 1（仅影响会话级覆盖后的阵容，静态配置仍建议 ≥2）。
- 项目版本升至 `1.2.1`。

## [1.2.0] - 2026-09-08

第八个里程碑：方正字体排版、CLI 诊断中心、推理模型 temperature 适配与交互打磨。

### Added

- **方正字体排版系统**：全站改用方正仿宋（正文）+ 方正大标宋（标题）@font-face 自托管字体，主模块加宽至 1360px，字号/行高重排。
- **CLI 诊断中心**：`GET /api/diagnostics` + 设置面板诊断区——codex / claude / agy 三大本地 CLI 的安装、版本、鉴权状态一键检查；无 auth 子命令的工具直接提示用户在终端 `agy login`，不再猜测。
- **运行参数面板**：设置面板新增「运行参数」——卡死判定超时与重试次数可自定义，随会话提交写入 effective config。
- **推理模型 temperature 适配**：注册表 `omit_temperature` 声明（模型级优先于厂商级）——GPT-5.6/6 系与 Moonshot K 系思考模型不接受自定义 temperature（传值 400），适配器按声明省略该参数。
- **DeepSeek json_object 兜底**：JSON 模式要求 prompt 含 "json"，自动追加中文提示行避免触发 400。
- **会话级 overrides 修复**：选择真实模型时节点适配器一并切换到所属厂商（修复 fake 演示节点拿真实模型名演戏——密钥与思考永不生效的假象）；cli_session 模型选中也联动 settings.cli。

### Changed

- **agy headless 调参**：去掉 `--mode plan`（计划流不产出最终答案导致空回），改 `--add-dir cwd` + `--print-timeout 180s`；`--print-timeout` 必须带单位。
- **失败处理**：失败弹窗过滤 wait 按钮、隐藏取消按钮；选主案弹窗浮动左下角不再遮挡方案；CLI 非交互（管道/CI）遇 EOF 自动取首个候选，Web 端 30min 无值守自动取首个候选（fake Judge 视同未配置转人工审核）。
- **移除「等待」按钮**（与会话暂停重复、易误操作卡住）；CallChunk 改单行流式状态不再刷屏。
- **密钥缺失 409 明确提示**：指明缺失的密钥变量（而非 500）。
- **模型注册表修正**：gpt-6-astra 上下文 1050000；Kimi K 系文档链接更新；注释澄清 gpt-5.2-chat-latest 未官方证实不收录。
- 项目版本升至 `1.2.0`。

### Fixed

- 徽标只保留文字（去掉图形）；只读思考框显示空白；UI 细节打磨。

## [1.1.1] - 2026-09-06

补丁版：思考档位全面注册表驱动（Claude 5 自适应思考、Kimi/Spark 档位核实）、光效与交互打磨。

### Added

- **Claude 5 系自适应思考**：`output_config.effort` + `thinking:{type:"adaptive"}`——4.7+ 收到 `budget_tokens` 会直接 400，5 系改用 effort 五档（low/medium/high/xhigh/max）控制思考深度；4.x 仍走 token 预算。
- **思考档位全量注册表驱动**：网页端阵容编辑器下拉框与后端校验均按各模型声明动态生成，不再假定 low/medium/high 三档（GPT-6 五档、GLM-5.3 三档、DeepSeek 三档、Gemini Flash 含 MINIMAL 等）；未知档位返回可选列表。
- **行星遮挡可交互区域**：光效行星/拖尾/恒星绕入卡片与顶栏矩形即被擦除，不再浮在内容之上；恒星跟随光标常显。
- **静态资源缓存击穿参数**：`?v=` 随版本递增，代码更新后浏览器立即拿到新版。

### Changed

- **Kimi 官方档位核实**（2026-09-06）：kimi-k3 为三档 `reasoning_effort`（low/high/max），剔除不存在的 k3-thinking/k2-turbo/k1.6，k2.7 修正为 k2.7-code；K2 标记为遗留档位。
- **Meta Muse Spark 档位核实**：Spark 1.1/1.2/1.3 补五档 reasoning_effort（minimal→xhigh）；聚合平台统一旋钮说明入档。
- 毛玻璃卡片/顶栏透明度提升（68%→55%、72%→60%），光晕居中压层使模糊有物可依；只读思考框不再空白。
- `docs/PROVIDERS.md` 增补「思考档位对照」表（各厂商参数/可用档位/约束，核实 2026-09-06）。
- 项目版本升至 `1.1.1`。

### Fixed

- 阵容编辑器 `levelsFor` 暂时性死区导致整体渲染失败——函数先定义后使用。
- 帮助气泡改用 JS 委托控制显隐，规避原生弹出层卡住 `:hover` 残留问题。

## [1.1.0] - 2026-09-06

第七个里程碑：多厂商注册表扩展、Web 设置中心与可视化打磨。

### Added

- **模型注册表扩展到 14 家厂商 / 139 个模型**（全部官方文档直证，核实日期
  2026-09-05/06）：新增 DeepSeek、Moonshot Kimi、智谱 GLM（含 GLM-5.3 /
  GLM-5.3-Flash）、阿里通义 Qwen、xAI Grok、OpenRouter、硅基流动、Ollama、
  vLLM、Meta Model API（端点已迁移 `api.meta.ai/v1`，密钥变量 `MODEL_API_KEY`）；
  OpenAI / Anthropic / Google / CLI 清单同步到当日在役世代（GPT-6 家族仅
  `gpt-6-astra`；Gemini Flash 线到 3.8、Pro 线旗舰仍为 3.1 Pro）。
- **零代码厂商接入底座**：注册表 `adapter` 字段声明协议实现——新增一家
  OpenAI 兼容厂商只需一份 TOML；`secret_required = false` 支持本地端点
  （Ollama / vLLM）缺密钥不发 Authorization；`thinking.extra` 伴生字段
  （如 DashScope `enable_thinking`）随请求自动合并。
- **Web 设置中心**：顶栏收敛为「新会谈 + 设置」——鼠标光效开关、主题
  选择与导入导出、中英文切换、11 家厂商密钥注入全部并入统一面板。
- **会话级阵容覆盖**：网页端可逐节点临时改模型与思考档位（深拷贝配置 +
  注册表严格校验，未知节点 / 注册表外模型 / 不可翻译档位一律 422）；
  续跑自动继承，全局配置不被污染。
- **会话删除**：`DELETE /api/sessions/{id}`（运行中先终止再清库，404 幂等）+
  历史列表删除按钮（确认弹窗，中英文案）。
- **恒星与行星鼠标光效**（可在设置中开关）：淡蓝色恒星光晕 + 五颗淡色行星
  引力环绕（弹簧物理、惯性甩尾与回摆），移动时留下连续浅蓝光拖尾（0.5s
  渐隐）；`prefers-reduced-motion` 与高对比模式自动降级。
- **问号帮助提示**：失败策略、raw 导出、独立 Judge 三处「?」悬停说明
  （JS 控制显隐，规避原生弹出层卡住 `:hover` 的残留问题）。
- **全量模型干跑校验**：`tools/check_models.py` + 131 项参数化回归——注册表
  内每个模型真实实例化适配器并构造调用负载，校验模型 ID、思考旋钮、伴生
  字段、Anthropic 预算钳制与元数据完备性；新模型条目数据不全会直接测试失败。
- **CLI 订阅默认模型收敛**：Codex / Claude Code / Antigravity 统一为一条
  「订阅默认模型」，不向 CLI 传 `--model`，模型切换由用户在 CLI 内完成。
- **静态模型目录端点** `GET /api/models`；`GET /api/meta` 升级为携带每节点
  厂商 / 模型显示名 / 思考支持与档位，judge 完整对象。
- **视觉强化**：环境极光光晕与颗粒层、渐变品牌徽标、玻璃顶栏与半透明毛玻璃
  卡片、主按钮渐变辉光、聚焦光环、阵容编辑器交互件圆角与悬浮感；
  所有颜色走主题变量 `color-mix()` 派生，三套主题自动适配。
- **静态资产 `Cache-Control: no-cache` 中间件**：代码更新后浏览器立即拿到
  新版本（保留 ETag 304）。
- 密钥预设扩到 11 家厂商（新增 DeepSeek / Moonshot / 智谱 / 通义 / xAI /
  OpenRouter / 硅基流动 / Meta）。
- **Web UI 模型密钥面板**：预设 + 任意自定义变量名；只写系统钥匙串、绝不回显；
  来源状态（环境变量 / `.env` / 钥匙串 / 未配置）实时标注，环境变量遮蔽钥匙串时
  显式提示。底层新增 `core/secrets.secret_status()`。
- **Windows 三类分发**：`ai-council.exe`（单文件，下载即用）、
  `ai-council-win64.zip`（目录版，解压即用、启动无需自解压）、
  `AI-Council-Setup-<ver>.exe`（Inno Setup 用户级安装器，免管理员，含开始菜单与
  桌面快捷方式）。脚本：`tools/build_exe.py`（`--onedir` / `--zip`）与
  `tools/build_installer.py`（目录产物 + zip + 安装器一步到位）。

### Changed

- 顶栏布局收敛：密钥 / 语言 / 主题等控件并入设置面板。
- `--glass-fill-card` 类毛玻璃透明度提升，背景光效可透出卡片。
- `config.example.toml` 增补 10 家厂商接入示例；`docs/PROVIDERS.md` 全量
  刷新（14 家厂商参数表，核实 2026-09-05/06）。
- 项目版本升至 `1.1.0`。

### Fixed

- **冻结版 exe 在 Windows GBK 控制台崩溃**：打印 `⚠`/`✓` 等符号触发
  UnicodeEncodeError。CLI 入口现强制 stdout/stderr 为 UTF-8。
- 问号气泡被相邻卡片遮挡：卡片 `backdrop-filter` 层叠上下文困住气泡，
  悬停时整卡抬升解决。
- 问号气泡被 `overflow: hidden` 裁剪：顶部装饰 sheen 改用
  `border-radius: inherit` 自贴圆角。
- 下拉框移出后残留提示框：移除 select 的 `text-overflow: ellipsis` 并
  挂空 `title`，压制 Chromium 对截断文字的原生提示；页面气泡改 JS 控制显隐。

### Security

- 不变量不变：密钥只写系统钥匙串 / `.env`；附件与模型输出仍以数据区包裹。

### 测试

- 新增约 150 项：注册表厂商矩阵与世代防伪（伪造 slug 回归）、会话覆盖
  矩阵、131 项全模型干跑、密钥预设清单、删除端点端到端、光效气泡 DOM
  探针等。合计 349 项（1 跳过）；`ruff` 全过。
- `council run` 与 `council serve`（静态资源 / 主题 / 完整会话）均以构建产物做过
  端到端冒烟；Web 密钥 API 另有 7 项离线测试（keyring 打桩）。

### Removed

- `gpt-5.2-chat-latest`、裸 `gpt-6`、`gemini-3.7-pro`、`gemini-3.6-pro`
  等未证实或已下线的模型条目；DeepSeek 收敛为官方在役的三个模型 ID。

## [1.0.0] - 2026-09-02

第六个里程碑（M6）：CI、打包与 v1.0.0 发布。

### Added

- **GitHub Actions CI**（`.github/workflows/ci.yml`）：quality job（`ruff format --check`、
  `ruff check`、`mypy --no-incremental`）；test job 矩阵（Python 3.11 / 3.12 / 3.13 ×
  ubuntu / windows，测试全部离线、只用 fake 适配器）；build job（`uv build` → 校验
  wheel 内含 `council/web/static` 与版本元数据、sdist 含 tests/docs → 全新 venv 安装
  wheel 后跑一次 fake `council run` 冒烟 → 上传 dist artifact）。
- **发布工作流**（`.github/workflows/release.yml`）：推送 `v*` 标签即 `uv build` 并创建
  GitHub Release，附带 wheel 与 sdist。
- **版本单源**：`pyproject.toml` 改用 hatch dynamic version（`path = council/__init__.py`），
  `council.__version__ = "1.0.0"` 是唯一版本源；API meta 同源；打包一致性测试守护
  metadata == attr == api。
- **打包修正**：移除 wheel `force-include`——`council/web/static` 位于包树内会自动随包，
  force-include 反而导致「同一路径二次写入」构建报错；wheel / sdist 构建与全新安装冒烟通过。

### Changed

- 项目版本升至 `1.0.0`；classifier 由 Alpha 改为 Production/Stable。

### 测试

- 新增 3 项打包一致性测试。合计 200 项（1 跳过）。

## [0.5.0] - 2026-09-02

第五个里程碑（M5）：Web UI、CSS 变量主题与 i18n。

### Added

- **Web UI（`council serve`，FastAPI + WebSocket）**：与会话同进程运行同一套引擎——
  HTTP 只做建会话/动作/导出转发，事件经 WebSocket 实时推送（先订阅、后按 `since=seq`
  回放并按 seq 去重；`CALL_CHUNK` 不落库只走直播）。暂停 / 恢复 / 终止 / 运行中切换
  失败策略全部可用；干预面板（`ask_user`、选主案、人工决定）以 WS 问答票据往返；
  无人值守时保守兜底（剔除失败节点 / 取首个候选），会话绝不因没人回答而挂死。
- **附件经数据区上传**：文件内容以 JSON 提交，服务端先落应用数据目录、由 M4 附件门
  （扩展名 + 内容嗅探）在 POST 内同步校验，不合格立即 400，不产生幽灵会话；按会话
  子目录存放以保留原始文件名。
- **导出即下载**：`GET /api/sessions/{id}/export` 渲染净化后的 Markdown 流式返回
  （`raw` 参数绕过净化），浏览器下载——服务端不写用户路径。
- **CSS 变量主题**：浅色 / 深色 / 高对比三套内置主题是纯数据 JSON；变量完整性、色值
  合法性、WCAG 对比度均有离线测试；主题可导入 / 导出并持久化到浏览器本地。
- **i18n zh/en**：界面文案走 JSON locale，双语 key 集合一致性有离线测试。
- **EventStore 线程屏障**：`threading.Lock` 保护 sqlite 连接，`close()` 等所有在飞 SQL
  完成——修复「任务取消后仍运行的 worker 与连接关闭竞争」导致的 Windows
  access violation。

### Changed

- 不变量不变：Web 与会话同进程，事件日志仍是唯一事实源；resume 依旧只重发未完成调用。

### 测试

- 新增 12 项：API / WebSocket 直播与回放 / 暂停恢复与终止 / ask_user 无人值守兜底 /
  附件 fail-fast / 导出 / 双语 key 与主题数据矩阵。合计 197 项（1 跳过）。

## [0.4.0] - 2026-09-02

第四个里程碑（M4）：约束层——附件限制、单一方案校验、净化管线、导出。

### Added

- **附件读取**（`core/attachments.py`）：按用户要求放开机器可读类型——默认允许
  `.txt / .md / .json / .csv`（`[attachments].allowed_suffixes` 可配）；扩展名+
  内容双重校验（`.json` 必须严格可解析、`.csv` 必须可被 csv 模块解析、UTF-8
  严格解码含 BOM、NUL 字节视为二进制伪装），拒绝符号链接/目录/越权大小与数量；
  超上下文预算按「头部+尾部+中间省略标记」截断并在 UI/提示词明示。
- **附件进数据区**：全部内容经 `<data_zone>` 注入提示词；`SessionCreated` 只记
  文件名，续跑时需用 `--file` 重新提供（缺失则明确报错，不静默丢上下文）。
- **单一方案校验**（`core/single_plan.py`）：提案/修订输出在结构合法后仍须通过
  「唯一方案」门——并列方案标题（方案一/二、选项 A/B、Plan B、备选、或者可以、
  两种思路等）命中即自动重新提问并给出「合并为一个方案」的严格修复提示（最多
  2 次），仍违规则拒绝该输出并按失败策略处理；代码块/行内代码/URL 受保护区
  豁免，引用示例不会误伤。
- **净化管线**（`council/sanitize/`）：规则表驱动、逐条可开关
  （`[sanitize].disabled_rules`），去除装饰性 Markdown 噪声/空行/行尾空格/
  套话开头结尾/表情堆砌，规范中英文间距与全角半角标点；代码块、行内代码、
  URL、列表与表格语义全程保护；事件日志永远保留 raw_text，净化只是视图。
- **导出**：`council export <session> <path.md>`（默认净化，`--raw` 走原始）——
  用户主动指定的唯一写入点，输出终稿/执行步骤/假设/风险/待确认问题/人工决定/
  收敛过程。

### Changed

- `council run/resume` 新增 `--file` 附件入口。

### 测试

- 新增约 40 项：附件规则矩阵（伪装扩展名/非 UTF-8/NUL/符号链接/限额/截断）、
  单一方案 golden 与自动纠正回路、净化 golden 逐规则、导出端到端。合计 185 项。

## [0.3.0] - 2026-09-02

第三个里程碑（M3）：韧性加固。

### Fixed

- **`policy = pause` 现在真的冻结**：`_pause_and_wait` 此前从不清除暂停门，
  已打开的门让等待立即返回——冻结等于没冻。

### Added

- **断网自动续跑**：传输型失败（network/rate_limit/timeout）导致整阶段阻塞
  时，引擎在一个冷却周期后做健康探测并重新接纳被剔除节点，就地重试该阶段；
  探测仍失败则落盘退出交给 `council resume`。`PhaseBlocked`/`NodeUnavailable`
  以 `retryable` 标记区分传输型与人工型阻塞。
- **熔断器抽象**：`core/breaker.py` 独立连败计数（触发/复位/半开语义可单独
  单测）；事件日志中的 degraded 状态仍是持久事实。
- **干预动作补齐测试**：WAIT（冻结后进程内恢复续跑）、SWITCH_ADAPTER（重建
  适配器并恢复）、运行中切换失败策略立即生效。

### 测试

- 新增 14 项韧性测试：空闲卡死检测（idle 超时分类）、流中断后续跑只重发未
  完成调用（不重复计费）、断网阻塞→探测→自动恢复、持续断网落盘退出、
  熔断器矩阵。合计 148 项测试。

## [0.2.0] - 2026-09-02

第二个里程碑（M2）：真实适配器与数据化注册表。

### Added

- **真实适配器族**：`openai_api`（含任意 OpenAI 兼容端点，自定义 `base_url`）、`anthropic_api`、`google_api`、`cli_session`（Codex CLI / Claude Code / Antigravity 本地登录态）、`generic_http`（用户自定义请求模板，URL / headers / body 模板 / 响应 JSONPath）。
- **cli_session 安全约束**：参数一律数组传递（无 `shell=True`），强制各家只读/禁工具/非交互参数（Codex `--sandbox read-only --ephemeral`、Claude `--disallowedTools`、Antigravity `--mode=plan`），只读临时运行目录，超时终止整个进程树；安全参数固化在代码中，不可由用户数据覆盖。
- **模型注册表**：`council/registry/*.toml` 数据化模型 ID、能力、思考等级映射（OpenAI `reasoning_effort` 枚举、Anthropic `budget_tokens`、Gemini `thinkingLevel`/`thinkingBudget` 按代际选择）；用户可在 `<数据目录>/registry/*.toml` 覆盖或新增，引擎零改动。
- **密钥管理**：解析顺序 环境变量 → `.env` → OS keyring；`council key set/delete/check` 只写系统钥匙串、绝不回显值；`redact()` 供日志脱敏；`secret_fingerprint` 供「同账号」比较而不落地明文。
- **厂商核实文档**：`docs/PROVIDERS.md` 记录全部参数名、取值范围、官方文档链接与核实日期（2026-09-02）；查不到的项（adaptive effort 枚举、官方定价等）明确列为 TODO，绝未编造。
- **配置增强**：`nodes.settings` 承载适配器专属参数；`config-check` 与 `run` 输出注册表校验警告（未知模型、不支持的思考等级、cli_session 缺 profile）。

### Changed

- P7 统计在注册表提供已核实定价时输出成本，否则 `cost_usd` 保持 `None`（不编数字）。

### 测试

- 新增 48 项离线测试（httpx MockTransport + 假 CLI 子进程），覆盖三家 API 的流式解析/参数构造/错误分类、`cli_session` 注入防护与超时杀树、注册表合并与校验、密钥解析与脱敏。

## [0.1.0] - 2026-09-02

首个里程碑（M1）：引擎骨架端到端可用。

### Added

- **会议状态机**：P0 上下文 → P1 并行独立提案 → P2 Judge 选主案（可切人工）→ P3 并行独立评审 → P4 round-robin 辩论 → P5 裁定 → P6 完整修订 → 循环至 PASS / max_rounds / 收敛停滞 → P7 终稿与统计。阶段流转只读结构化枚举字段，非法 JSON 最多修复 2 次后升级为 `NEED_USER_DECISION`，绝不静默降级。
- **事件溯源存储**：SQLite（WAL、`synchronous=FULL`、单事务追加）事件日志；`(session_id, phase, round, node_id, attempt)` 幂等键（辩论轮带 `tag`）；`council resume` 回放复用已完成调用，只重发 in-flight / 失败调用。
- **容错**：错误六分类（auth / rate_limit / timeout / content_refusal / network / contract），指数退避加抖动重试，节点熔断与健康探测自动回归，`continue / pause / ask_user` 三策略可在运行中切换，低于 `min_quorum` 强制冻结。
- **干预协议**：`InterventionHandler`（on_failure / on_select / on_decision），CLI 用 stdin 实现，Web UI 之后可无侵入接入。
- **配置系统**：单一 `config.toml`（pydantic 校验、`extra=forbid`、语义校验、schema_version 迁移、指纹与人类可读 diff）；Judge 与参与者重合直接报错，同账号同模型给出独立性警告。
- **提示词与注入防护**：模板文件化（`{{placeholder}}` 渲染，未知占位符抛错），`<data_zone>` 边界包裹与闭合标签转义。
- **输出契约**：Proposal / Selection / Review / Debate / Verdict / Revision 六套结构化契约与修复回路；`NEED_USER_DECISION` 强制携带 `question_for_user`。
- **CLI**：`council run` / `resume` / `sessions` / `config-check`；零配置默认使用内置 fake 适配器，克隆后无需任何密钥即可跑通完整八阶段。
- **测试**：84 项测试全部离线（fake 适配器，无真实网络），覆盖修复回路、失败策略、熔断、法定人数、断点续跑、配置校验与 CLI 冒烟。

### Security

- 密钥不入仓库：配置模型不包含任何密钥字段；真实密钥仅存 OS keyring 或已 gitignore 的 `.env`（M2 接入）。
- 附件与模型输出一律视为数据：数据区包裹 + 系统提示声明 + 闭合标签转义。

[Unreleased]: https://github.com/xmwy0712/ai-council/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/xmwy0712/ai-council/releases/tag/v1.3.0
[1.2.2]: https://github.com/xmwy0712/ai-council/releases/tag/v1.2.2
[1.2.1]: https://github.com/xmwy0712/ai-council/releases/tag/v1.2.1
[1.2.0]: https://github.com/xmwy0712/ai-council/releases/tag/v1.2.0
[1.1.1]: https://github.com/xmwy0712/ai-council/releases/tag/v1.1.1
[1.1.0]: https://github.com/xmwy0712/ai-council/releases/tag/v1.1.0
[1.0.0]: https://github.com/xmwy0712/ai-council/releases/tag/v1.0.0
[0.5.0]: https://github.com/xmwy0712/ai-council/releases/tag/v0.5.0
[0.4.0]: https://github.com/xmwy0712/ai-council/releases/tag/v0.4.0
[0.3.0]: https://github.com/xmwy0712/ai-council/releases/tag/v0.3.0
[0.2.0]: https://github.com/xmwy0712/ai-council/releases/tag/v0.2.0
[0.1.0]: https://github.com/xmwy0712/ai-council/releases/tag/v0.1.0
