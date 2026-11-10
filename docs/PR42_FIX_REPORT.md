# PR #42 草稿修复说明与逐项验证结果

本文件记录「修复 PR #42 草稿的 Wiki 来源追踪遗留问题并通过验证」这一轮的改动与实测结果。
按 `docs/EXPERIMENT.md:3` 的约定：凡未能实测的一律标注「待实验」，不填推测值。

## 一、本轮范围与基线

- 任务：在 PR #42「未完成：Wiki 来源追踪与删除后可见性」草稿已有的改动上修复遗留问题并通过验证。
- 基线：PR #42 分支 `agent/parallel-delegation-86962b0ee6a2` 相对 master 的 3 个提交、19 个文件（+2978 / -104），已在本工作分支的 checkpoint 提交 `98e83aa` 中。本轮在该基线上继续修改，不是从 master 重新实现。
- 本轮**没有合并 PR #42**，也没有改动它的 open / draft 状态；是否合并留待用户根据修复结果决定。
- 失败轮留下的 3 个诊断文件（根目录 `_ui_failures.txt`、`backend/_failures.txt`、`frontend/scripts/_debug-check4.mjs`）已从本分支删除，不作为交付内容。

## 二、修复内容

### 1. 旧证据 ID 复用导致已隐藏页面重新出现（后端）

- 根因：`backend/app/wiki_visibility.py` 的 `has_deleted_source()` 对旧页面（只有 `WikiEvidence`、没有 `WikiGenerationSource`）用裸整数 `WikiEvidence.doc_id` 直接 inner join `Document`，只看 `Document.deleted`。来源文档被物理删除后，SQLite 会把这个 ID 分给新文档；新文档 `deleted=false`，谓词即失效，已隐藏页面重新可见。
- 修复：改用证据上的 `chunk_id` 外键作为稳定身份。删除文档时 `kbs.delete_document()` 会物理删除该文档的分块，`WikiEvidence.chunk_id` 被 `ON DELETE SET NULL` 置空；即使 ID 被复用也永远不会自动恢复。分块仍在时，再通过 `Chunk.doc_id` 关联 `Document`，使软删除判定依旧可靠。判定条件为：`chunk_id IS NULL`、或分块不存在、或归属文档 `deleted=true`。
- 影响文件：`backend/app/wiki_visibility.py`。
- 对应回归：`test_legacy_evidence_only_page_stays_hidden_after_document_id_reuse`。

### 2. 管理员证据误显示另一个知识库的文件名（后端）

- 根因：`backend/app/routers/wiki.py` 的 `wiki_evidence()` 用裸 `doc_id` 批量查 `Document.filename`。原文档被物理删除、ID 被**另一个知识库**的文档复用后，证据里会显示那个文档的文件名，等于泄露别的库的文件名（回归测试里是 `other-kb-private-document-name.md`）。
- 修复：只有当证据仍指向一个活着的分块，且该分块记录的 `doc_id` / `kb_id` 与证据行一致时，才拿它去查文件名；否则 `doc_name` 留空。已删除来源的名称继续由管理员专用的 `deleted_generation_sources` 展示（它基于稳定的 `doc_ref_id`），不靠裸 `doc_id` 反查。
- 影响文件：`backend/app/routers/wiki.py`。
- 对应回归：`test_cross_shard_citation_stays_hidden_when_document_id_reused_in_another_kb`。

### 3. 旧草稿提交重新公开已删除内容（后端 + 前端）

- 根因：`update_wiki_page()` 只从**当前版本**继承来源依赖。用户保留了基于旧版本 v1（含已删除来源）的草稿，页面随后被用有效来源重新生成成 v2；提交旧草稿会生成只继承 v2 安全依赖的人工版本，于是已删除内容重新公开。编辑接口完全没有草稿基底的并发校验。
- 修复（两层）：
  1. `WikiPageUpdateIn` 新增可选字段 `expected_version_id`；带上时不等于 `page.current_version_id` 就返回 409「版本冲突」。前端 `wikiApi.save()` 发送 `detail.version.id`，`WikiTab.save()` 传入该值，并在保存期间冻结两个输入框。
  2. 兼容旧调用方：不携带版本令牌时，后端按正文逐字比对「来源已失效」的历史版本，命中即同样返回 409，拒绝把缓存的旧草稿重新发布。
- 影响文件：`backend/app/schemas.py`、`backend/app/routers/wiki.py`、`frontend/src/api.ts`、`frontend/src/components/WikiTab.tsx`、`docs/API.md`（补充契约）。
- 对应回归：`test_manager_cannot_republish_cached_pre_delete_text_after_clean_rebuild`；新增 `test_expected_version_id_rejects_a_stale_edit` 覆盖新令牌的 409 / 200 两条路径。

