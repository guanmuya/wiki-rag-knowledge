# v1.0.18 交付说明与 Release 发布步骤

> 本文件是 **v1.0.18 的交付说明**：上半是 GitHub Release 正文草稿（可直接粘贴），
> 下半是**合并落地后**要执行的上传与校验命令。
> 发布链路的背景与风险留档见 [`docs/UPDATE.md`](UPDATE.md)；
> 构建环境的实测数字（产物大小、`latest.yml` 原文、安装包 sha512、门禁输出）见
> [`docs/evidence/release-v1.0.18-build.txt`](evidence/release-v1.0.18-build.txt)。

- 版本：`1.0.18`（`backend/app/main.py` 的 `VERSION` 与 `frontend/package.json` 的 `version` 同步递增）
- 更新源：GitHub Releases（`guanmuya/wiki-rag-knowledge`，仓库公开）
- 资产三件套：`latest.yml`、`wiki-rag-knowledge-1.0.18-x64-setup.exe`、`wiki-rag-knowledge-1.0.18-x64-setup.exe.blockmap`
- 安装包**无代码签名证书**：Windows 首次安装会提示 SmartScreen「未知发布者」，选择「更多信息 → 仍要运行」即可；残余风险见 `docs/UPDATE.md` §6.2

---

## 1. Release 正文草稿（可直接粘贴）

```markdown
## 企业知识库 v1.0.18

Windows x64 桌面版安装包。已装 v1.0.17 及更早版本的用户可在「关于 → 软件更新 → 检查更新」里自动升级。

### 本版变化（v1.0.17 之后）

- **工程门禁**：CI 从无到五项检查（后端全量测试、前后端契约一致性、前端类型检查、前端 Web 构建、前端自动化测试），任一失败阻断合并
- **前后端契约校验**：新增 `pnpm check:contract`，把 `schemas.py` / 路由装饰器 / `preload.cjs` 暴露面与 `types.ts`、`docs/API.md` 对齐
- **后端**：`kbs` 路由按职责拆成 documents / tasks / cost 三个模块；`llm.py`、`kbs.py` 拆分，大文件收敛
- **安全与可观测性（P1-8）**：强制首次改密、登录失败限流（账号 5 次 / 来源 20 次）、上传文件魔数校验、Windows 密钥文件权限收紧、静默异常补日志、迁移账本 `schema_migrations`
- **前端**：统一请求封装（超时 / 401 / 错误语义集中一处）、大页面与重复文案收敛
- **仓库治理**：历史看板与截图归档、补齐 `LICENSE` / `CHANGELOG` / `CONTRIBUTING` / `.editorconfig`、一次性脚本收进 `scripts/dev-only/`
- **文档口径统一**：新增 `docs/FACTS.md` 作为唯一数值口径（表数、`WRK_*` 可调项、测试基线、代码锚点）

### 安装

1. 下载 `wiki-rag-knowledge-1.0.18-x64-setup.exe`
2. Windows 会提示「未知发布者」（安装包未做代码签名）→ 更多信息 → 仍要运行
3. 安装包为向导式，可改安装目录；卸载**不会**删除 `%APPDATA%\wiki-rag-desktop\data` 里的知识库数据

### 已知限制

- 只支持 NSIS 安装版；`--pack` 的免安装目录不会自动更新
- 1.0.0 版本没有更新器，需手动安装一次
- 安装包未签名，自动更新只校验 `latest.yml` 的 sha512 完整性，不校验发布者身份
```

---

## 2. 发布步骤（**合并落地、`v1.0.18` tag 已在远端之后**再执行）

> 本版**不预先创建 tag**：tag 由平台在 PR 合并后打在 `master` 的合并提交上。
> `electron-builder --publish always` 在 release 不存在时会**自己建 release 并连带打 tag**，
> 因此这里不用它，改用 `gh release create --verify-tag`——tag 不存在时它直接失败，不会误建。

### 2.1 前提

1. PR 已合并到 `master`，远端已存在 `v1.0.18` tag，且 tag 指向合并提交：

   ```powershell
   git ls-remote --tags origin | Select-String 'v1\.0\.18'
   gh api repos/guanmuya/wiki-rag-knowledge/git/ref/tags/v1.0.18
   ```

