# 个人 Agent 与核心插件

> 面向管理员。v1 的「个人 Agent」入口只对管理员开放：CLI 核心会以应用进程身份
> 在本机执行命令，等同于把 shell 交给使用者，不应该默认开放给每个员工。

## 1. 它是什么

应用里有一个「个人 Agent」工作台：左侧是会话，顶部可以随时切换 **Agent 核心**，
每个核心都可以使用你在「模型设置」里已经配置好的模型。

- **CLI 核心**（内置 + 插件）：Claude Code、Codex CLI、Qwen Code、Aider、Goose
  等本机命令行 Agent。应用负责找到可执行文件、把当前会话选中的模型注入成该 CLI
  认识的环境变量，然后启动它并把输出流回界面。
- **核心可随时更换**：会话是「每轮无状态」的，切换核心不会丢历史；下一轮消息
  就由新核心执行，聊天里会留一条「已切换核心」的系统提示。
- **不检索知识库**：知识库问答在「智能问答」页（那边有完整的检索、引用与拒答
  链路）。个人 Agent 只做「本机助手」这一件事，所以没有知识库核心、也不带
  `kb_ids`；两者各司其职，不重复实现。

提示词是通用个人助手定位（回答用户问题、必要时用本机工具），并明确要求
**直接回答问题**：不先让用户确认任务类型，不复述自己收到的设定，不罗列任务清单。
这条规则来自一次真实反馈——模型把用户本机 Codex 的人格设定当成了本应用的设定，
两轮都没回答用户问的市场规模。事后查清是两个原因叠在一起：① Codex 直接用了用户
`~/.codex`（人设被当成本应用的设定，见 §2.1）；② 提示词是多行的，而 `codex exec`
只取第一行的位置参数，用户的问题根本没送到模型（见 §3.3）。两处都已修。

这和 magpie 的关系：

| magpie | 本实现 |
| --- | --- |
| Agents 页：检测已安装的 Agent、为它选模型 | 「核心」注册表 + `shutil.which` 探测 + 模型选择器 |
| 本地 gateway：所有 Agent 共用一个模型入口 | 不做网关，直接把供应商的 base_url / key / model 按协议注入 CLI 环境变量 |
| 插件（npm 包，provider / middleware） | JSON 清单插件：只描述「怎么启动一个 CLI 核心」，不加载第三方代码 |
| 改各 Agent 的配置文件 | 只在本轮子进程的环境变量里注入，退出即失效；唯一的例外是 Codex 的应用独占配置目录（见 §2.1，它是「隔离」而不是「改用户配置」） |

## 2. 内置核心