### 4. 界面自动复查的 5 项场景（前端）

全部集中在 `frontend/src/components/WikiTab.tsx` 的后台复核逻辑：

- **旧当前版本、历史正文、关联标题残留**：后台复核原先只把详情存进 `verifiedDetail`，仅在页面曾被撤销时才用于恢复，页面仍可见时完全不刷新正文。现在复核发现 `version.id` 变化时刷新详情、草稿基准（仅非编辑态）并清空旧版本历史视图；关联 chip 的标题改为取最新目录中的当前标题，不再信任详情响应里的旧标题。
- **历史列表变化后仍展示旧版本正文**：复核时若版本历史弹窗打开，会重新拉取 `/versions`；已不在返回列表里的 `viewVersion` 会被清掉，避免继续展示旧正文。
- **撤销后换版本恢复时旧草稿重显**：新增 `revokedVersionIdRef` 记录撤销时的版本 ID。恢复时若返回的是**不同版本**，丢弃旧草稿并退出编辑态；同一版本恢复才保留草稿（对应「放弃未保存的修改？」确认流程）。
- **管理员降权未复核**：原逻辑 `if (isAdmin) return` 让管理员完全不参与复核。现在管理员同样参与 5 秒复核；「目录成功返回但不再列出当前页」对任何角色都撤销，而「整个目录/详情请求失败（403 / 网络错误）」仍按瞬时故障处理、不隐藏缓存内容，避免误伤管理员复核视图。
- **保存期间输入未冻结、未带版本令牌**：两个 `TextArea` 均加 `disabled={saving}`；保存请求带 `expected_version_id`。

### 5. 验证脚本中的一处断言修正（需明确说明）

`frontend/scripts/verify-wiki-source-revocation.mjs` 原有一句 `assert.equal(window.__pollCount(), 0)`，注释为「System administrators are not polled」。这句断言把**草稿尚未修复的行为**写成了期望：管理员完全不复核，正是 PR 正文列出的「管理员降权未正确处理」。修复后管理员需要参与复核，该断言改为 `1`，并同步改注释。

这是把草稿里编码旧行为的断言改成修复后的真实行为；5 项界面检查、后续全部场景断言和 XSS 转义检查**没有删减、没有放宽**。本项在报告中单独列出，便于审阅者判断。

### 6. 新增回归测试

`backend/tests/test_wiki_generation_visibility.py` 末尾新增 `test_expected_version_id_rejects_a_stale_edit`：验证错误的 `expected_version_id` 返回 409 且不新增版本，正确的返回 200 并生成新版本。该用例在修复前的代码上会失败（旧 schema 会忽略未知字段并返回 200）。

## 三、逐项验证结果（本轮实测）

执行环境：Python 3.13.11、fastapi 0.162.0、SQLAlchemy 2.1.8、pydantic 2.14.1、pytest 9.2.0；Node v24.18.0、pnpm 12.3.4、Playwright 1.64.1（chromium-1248 已安装）。前端依赖本轮通过 `pnpm install --frozen-lockfile` 补齐。

| 检查项 | 命令 | 实际输出 | 结论 |
|---|---|---|---|
| 三项真实后端安全回归 | `python -m pytest -q -p no:cacheprovider tests/test_wiki_generation_visibility.py` | `10 passed in 3.46s`（该文件含草稿原有 9 项，其中 3 项为回归；本轮新增 1 项） | **实测通过** |
| PR 正文记录的 42 项后端命令 | `python -m pytest -q -p no:cacheprovider tests/test_wiki_generation_source_migration.py tests/test_wiki_input_source_race.py tests/test_wiki_initial_delete_race.py` | `42 passed in 10.26s` | **实测通过** |
| 完整后端测试（草稿 head 未跑过） | `python -m pytest -q -p no:cacheprovider` | `618 passed, 1 skipped in 125.53s (0:02:05)` | **实测通过**（离线配置） |
| 界面撤销验证（5 项场景 + 后续全部场景） | 在 `frontend/` 下 `node scripts/verify-wiki-source-revocation.mjs` | `Wiki revocation UI checks passed: tree, detail, history, drafts, restoration, races, failures and admin`，退出码 0 | **实测通过** |
| 前端构建 | 在 `frontend/` 下 `pnpm run build:web` | `tsc -b && vite build`：`✓ 65 modules transformed` / `✓ built in 1.09s`，退出码 0 | **实测通过** |
| PR #42 分支的 GitHub Actions / check runs | `workflow_runs` 按分支 `agent/parallel-delegation-86962b0ee6a2` 查询 | `No workflow runs ... matching branch` | **未实测**：该分支没有任何 CI 记录，不能当作检查通过 |
| 合并 PR #42 | — | 未执行 | 按约束留给用户决定 |