2. 本机有 GitHub 凭据。探测方式（**不要把 token 贴进聊天或写进任何文件**）：

   ```powershell
   gh auth status          # 期望：Logged in to github.com account guanmuya
   if ($env:GH_TOKEN) { 'GH_TOKEN 存在' } else { 'GH_TOKEN 不存在' }
   ```

3. 安装包三件套已构建在 `frontend/release/updates/`（该目录被 `.gitignore` 忽略，不入库）：

   ```powershell
   Get-ChildItem frontend/release/updates
   ```

   > 换机器重新构建：`cd frontend; node scripts/build.mjs`（需要 Node 22+/pnpm、可联网下载 Electron 与 NSIS 工具链、
   > 以及装了 `backend/requirements-build.txt` 的 Python 环境，默认取仓库根 `.venv`，可用 `WRK_PYTHON` 覆盖）。

### 2.2 创建 Release 并上传三个资产

```powershell
# 在仓库根目录；--verify-tag 保证 tag 已存在才发布，不会顺手建 tag
gh release create v1.0.18 --verify-tag `
  --title "v1.0.18" `
  --notes-file docs/RELEASE_NOTES_v1.0.18.md `
  frontend/release/updates/latest.yml `
  frontend/release/updates/wiki-rag-knowledge-1.0.18-x64-setup.exe `
  frontend/release/updates/wiki-rag-knowledge-1.0.18-x64-setup.exe.blockmap
```

等价写法（Release 已存在、只补传资产）：

```powershell
gh release upload v1.0.18 --clobber `
  frontend/release/updates/latest.yml `
  frontend/release/updates/wiki-rag-knowledge-1.0.18-x64-setup.exe `
  frontend/release/updates/wiki-rag-knowledge-1.0.18-x64-setup.exe.blockmap
```

### 2.3 发布后必做的三项校验

1. **三个资产都在**：

   ```powershell
   gh release view v1.0.18 --json tagName,isDraft,assets --jq '.tagName, .isDraft, (.assets[] | "\(.name) \(.size)")'
   ```

2. **匿名可下载**（`latest.yml`、安装包、`.blockmap` 三个都应是 `200`；仓库是公开的，不需要令牌）：

   ```powershell
   $base = 'https://github.com/guanmuya/wiki-rag-knowledge/releases/download/v1.0.18'
   foreach ($f in @('latest.yml','wiki-rag-knowledge-1.0.18-x64-setup.exe','wiki-rag-knowledge-1.0.18-x64-setup.exe.blockmap')) {
     $code = curl.exe -sIL -o NUL -w '%{http_code}' "$base/$f"
     "$f -> $code"
   }
   ```

   > 注意：首次上传后 GitHub 的 `release-assets.githubusercontent.com` 有 1–2 分钟 CDN 延迟，
   > 刚上传就 `404` 属正常，等一会儿再验（见 `docs/UPDATE.md` §2.1 的实测记录）。

3. **`latest.yml` 与资产对得上**：`latest.yml` 里的 `version` 必须是 `1.0.18`，
   `files[].url` / `path` 必须与上传的安装包**文件名完全一致**，`sha512` 与本地构建出的
   安装包一致（本地值见 `docs/evidence/release-v1.0.18-build.txt`）：

   ```powershell
   curl.exe -sL https://github.com/guanmuya/wiki-rag-knowledge/releases/download/v1.0.18/latest.yml
   ```

### 2.4 客户端侧确认（可选，但测过才算闭环）

用已装 v1.0.17 的机器打开「关于 → 软件更新 → 检查更新」，应检出 1.0.18 并下载；
下载完成状态应变成「待安装」，主进程日志无 `update:error`。
判据与 v1.0.2 那次实测相同（`docs/UPDATE.md` §2.1）。

---

## 3. 本版没做的事（如实记录）

- **没有在构建阶段上传 Release**：本机环境里没有 `GH_TOKEN` / `GITHUB_TOKEN`（`gh auth status` 显示本机确有
  已登录的 `guanmuya` 账号，但那是操作者本机的凭据，发布动作留给人工执行时再取用），
  且 `v1.0.18` tag 需等合并后才存在——先建 Release 会连带把 tag 打到非合并提交上，明确不做。
- **没有打 tag**：`v1.0.18` 由平台在合并落地后创建。
- **没有代码签名**：见 `docs/UPDATE.md` §6.2，属外部凭据，需用户单独授权。
- 业务代码零改动：本版只 bump 版本号、补交付说明、构建安装包。