| id | 名称 | 协议 | 探测命令 | 注入的环境变量 |
| --- | --- | --- | --- | --- |
| `claude-code` | Claude Code | `anthropic` | `claude` | `ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_MODEL` |
| `codex` | Codex CLI | `openai` / `openai-responses` | `codex` | `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`CODEX_HOME`（应用独占，见 §2.1） |
| `qwen-code` | Qwen Code | `openai` | `qwen` | `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_MODEL` |
| `aider` | Aider | `openai` | `aider` | `OPENAI_API_BASE`、`OPENAI_API_KEY` |
| `goose` | Goose | `openai` | `goose` | `GOOSE_PROVIDER=openai`、`GOOSE_MODEL`、`OPENAI_BASE_URL`、`OPENAI_API_KEY` |

界面上只会显示本机实际能探测到的核心；没装的会显示「未安装」和缺少的命令名。
协议不兼容的模型不会出现在该核心的模型列表里（例如 Claude Code 只列
Anthropic Messages 协议的供应商）。

> 除 Codex 之外，CLI 自己的登录态、配置文件、权限策略仍然生效。应用只注入
> 「这一轮用哪个模型」，不会替 CLI 打开 `--dangerously-skip-permissions` 之类的开关；
> 需要工具权限时，请在插件清单的 `run_args` 里按该 CLI 的文档自行配置，并清楚风险。

### 2.1 Codex 的配置隔离（`CODEX_HOME`）

Codex CLI 默认从 `~/.codex` 读**用户自己的** `AGENTS.md`、`config.toml`、hooks、
memories、人格设定、登录态和历史会话。个人 Agent 直接用那个目录，就会把用户长期
使用的开发规范当成本应用的系统提示词（§1 提到的那次答非所问就是这么来的）。所以
Codex 核心跑在应用自己的配置目录里：

| 项 | 值 / 行为 |
| --- | --- |
| 目录 | `<数据目录>/agent_codex_home`（默认 `<仓库>/data/agent_codex_home`），持久、应用独占，**所有会话共用一份**。里面除了应用写的 `config.toml` 和 `skills/`，其余是 Codex 自己写的运行状态（`sessions/`、`logs_*.sqlite` 等）——它们都留在这个目录里，既不进用户 `~/.codex`，也不进仓库（`data/` 已被 gitignore） |
| `config.toml` | 应用生成，只有说明性注释，没有任何个性化配置；被手改过会在下次执行前恢复 |
| 模型与供应商 | **不落盘**，每轮仍用 `-c model_provider=openai -c openai_base_url=…` 和 `OPENAI_API_KEY` 注入，换模型/换供应商照旧 |
| 用户 `~/.codex` | 不读取、不改写：`AGENTS.md`、`config.toml`、`hooks`、`memories`、`auth.json`、`sessions` 都不参与 |
| 技能 | 用户 `~/.codex/skills/*`（含 `SKILL.md` 的技能目录，跳过 `.system`）以**目录联接/符号链接**挂到 `<home>/skills/<名字>`，只读复用；链接不可用时复制到应用目录（仍然不写用户目录）。登记结果写在 `<home>/skills/.wrk-app-skills.json`，源里删掉的技能会同步撤掉 |
| 探测 | 版本探测（`codex --version`）也带上同一个 `CODEX_HOME`，避免探测过程往用户 `~/.codex` 里写 `installation_id`、`.sandbox` 之类的东西 |
| 失败 | 隔离目录建不出来时直接报错，**不会**偷偷退回用户 `~/.codex`——那样等于把人设又带回来 |

为什么不用 `[[skills.config]]`：在 codex-cli 0.167.0 上实测，`[[skills.config]]` 里指向
`CODEX_HOME` 之外的绝对路径**不会**出现在技能目录里（`codex exec` 发给模型的请求体里
看不到该技能）；而 `<CODEX_HOME>/skills/` 是技能根，链接进去的技能会进
「Available skills」。技能内容不进仓库，也不复制进用户目录。

> Codex 自身的「宿主技能发现」仍会读 `~/.agents/skills`（那是另一套共享技能库）。
> 这属于「技能复用」，与提示词/人设无关；如果不想让它出现，请在那个目录里清理技能。

**本机实测**（Windows + codex-cli 0.167.0，2026-11-11）。做法：用应用自己的
`codex_home.prepare()` + `plugins.build_env()` 起真实的 `codex exec`，把它的 base_url
指向一个只记录请求体的本地假 API（不消耗真实密钥），再比对模型实际收到的内容：

| 指标 | 隔离后（`<数据目录>/agent_codex_home`） | 不隔离（`CODEX_HOME` 指向放了真实 `AGENTS.md` 的目录） |
| --- | --- | --- |
| 用户 `~/.codex/AGENTS.md` 的人格设定（探针串「御主」） | **不出现** | 出现（额外 11,371 字符的 user 消息） |
| 应用提示词 | 288 字符、单行，含用户提问 | 同 |
| 用户技能 `wf-verify`（只存在于 `~/.codex/skills`） | 出现在可用技能列表（`r0/wf-verify/SKILL.md`） | 不出现 |
| `~/.agents/skills` 的共享技能 `archify` | 出现 | 出现 |

同一轮执行前后，`~/.codex` 的 `AGENTS.md`、`config.toml`、`auth.json` 与 4 个 SQLite
库的修改时间全部未变；两次完整探测前后，`skills/` 下 13,183 个文件也一个没动。
**本机无法验证的**：真实供应商的回答质量（没有可用密钥）、macOS/Linux 上的符号链接
路径（只验证了 Windows 的目录联接与「链接失败退回复制」）、其它 CLI 核心的刷新行为。


## 3. 写一个插件

把 JSON 文件放进数据目录的 `agent_plugins/`（界面「核心与插件」里能看到完整路径），
然后点「重新扫描」。文件名随意，`id` 必须唯一。

### 3.1 最小示例

```json
{
  "schema": 1,
  "id": "my-agent",
  "name": "My Agent",
  "description": "我自己的 CLI Agent",
  "kind": "cli",
  "protocols": ["openai"],
  "bins": ["my-agent"],
  "version_args": ["--version"],
  "run_args": ["run", "--prompt", "{prompt}", "--model", "{model}"],
  "env": {
    "OPENAI_BASE_URL": "{base_url}",
    "OPENAI_API_KEY": "{api_key}",
    "OPENAI_MODEL": "{model}"
  },
  "output": "text",
  "timeout_seconds": 900
}
```

### 3.2 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `schema` | 否 | 固定 `1`；不写按 1 处理 |
| `id` | 是 | 小写字母/数字/`.`/`_`/`-`，以字母开头，≤64 字符；不能与内置核心重名 |
| `name` | 是 | 界面显示名，≤80 字符 |
| `description` | 否 | 卡片上的一句话说明 |
| `kind` | 是 | 只能是 `cli`。个人 Agent 不再内置知识库核心，`kb` 是保留 id，插件也不能占用 |
| `protocols` | 是 | `openai` / `openai-responses` / `anthropic` 的非空子集 |
| `bins` | 是 | 按顺序探测的**命令名**，只能写 PATH 中可查找的名字（如 `qwen`、`python`）；不接受绝对路径、相对路径、`..`、空格或 shell 元字符；解析结果若是 `.cmd/.bat` 等批处理脚本或命令解释器会被拒绝 |
| `version_args` | 否 | 探测版本用的参数，默认 `["--version"]`；失败不影响可用性 |
| `run_args` | 是 | 非交互执行参数，必须包含 `{prompt}`，不得包含 `{api_key}`，也不得把 `&`、`|`、`^`、`%VAR%` 这类 cmd 元字符/变量引用作为独立参数 |
| `env` | 否 | 注入子进程的环境变量模板，最多 32 项；键必须在 `ALLOWED_ENV_KEYS` 白名单内，不能覆盖 `PATH`/`PYTHONPATH`/`NODE_OPTIONS` 等系统或代码加载变量 |
| `output` | 否 | `text`（默认，逐行当正文）或 `jsonl`（逐行解析 JSON） |
| `text_paths` | 否 | `output=jsonl` 时提取正文的点路径，如 `["item.text","message"]` |
| `answer_item_types` | 否 | `output=jsonl` 时哪些 `item.type` 的文字算「最终回答」，如 `["agent_message"]`；留空时推理、命令执行等已知过程类型默认归「思考过程」，其它仍按旧行为当正文。见 §3.4。写了 `output=text` 会被拒绝 |
| `usage_event` | 否 | `output=jsonl` 时哪一类事件带 token 用量（如 `turn.completed`）；写了 `output=text` 会被拒绝。见 §3.4 |
| `timeout_seconds` | 否 | 单轮硬超时，10..3600，默认 900；仍受全局 `WRK_AGENT_RUN_TIMEOUT` 限制 |
| `requires_workspace` | 否 | 默认 `true`；CLI 在会话工作目录里启动（该目录必须落在 `AGENT_WORKSPACE_DIR` 内） |

> **环境变量白名单（P1-7）**：清单 `env` 的键必须在这组允许键里，默认拒绝其余所有键：
> `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_API_BASE`、`OPENAI_MODEL`、`OPENAI_ORG_ID`、
> `OPENAI_ORGANIZATION`、`OPENAI_PROJECT_ID`、`ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、
> `ANTHROPIC_BASE_URL`、`ANTHROPIC_MODEL`、`GOOSE_PROVIDER`、`GOOSE_MODEL`、
> `NO_COLOR`、`FORCE_COLOR`、`TERM`、`LANG`、`LC_ALL`、`PYTHONIOENCODING`、`PYTHONUTF8`。
> `PATH`、`PYTHONPATH`、`NODE_OPTIONS`、`LD_PRELOAD` 等能改变「加载哪段代码」的变量一律不接受；
> 系统先保留自身的最小环境（PATH、TEMP、HOME 等），再叠加白名单模板，且系统变量不会被模板改写。
> 需要新键时改 `backend/app/services/agent/plugins.py` 的 `ALLOWED_ENV_KEYS` 并补测试。

### 3.3 占位符

| 占位符 | 值 |
| --- | --- |
| `{prompt}` | 拼好的提示词（系统说明 + 最近对话 + 本轮用户消息） |
| `{model}` | 会话当前模型在供应商侧的 model id |
| `{base_url}` | 按协议规范化后的供应商地址（OpenAI 补 `/v1`，Anthropic 去掉尾部 `/v1`） |
| `{api_key}` | 供应商密钥；没有配置时是 `local`。**只允许出现在 `env` 模板里**，写进 `run_args` 的清单会被直接拒绝 |
| `{workspace}` | 会话工作目录的绝对路径（一定在 `AGENT_WORKSPACE_DIR` 内） |
| `{session_id}` | 会话 id |
| `{app_name}` | 应用内的界面名（当前是「个人 Agent」） |

> `isolated_home` 是内置 Codex 核心的字段（见 §2.1），清单里写它会被拒绝——
> 外部插件不允许给自己要一份应用独占的配置目录。

参数和环境变量都会做占位符替换；不会经过 shell，`run_args` 是数组，不拼接字符串。
应用不再为 `.cmd/.bat` 包一层 `cmd.exe /c`（那样参数会被二次解析，`& | ^ %VAR%` 可注入命令），
`Popen(shell=False)` 由 Python 自己转义参数。命令行摘要、运行日志、正文与错误信息会统一
把密钥替换成 `<密钥>`、把完整提示词替换成 `<提示词>`。

> **`{prompt}` 一定是单行。** `codex exec [PROMPT]` 只把位置参数的第一行当成任务：
> 传多行提示词时，后面的工作目录、对话记录和用户提问会被静默丢掉，模型只看到第一行，
> 于是答非所问（codex-cli 0.167.0 本机实测，请求体里只剩第一行）。应用因此在拼提示词
> 时就把换行压成空格，插件不需要也不应该自己处理换行；不要用「多行提示词」来表达结构，
> 用一句话把要求和用户问题都说清楚。

### 3.4 JSONL 输出

如果 CLI 输出结构化事件，用：

```json
{
  "output": "jsonl",
  "text_paths": ["item.text", "msg.message", "message", "text"]
}
```

应用会逐行 `json.loads`，按 `text_paths` 顺序找第一个非空字符串作为正文候选；
解析失败的行会作为运行日志展示，不会中断执行。Codex CLI 的
`codex exec --json` 就使用这种模式。

**正文分两栏**：候选文字还要按事件的 `item.type` 分流成「回答」与「思考过程」，
见下一节。只想让某类事件进正文的清单必须声明 `answer_item_types`。

#### 思考过程与最终回答（`answer_item_types`）

`codex exec --json` 把推理和最终回答都放在**同一种事件**里（`item.completed`），
区别只在 `item.type`。只看 `text_paths` 时，两者都会进正文——用户 2026-11-11
的截图就是这样：气泡里从「用户问的是…」一路铺到「Command rejected by policy…」，
是整段内心推理与工具尝试，最终答案反而没落在气泡里。

本机 `codex-cli 0.167.0` 的实测事件（2026-11-11，用本地 mock 的 Responses API
喂一次确定性响应，没有调用真实在线模型；`CODEX_HOME` 用临时目录）：

```json
{"type":"thread.started","thread_id":"01a1cb10-..."}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"error","message":"Model metadata for `gpt-5` not found..."}}
{"type":"item.completed","item":{"id":"item_2","type":"reasoning","text":"用户问的是市场规模。先看看工作目录里有没有资料。"}}
{"type":"item.completed","item":{"id":"item_3","type":"agent_message","text":"2026 年市场约 240 亿元。"}}
{"type":"turn.completed","usage":{"input_tokens":120,"cached_input_tokens":0,"output_tokens":30,"reasoning_output_tokens":5}}
```

另有顶层 `{"type":"error","message":"..."}`（重连提示等）与 `item.type:"error"`
（websocket 回退等），前者由运行器当运行日志，后者不带 `text` 所以不进正文。
本机的命令执行全被策略拦下（"blocked by policy"），**没有产生任何 JSONL item**，
只有模型在推理里提到它；所以这里不照搬推测的 `command_execution` 字段结构，
也不做逐条命令步骤面板。二进制里能确认存在 `command_execution` / `file_change` /
`mcp_tool_call` / `web_search` 等 item 类型名，但它们的字段形状本机未实测。

因此内置 `codex` 清单声明 `answer_item_types: ["agent_message"]`。分流规则三档：

1. **声明了 `answer_item_types`**：只认名单里的 `item.type` 为回答；其余一切带文字
   的事件（`reasoning`、命令执行、文件改动…）都进「思考过程」。推荐新清单用这档。
2. **没声明，但 `item.type` 是 `reasoning` / `command_execution` / `file_change` /
   `mcp_tool_call` / `web_search`**：默认当过程，不进回答（外部插件不改清单也不会
   再被「推理冒充回答」坑到）。
3. **其余情况照旧**：`text_paths` 命中的第一个非空字符串就是回答正文——不能因为
   这次改动悄悄吞掉不认识的事件类型里的正文。

界面上「思考过程」是**默认折叠**的卡片（复用智能问答页 `.thinking-card` 的视觉），
回答留在主气泡；正在执行时回答位置只显示「正在思考…」。运行器用 `thinking`
事件推推理增量、用 `text` 事件推回答增量，收尾把推理写进助手消息的
`meta.thinking`（超长截断时 `meta.thinking_truncated: true`）。历史回放只用
`content`，思考过程不会塞进下一轮提示词。

取舍与边界（2026-11-11 定）：

- 命令执行/工具调用（含被策略拦截的命令）算「过程」的一部分，不进回答；本机实测
  被拦的命令没有独立 JSONL 事件，只有推理文字，所以不为它另造字段。
- 一轮**只出推理、没有最终回答**时，状态记 `failed`，正文明确写「只产出了思考过程，
  没有给出最终回答」，`meta.thinking` 保留全部推理供展开核对——不把推理倒进正文，
  也不假装执行成功。
- **已落库的旧消息不重解析**：库里只有当时的正文，没有原始 JSONL，事后切分只能
  靠猜。隔离只影响新执行；旧消息照原样显示。

#### 用量事件（`usage_event`）

想知道每个核心花了多少 token，让清单声明哪一类事件带用量：

```json
{
  "output": "jsonl",
  "text_paths": ["item.text"],
  "usage_event": "turn.completed"
}
```

匹配到该事件时，应用从事件的 `usage` 对象里读 token：

- 输入侧依次找 `input_tokens`、`prompt_tokens`；
- 输出侧依次找 `output_tokens`、`completion_tokens`；
- 只接受非负整数；负数、布尔、字符串、小数一律忽略（哪侧坏就丢哪侧，不猜）；
- **一次执行内出现多条用量事件会累加**，但调用数始终记 **1 次**——统计的是「用户点了一次」，
  不是 CLI 内部真实发了几个模型请求。

以 Codex CLI 0.167.0 的事件为例（`codex exec --json`）：

```json
{"type":"thread.started","thread_id":"..."}
{"type":"item.completed","item":{"type":"agent_message","text":"..."}}
{"type":"turn.completed","usage":{"input_tokens":120,"cached_input_tokens":40,"output_tokens":30,"reasoning_output_tokens":5,"total_tokens":150}}
```

内置 `codex` 清单声明的就是 `usage_event: "turn.completed"`。纯文本输出（`output=text`）
没有可解析的事件，**不要**写 `usage_event`——清单校验会直接拒绝，而不是静默忽略。

执行结束后这份用量写进助手消息的 `meta.usage`（键名同 `CallStats.as_dict()`：

```json
{"calls":1,"chat_calls":1,"live_calls":1,"offline_calls":0,
 "prompt_tokens":200,"completion_tokens":50,"embed_tokens":0,"latency_ms":1234}
```

），并成为 `/api/usage` 里「个人 Agent」来源的数据源（`docs/API.md` §7.2）。
一个用量事件都没解析到时，`meta` 里会多一个 `usage_unmetered: true`：调用照记 1 次，
token 记 0，用量页把它算进 `unmetered_records`。**不填推测值**是这个字段的设计目标——
CLI 没吐用量就老实说「未计量」。

### 3.5 一个能直接跑的示例

`docs/examples/agent-plugin-echo.json` 用 Python 回显提示词和注入的模型，用来验证
「插件被发现 → 环境变量注入 → 输出流回界面」这条链路：

```json
{
  "schema": 1,
  "id": "echo-agent",
  "name": "Echo Agent（示例）",
  "kind": "cli",
  "protocols": ["openai"],
  "bins": ["python"],
  "version_args": ["--version"],
  "run_args": [
    "-c",
    "import os,sys; print('echo:', (sys.argv[1] if len(sys.argv)>1 else '')[-120:]); print('model:', os.environ.get('OPENAI_MODEL',''))",
    "{prompt}"
  ],
  "env": {
    "OPENAI_BASE_URL": "{base_url}",
    "OPENAI_API_KEY": "{api_key}",
    "OPENAI_MODEL": "{model}"
  }
}
```

复制到 `agent_plugins/`，点「重新扫描」，选它、选一个 OpenAI 兼容模型即可对话。
它不会真的调用模型，只用来验证接线。

## 4. 模型注入规则

应用不复制、不修改你的供应商配置，也不会把密钥写进插件的 JSON 或 CLI 的配置文件。
每轮执行时：

1. 取会话选中的 `ModelConfig` 与 `Provider`；
2. 按供应商 `protocol` 生成 `base_url`：
   - `openai` / `openai-responses`：缺版本段时补 `/v1`（与 `services/llm._join` 一致）；
   - `anthropic`：去掉尾部 `/v1`（Claude Code 自己会拼 `/v1/messages`）；
3. 解密供应商密钥，填入清单 `env` 模板（只允许白名单键）；
4. 用最小环境变量集启动子进程（只保留 PATH、TEMP、HOME、APPDATA 等系统变量，
   不继承调用进程里的其它密钥类变量，也不允许清单改写系统变量）；
5. 命令行参数不含密钥，密钥只经 `env` 进入子进程；输出与日志统一脱敏。

换模型不需要改插件；换供应商也不需要。只有协议不兼容时界面会提示换模型。

## 5. 执行、停止与恢复

- 发送消息后先落一条 `pending` 的 assistant 消息，后台线程执行核心；
  前端每 350ms 轮询 `/api/agent/runs/{id}/events?after=` 追加回答正文（`text`）、
  思考过程（`thinking`）、日志和证据。
- 点「停止」会终止子进程树（Windows 用 `taskkill /T /F`，POSIX 用进程组），
  消息收尾为 `stopped`。
- 超过 `timeout_seconds` 自动终止，消息收尾为 `failed`。
- 应用重启时，所有 `pending` 的 Agent 消息会收尾为 `failed`，不会永远转圈。
- 事件只保存在内存里，最终结果落在 `agent_messages`；历史会话刷新后仍可读。

### 5.1 历史上的「知识库助手」会话

早期版本内置过 `kb` 核心，那些会话的 `core_id` 仍是 `kb`，消息也还在库里。现在的处理：

- **还能打开、还能读**：会话列表里照常出现，标题旁标注「知识库助手（已下线）」，
  历史消息完整可读；会话载荷里 `core_available=false`、`core_legacy=true`。
- **不能再用它提问**：非历史的 `core_id=kb` 请求返回 400，并指路「智能问答」页；
  历史 kb 会话发消息同样 400，提示先换核心。
- **可以改标题、可以换核心**：换到任意 CLI 核心后照常继续对话，聊天里会留一条
  「已切换 Agent 核心：知识库助手（已下线） → …」的系统消息；换核心时如果没指定
  模型，会自动挑一条与新核心协议兼容的。
- **只读的数据不迁移**：`agent_sessions.kb_ids` 列留在库里（历史值仍可查），但应用
  不再读它、也不写它，接口不再返回 `kb_ids`。删列会让老库升级时多一次不必要的迁移，
  所以保留。

## 6. 安全边界

- 入口、核心管理、插件启停都要求管理员；普通员工调用返回 403。
- 插件是**声明式**的：应用不会 import 插件代码、不会执行 JS；只按清单启动
  你指定的可执行文件。
- **`bins` 只认 PATH 里的命令名**：清单写绝对路径、相对路径、`..` 或命令解释器会被拒绝；
  解析结果若是 `.cmd/.bat` 等批处理脚本也会被拒绝（Windows 会把它们交给 `cmd.exe`
  二次解析参数）。内置核心的探测命令由代码写死、参数固定，不受此限制。
- 子进程环境变量使用**白名单**：只保留 PATH、TEMP、HOME 等系统变量，清单只能覆盖
  `ALLOWED_ENV_KEYS` 里的键，`PATH`/`PYTHONPATH`/`NODE_OPTIONS` 等一律不接受。
  版本探测子进程同样带这份最小环境，不继承后端全量环境。
- **密钥不进命令行**：`run_args` 里出现 `{api_key}` 的清单会被直接拒绝；密钥只放进 `env`
  模板。命令行摘要、运行日志、正文与错误信息统一脱敏成 `<密钥>` / `<提示词>`。
- CLI 能读写会话工作目录；默认目录是数据目录下的 `agent_workspace`。**工作目录必须落在
  `WRK_AGENT_WORKSPACE` 指向的目录内**（含其子目录），越界（含同前缀目录、`..`）返回 400；
  运行器在启动前还会再校验一次，防止历史会话里存着收敛之前的越界路径。
- 核心能执行什么，最终取决于你安装的 CLI 本身的权限模型；应用不会替它提权。

## 7. 接口

前缀 `/api`，全部要求管理员 Bearer 令牌。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/agent/cores` | 核心列表 + 探测结果 + 插件目录 + 清单错误；`isolated_home=codex` 表示该核心用应用独占的配置目录 |
| GET | `/agent/models?core_id=` | 模型列表，`compatible` 标记协议是否匹配 |
| POST | `/agent/plugins/rescan` | 清探测缓存并重新扫描插件目录 |
| PATCH | `/agent/plugins/{id}` | `{enabled}`：停用/启用核心（`kb` 返回 400 并指路「智能问答」） |
| GET | `/agent/sessions` | 当前管理员的 Agent 会话 |
| POST | `/agent/sessions` | `{title,core_id,model_config_id,workspace}`；不再接受 `kb_ids` |
| GET | `/agent/sessions/{id}` | 会话详情 + 消息 + 核心信息 + `core_available`/`core_legacy` |
| PATCH | `/agent/sessions/{id}` | 改标题/核心/模型/工作目录（不再有知识库） |
| DELETE | `/agent/sessions/{id}` | 删除会话（执行中返回 409） |
| POST | `/agent/sessions/{id}/messages` | `{content}` → `{run_id, user_message, assistant_message}` |
| GET | `/agent/runs/{id}/events?after=` | 轮询事件；`status` 为 `running/ok/failed/stopped` |
| POST | `/agent/runs/{id}/stop` | 停止执行 |

## 8. 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `WRK_AGENT_ENABLED` | `1` | 为 `0` 时不注册 Agent 路由 |
| `WRK_AGENT_PLUGIN_DIR` | `<数据目录>/agent_plugins` | 插件 JSON 目录 |
| `WRK_AGENT_WORKSPACE` | `<数据目录>/agent_workspace` | 默认工作目录；会话工作目录必须落在它里面（含子目录） |
| `WRK_AGENT_RUN_TIMEOUT` | `900` | 单轮执行硬超时（秒） |
| `WRK_AGENT_HISTORY_MESSAGES` | `12` | 回放给 CLI 的历史消息条数 |
| `WRK_AGENT_HISTORY_CHARS` | `1200` | 单条历史消息的最大字符数 |

Codex 的隔离配置目录**不新增环境变量**：它固定是 `<数据目录>/agent_codex_home`，
跟着 `WRK_DATA_DIR` 走（换数据目录就换一份隔离目录，里面只有应用自己写的配置）。
给 Codex 子进程设置的 `CODEX_HOME` 是内部注入值，不需要你配置；调用进程里已有的
`CODEX_HOME` 也会被它覆盖。

## 9. 排障

| 现象 | 原因与处理 |
| --- | --- |
| 核心显示「未安装」 | `bins` 里的命令不在 PATH；装好后点「重新扫描」 |
| 模型列表为空 | 没有与该核心协议兼容的已启用 LLM；去「模型设置」添加对应协议的供应商 |
| 启动后立刻失败 | 看消息里的 stderr；常见是 CLI 版本参数不兼容、需要登录、或工作目录不存在 |
| 回复里还带着你本机 Codex 的人格设定（「御主」之类） | 这一轮没有走隔离目录，或 `CODEX_HOME` 没生效：先点「重新扫描」看 Codex 是否显示为可用，再查 `<数据目录>/agent_codex_home/config.toml` 是否存在、日志里有没有「使用应用自己的配置目录」那行 |
| Codex 说技能不存在 | 用户 `~/.codex/skills/<名字>/SKILL.md` 缺失或缺少 YAML frontmatter（`---` 包裹的 `name`/`description`）；修好后下一轮执行会自动同步（第一次执行时会建链接） |
| 技能目录里出现奇怪目录 | `<home>/skills/.system` 是 Codex 自己写的，应用不同步也不清理它；`.wrk-app-skills.json` 是应用的登记文件 |
| 「隔离配置目录准备失败」 | 数据目录不可写，或 `<数据目录>/agent_codex_home` 被同名文件占了；清掉它并确认数据目录可写后重试（应用不会退回用户 `~/.codex`） |
| 历史会话提示「知识库助手已下线」 | 见 §5.1：换成任意 CLI 核心即可继续对话，历史消息不会丢 |
| 输出乱码 | CLI 不是 UTF-8 输出；在清单 `env` 里加 `PYTHONIOENCODING` 或该 CLI 的编码开关 |
| 插件没出现 | 看「核心与插件」顶部的清单错误；常见是 JSON 语法、id 重复、缺 `{prompt}` |
| 清单里写了绝对路径，插件没出现 | `bins` 只能写 PATH 中的命令名；改成命令名，或把可执行文件所在目录加进 PATH |
| 装了 CLI 仍显示「未安装」 | 若 PATH 解析到 `.cmd/.bat` 批处理脚本会被安全边界拒绝；改用该 CLI 的原生可执行文件（`.exe`），或安装提供原生二进制的版本 |
| 提示「env.xxx 不在允许的白名单键内」 | 把该环境变量登记到 `ALLOWED_ENV_KEYS`，或改用白名单里已有的键 |
| 提示「工作目录必须位于 Agent 工作区内」 | 把项目目录放到 `WRK_AGENT_WORKSPACE` 下，或把 `WRK_AGENT_WORKSPACE` 指到包含它的父目录后重启 |
| 清单里写了 `isolated_home`，插件被拒 | 该字段只给内置 Codex 核心用；外部插件删除这一项即可 |