说明：本轮所有 pytest 用例均在离线配置下运行（模型响应与向量被测试桩替代，不需要在线模型凭据），因此「完整后端测试」的结论只覆盖离线可复现的行为。

## 四、未实测 / 待实验 与已知限制

以下各项**没有在本轮实测**，按 `docs/EXPERIMENT.md:3` 标注「待实验」，不填任何推测数字：

- 在线模型配置（配置 B）下的生成、来源追踪与删除后可见性：本机无在线模型配置与 API Key，未实测 → **待实验**。
- 任何依赖真实在线模型调用的端到端场景（含模型调用期间删除文档的实际服务端行为）→ **待实验**。
- GitHub Actions / check runs 与提交状态：PR #42 分支至今没有检查记录；排队中的 Cursor 检查套件无结论，不计为通过 → **未实测**。
- 合并后的集成行为（合并到 master 之后的表现）：本轮未合并 → **未实测**。

以下为 PR 正文列出的**设计限制**，本轮不改、也无法靠代码反推，如实保留：

- 升级前未记录的模型输入无法可靠反推；
- 已保存的会话、回答、任务记录不做回溯清理；
- 模型调用过程中被删除的内容可能已经发送给模型。

本草稿没有解除对敏感知识库的既有访问限制；本轮修复方向均为「更保守地隐藏」，未放宽任何访问判定。

## 五、复现脚本与路径清单（回答待明确问题）

| 类别 | 路径 | 复现命令 |
|---|---|---|
| 3 项后端安全回归 | `backend/tests/test_wiki_generation_visibility.py`：`test_legacy_evidence_only_page_stays_hidden_after_document_id_reuse`、`test_manager_cannot_republish_cached_pre_delete_text_after_clean_rebuild`、`test_cross_shard_citation_stays_hidden_when_document_id_reused_in_another_kb` | 在 `backend/` 下 `python -m pytest -q -p no:cacheprovider tests/test_wiki_generation_visibility.py` |
| 5 项界面场景 | `frontend/scripts/verify-wiki-source-revocation.mjs`（`check()` 中的 5 项，外加后续目录/详情/历史/草稿/竞态/管理员场景） | 在 `frontend/` 下 `node scripts/verify-wiki-source-revocation.mjs` |
| 新版本令牌契约 | `backend/tests/test_wiki_generation_visibility.py::test_expected_version_id_rejects_a_stale_edit` | 同上 pytest 命令 |

是否需要补齐：3 项后端回归与 5 项界面场景的复现脚本**已存在、无需新建**；本轮补齐的是 1 个新字段的回归用例，以及修正验证脚本中 1 处与修复目标自相矛盾的断言。除此之外没有发现缺失的复现路径。

## 六、交付与分支说明（含被搁置的约束条款）

- 本轮改动落在会话分支 `tds/conv-01a1c5ee-1a9f-76b1-bf85-a340be5d73ed`，由平台在回合结束时提交并推送，形成该分支的变更评审。
- 需明确搁置两条执行约束并说明原因：
  1. 「把修复结果推回 PR #42 现有分支 `agent/parallel-delegation-86962b0ee6a2`」：本会话工作区的远程推送由平台统一处理，且只针对本会话分支 `tds/conv-*`；工作区未获授权向 PR #42 分支推送，且平台规则禁止在此工作区切换到其他分支。本轮做过一次 `git push --dry-run` 探测，结果是 `non-fast-forward`（本分支基于较新的 master，不是 PR #42 head 的后代），若要写入只能强制改写该 PR 的历史，属于未经用户明确授权、且超出本工作区推送流程的动作，因此**没有执行**。PR #42 仍是 open、draft、未被合并或改动。
  2. 「不另起 PR」：平台为每个会话分支自动建立评审入口，这不在本构建可控范围内，故同样无法保证；本构建没有手动新建任何 PR。
- 若要让原 PR #42 反映修复后的状态，需由用户（或持有该仓库写权限凭据的一方）把本会话分支的改动落到 `agent/parallel-delegation-86962b0ee6a2`，或决定直接合并本会话分支。合并与否本轮不代劳。

## 七、结论

PR #42 草稿自报的 3 项后端安全回归与 5 项界面场景，本轮已全部修复并在本机离线环境下实测通过；草稿未跑过的完整后端测试也补跑通过（618 passed / 1 skipped）。仍未实测的在线模型、CI 与合并后行为已按约定标注「待实验」，PR 正文的设计限制如实保留。本草稿仍不应在合并决策前被当作已完成交付，是否合并由用户决定。
