# 个人 Agent 与核心插件

> 面向管理员。v1 的「个人 Agent」入口只对管理员开放：CLI 核心会以应用进程身份
> 在本机执行命令，等同于把 shell 交给使用者，不应该默认开放给每个员工。

## 1. 它是什么

应用里新增了一个「个人 Agent」工作台：左侧是会话，顶部可以随时切换 **Agent 核心**，
每个核心都可以使用你在「模型设置」里已经配置好的模型。

- **知识库助手**（内置）：直接走现有 RAG 链路，检索知识库、带引用回答；
  没有配置模型时仍可用抽取式回答。
- **CLI 核心**（内置 + 插件）：Claude Code、Codex CLI、Qwen Code、Aider、Goose
  等本机命令行 Agent。应用负责找到可执行文件、把当前会话选中的模型注入成该 CLI
  认识的环境变量，然后启动它并把输出流回界面。
- **核心可随时更换**：会话是「每轮无状态」的，切换核心不会丢历史；下一轮消息
  就由新核心执行，聊天里会留一条「已切换核心」的系统提示。

这和 magpie 的关系：

| magpie | 本实现 |
| --- | --- |
| Agents 页：检测已安装的 Agent、为它选模型 | 「核心」注册表 + `shutil.which` 探测 + 模型选择器 |
| 本地 gateway：所有 Agent 共用一个模型入口 | 不做网关，直接把供应商的 base_url / key / model 按协议注入 CLI 环境变量 |
| 插件（npm 包，provider / middleware） | JSON 清单插件：只描述「怎么启动一个 CLI 核心」，不加载第三方代码 |
| 改各 Agent 的配置文件 | 不落盘改配置；只在本轮子进程的环境变量里注入，退出即失效 |

## 2. 内置核心

