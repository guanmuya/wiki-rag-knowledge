# 模型参考单价与费用估算

> 目标：让「用量概览」能给出**参考金额**，同时明确它不是账单。
> 事实源是代码：内置目录 `backend/app/services/price_catalog.py`、
> 匹配与取价 `backend/app/services/pricing.py`、接口见 `docs/API.md` §8（`/api/settings/pricing`）
> 与 §7.2（`/api/usage` 的 `cost`）。

## 1. 口径

- **币种与单位**：`USD`、每 100 万 token（`per_1m_tokens`）。查到的是别的币种时不换算——换汇率要维护汇率来源与更新时间，不在本轮范围。
- **只算生成 token**：`输入价 × prompt_tokens + 输出价 × completion_tokens ÷ 1e6`。视觉输入与生成同口径（都记在 `prompt_tokens` / `completion_tokens` 里）。
- **向量 token 一律未计价**：`CostRecord`、`Task.result`、`Message.route` 里都没有保存「哪条向量模型产生的 token」，拿生成模型的价去估会系统性算错。所以 `embed_tokens` 只进 `cost.unpriced_tokens`。
- **缓存 token 不参与**：公开价目页的缓存读价（如 `0.10`）只写进条目的 `note` 作为参考，不参与计算。要算缓存价得先有缓存 token 的埋点。
- **参考价，不是账单**：同一模型不同渠道价格可能差一个数量级（见 §4），实际扣费以供应商账单为准。接口固定返回 `reference: true` 与一段 `note`，界面应原样展示。

## 2. 内置目录与来源

`price_catalog.py` 的 `SOURCES` 列出本轮查证用到的价目页；`CATALOG` 每条都必须带
`source`、`source_url`、`verified_at`（`YYYY-MM-DD`），并且 `source_url` 必须落在
`SOURCES` 登记的站点下——`backend/tests/test_model_prices.py` 会逐条校验，缺来源的
条目直接让测试失败。

当前收录的价目页：

| 来源 | 地址 | 覆盖 |
| --- | --- | --- |
| OpenAI 官方价目表（标准档、短上下文） | <https://developers.openai.com/api/docs/pricing> | GPT / Embedding 系列 |
| Vercel AI Gateway 模型页 | <https://vercel-docs.vercel.sh/ai-gateway/models/> | 网关侧的 `anthropic/*`、`google/*`、`zai/*`、`deepseek/*` 等 |

**收录原则**：只录查得到公开价、说得清来源的模型。查不到就**不录**——宁可让用量页显示
「未计价」，也不编一个数字。Embedding 模型按「仅输入计价」录入（`output_per_1m = 0`），
`0` 在这里表示「没有输出 token」，不是「免费」。

## 3. 匹配规则（用户模型 ID ↔ 公开目录 ID）

用户配置里的模型标识是自由字符串（`kz/deepseek-v4.1-flash`），公开目录写的是完整 ID
（`deepseek/deepseek-v4.1-flash-1017`）。`pricing.normalize_model_key()` 把两边归一化：

1. 转小写、去首尾空白；
2. 取**最后一个 `/` 之后**的部分（丢掉 `kz/`、`deepseek/`、`openai/` 这类前缀）；
3. `_` 换成 `-`；
4. 去掉**日期结尾** `-(19|20)YY-MM-DD$`；
5. 去掉**纯数字结尾** `-\d{4,8}$`（如 `-1017`）。

只做**归一化后的精确相等**，不做子串、前缀或相似度匹配：`gpt-4o` 绝不能命中
`gpt-4o-mini`，`gpt-6` 也不能命中 `gpt-6-astra`。也不剥 `-v4.1` 这类段——那可能就是
模型名字本身。

`aliases` 用于登记「同一模型的另一种写法」（例如 `deepseek/deepseek-v4.1-flash`）。
如果两条**不同条目**归一化后撞同一个键，`pricing.catalog_index()` 直接抛错、测试失败：
这种情况只能由维护者合并条目或写显式 `aliases` 解决，静默取第一条会让金额取决于字典顺序。

## 4. 已知的不一致案例

| 模型 | 来源 A | 来源 B |
| --- | --- | --- |
| `deepseek/deepseek-v4.1-flash-1017` | 网关模型页：输入 `0.022` / 输出 `0.294` | 网关目录接口（`/v1/models/...`）：输入 `0.30` / 输出 `1.20`、缓存读 `0.007`，并标注 `varies_by_provider` |

两个数差一个数量级。目录只录一个（模型页的数值），另一个写进条目的 `note`；
金额仅供参考，实际以供应商账单为准。遇到这种模型，正确做法是让管理员在
`/api/settings/pricing` 里为**自己的那个模型配置**填价——用户价优先于内置价。

## 5. 取价优先级

1. **用户单价**：`model_prices` 表里同一 `model_config_id` 的行（管理员通过
   `PUT /api/settings/pricing/{model_config_id}` 填写）。**行存在**即代表填过价，
   填 `0` 也是合法价格（表示这个渠道不额外计费）；「没填」= 没有这一行。
2. **内置参考价**：按 §3 匹配 `price_catalog.CATALOG`。
3. **未计价**：两者都没有。该行进 `cost.unpriced_records`、其 token 进 `cost.unpriced_tokens`。

为什么用户价挂在**模型配置**上而不是模型名字上：同一个模型 ID 在不同供应商、不同
渠道的计价可以完全不同，只有「模型配置」这一层能区分它们。独立建 `model_prices`
表而不是给 `model_configs` 加两列，也是为了区分「没填价」和「填了 0」——
老库补列会自动给新 Float 列加 `DEFAULT 0`。

## 6. 维护流程

1. 维护时人工打开 §2 的价目页，核对要新增/修改的模型与数值（只在这一步联网）。
2. 改 `backend/app/services/price_catalog.py`：新增条目要写全 `model_id`、`input_per_1m`、
   `output_per_1m`、`source`、`source_url`、`verified_at`；同一模型有多个来源时按 §4 处理。
3. 跑 `python -m pytest backend/tests/test_model_prices.py -q`：字段完整性、来源归属、
   日期格式、归一化键唯一性、匹配规则（含「不命中」的反例）全部有断言。
4. 如改动影响用户可见口径，同步更新本文件与 `docs/API.md` §8 的字段说明。

CI（`.github/workflows/ci.yml`）与运行时**都不联网**：目录是随代码发布的常量，
进程内只做一次 `catalog_index()` 缓存。这也是选 Python 模块而不是 JSON 数据文件的原因——
桌面版用 PyInstaller 打包后端（`frontend/scripts/build-backend.mjs`），打包命令没有
`--add-data`，外部数据文件进不了产物。

## 7. 未计价与已知缺口

- 向量 token（知识库处理、检索查询向量化）全部未计价，见 §1。
- 没有保存模型的行一律未计价：`CostRecord`（历史上没有模型字段）、无成本流水的旧
  `Task.result`、以及升级前没有写 `model_config_id` 的旧问答消息。
- 失败/中止的调用：个人 Agent 只统计 `status=ok` 的执行；失败执行消耗的 token 也没有落库。
- 缓存 token、首字耗时、输出速度：没有埋点，接口不返回。
- 个人 Agent 的调用数按「每次执行 1 次调用」记，CLI 内部真实发起的模型请求数不可见。