| id | 名称 | 协议 | 探测命令 | 注入的环境变量 |
| --- | --- | --- | --- | --- |
| `kb` | 知识库助手 | 应用内 | — | — |
| `claude-code` | Claude Code | `anthropic` | `claude` | `ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_MODEL` |
| `codex` | Codex CLI | `openai` / `openai-responses` | `codex` | `OPENAI_BASE_URL`、`OPENAI_API_KEY` |
| `qwen-code` | Qwen Code | `openai` | `qwen` | `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`OPENAI_MODEL` |
| `aider` | Aider | `openai` | `aider` | `OPENAI_API_BASE`、`OPENAI_API_KEY` |
| `goose` | Goose | `openai` | `goose` | `GOOSE_PROVIDER=openai`、`GOOSE_MODEL`、`OPENAI_BASE_URL`、`OPENAI_API_KEY` |

界面上只会显示本机实际能探测到的核心；没装的会显示「未安装」和缺少的命令名。
协议不兼容的模型不会出现在该核心的模型列表里（例如 Claude Code 只列
Anthropic Messages 协议的供应商）。

> CLI 自己的登录态、配置文件、权限策略仍然生效。应用只注入「这一轮用哪个模型」，
> 不会替 CLI 打开 `--dangerously-skip-permissions` 之类的开关；需要工具权限时，
> 请在插件清单的 `run_args` 里按该 CLI 的文档自行配置，并清楚这样做的风险。

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
| `kind` | 是 | 外部插件只能是 `cli`；`kb` 是内置实现，不允许插件声明 |
| `protocols` | 是 | `openai` / `openai-responses` / `anthropic` 的非空子集 |
| `bins` | 是 | 按顺序探测的**命令名**，只能写 PATH 中可查找的名字（如 `qwen`、`python`）；不接受绝对路径、相对路径、`..`、空格或 shell 元字符；解析结果若是 `.cmd/.bat` 等批处理脚本或命令解释器会被拒绝 |
| `version_args` | 否 | 探测版本用的参数，默认 `["--version"]`；失败不影响可用性 |
| `run_args` | 是 | 非交互执行参数，必须包含 `{prompt}`，不得包含 `{api_key}`，也不得把 `&`、`|`、`^`、`%VAR%` 这类 cmd 元字符/变量引用作为独立参数 |
| `env` | 否 | 注入子进程的环境变量模板，最多 32 项；键必须在 `ALLOWED_ENV_KEYS` 白名单内，不能覆盖 `PATH`/`PYTHONPATH`/`NODE_OPTIONS` 等系统或代码加载变量 |
| `output` | 否 | `text`（默认，逐行当正文）或 `jsonl`（逐行解析 JSON） |
| `text_paths` | 否 | `output=jsonl` 时提取正文的点路径，如 `["item.text","message"]` |
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
| `{app_name}` | 应用名 |

参数和环境变量都会做占位符替换；不会经过 shell，`run_args` 是数组，不拼接字符串。
应用不再为 `.cmd/.bat` 包一层 `cmd.exe /c`（那样参数会被二次解析，`& | ^ %VAR%` 可注入命令），
`Popen(shell=False)` 由 Python 自己转义参数。命令行摘要、运行日志、正文与错误信息会统一
把密钥替换成 `<密钥>`、把完整提示词替换成 `<提示词>`。

### 3.4 JSONL 输出

如果 CLI 输出结构化事件，用：

```json
{
  "output": "jsonl",
  "text_paths": ["item.text", "msg.message", "message", "text"]
}
```

应用会逐行 `json.loads`，按 `text_paths` 顺序找第一个非空字符串作为正文；
解析失败的行会作为运行日志展示，不会中断执行。Codex CLI 的
`codex exec --json` 就使用这种模式。

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
  前端每 350ms 轮询 `/api/agent/runs/{id}/events?after=` 追加文本、日志和证据。
- 点「停止」会终止子进程树（Windows 用 `taskkill /T /F`，POSIX 用进程组），
  消息收尾为 `stopped`。
- 超过 `timeout_seconds` 自动终止，消息收尾为 `failed`。
- 应用重启时，所有 `pending` 的 Agent 消息会收尾为 `failed`，不会永远转圈。
- 事件只保存在内存里，最终结果落在 `agent_messages`；历史会话刷新后仍可读。

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
| GET | `/agent/cores` | 核心列表 + 探测结果 + 插件目录 + 清单错误 |
| GET | `/agent/models?core_id=` | 模型列表，`compatible` 标记协议是否匹配 |
| POST | `/agent/plugins/rescan` | 清探测缓存并重新扫描插件目录 |
| PATCH | `/agent/plugins/{id}` | `{enabled}`：停用/启用核心 |
| GET | `/agent/sessions` | 当前管理员的 Agent 会话 |
| POST | `/agent/sessions` | `{title,core_id,model_config_id,kb_ids,workspace}` |
| GET | `/agent/sessions/{id}` | 会话详情 + 消息 + 核心信息 |
| PATCH | `/agent/sessions/{id}` | 改标题/核心/模型/知识库/工作目录 |
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

## 9. 排障

| 现象 | 原因与处理 |
| --- | --- |
| 核心显示「未安装」 | `bins` 里的命令不在 PATH；装好后点「重新扫描」 |
| 模型列表为空 | 没有与该核心协议兼容的已启用 LLM；去「模型设置」添加对应协议的供应商 |
| 启动后立刻失败 | 看消息里的 stderr；常见是 CLI 版本参数不兼容、需要登录、或工作目录不存在 |
| 输出乱码 | CLI 不是 UTF-8 输出；在清单 `env` 里加 `PYTHONIOENCODING` 或该 CLI 的编码开关 |
| 插件没出现 | 看「核心与插件」顶部的清单错误；常见是 JSON 语法、id 重复、缺 `{prompt}` |
| 清单里写了绝对路径，插件没出现 | `bins` 只能写 PATH 中的命令名；改成命令名，或把可执行文件所在目录加进 PATH |
| 装了 CLI 仍显示「未安装」 | 若 PATH 解析到 `.cmd/.bat` 批处理脚本会被安全边界拒绝；改用该 CLI 的原生可执行文件（`.exe`），或安装提供原生二进制的版本 |
| 提示「env.xxx 不在允许的白名单键内」 | 把该环境变量登记到 `ALLOWED_ENV_KEYS`，或改用白名单里已有的键 |
| 提示「工作目录必须位于 Agent 工作区内」 | 把项目目录放到 `WRK_AGENT_WORKSPACE` 下，或把 `WRK_AGENT_WORKSPACE` 指到包含它的父目录后重启 |
