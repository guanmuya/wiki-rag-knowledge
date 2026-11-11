# v1.0.20 交付说明与 Release 发布步骤

> 本文件是 **v1.0.20 的交付说明**：上半是 GitHub Release 正文草稿（可直接粘贴），
> 下半是**合并落地后**要执行的上传与校验命令。
> 发布链路的背景与风险留档见 [`docs/UPDATE.md`](UPDATE.md)；
> 构建环境的实测数字（产物大小、`latest.yml` 原文、安装包 sha512、门禁输出）见
> [`docs/evidence/release-v1.0.20-build.txt`](evidence/release-v1.0.20-build.txt)。

> **发布状态：待发布（本轮只构建，不建 tag、不上传）** —— 本任务只递增版本号、补交付说明
> 与构建记录、产出 Windows NSIS 安装包。`v1.0.20` tag 由平台在 PR 合并落地后打在 `master`
> 的合并提交上，三件资产由下一个任务用 `gh release create --verify-tag` 上传；上传前请以
> `docs/evidence/release-v1.0.20-build.txt` §5 或现场重算的值逐项核对 `latest.yml`。

- 版本：`1.0.20`（`backend/app/main.py` 的 `VERSION` 与 `frontend/package.json` 的 `version` 同步递增）
- 更新源：GitHub Releases（`guanmuya/wiki-rag-knowledge`，仓库公开）
- 资产三件套：`latest.yml`、`wiki-rag-knowledge-1.0.20-x64-setup.exe`、`wiki-rag-knowledge-1.0.20-x64-setup.exe.blockmap`
- 安装包**无代码签名证书**：Windows 首次安装会提示 SmartScreen「未知发布者」，选择「更多信息 → 仍要运行」即可；残余风险见 `docs/UPDATE.md` §6.2

---

## 1. Release 正文草稿（可直接粘贴）

```markdown
## 企业知识库 v1.0.20

Windows x64 桌面版安装包。已装 v1.0.19 及更早版本的用户可在「关于 → 软件更新 → 检查更新」里自动升级。

### 本版变化（v1.0.19 之后）

- **个人 Agent：思考过程与最终回答分开显示**：此前模型的整段推理、命令与工具尝试会一起铺进回答气泡，最终答案反而被淹没。现在推理 / 命令执行 / 文件改动等过程统一收进默认折叠的「思考过程」卡片，回答气泡只保留最终答案；想看过程再展开
- **只有推理没有答案时明确标成未完成**：整轮只产出思考过程、没给出最终回答的运行，不再被当成一次正常回答，而是明确提示「这次执行只产出了思考过程，没有给出最终回答」，并标为未完成；展开「思考过程」可看它做到哪一步
- **旧消息不重解析**：升级前已有的历史消息按落库时的内容照常显示，不会重新拆分；新口径只对升级后新产生的消息生效

### 安装

1. 下载 `wiki-rag-knowledge-1.0.20-x64-setup.exe`
2. Windows 会提示「未知发布者」（安装包未做代码签名）→ 更多信息 → 仍要运行
3. 安装向导可改安装目录；卸载**不会**删除 `%APPDATA%\wiki-rag-desktop\data` 里的知识库数据

### 已知限制

- 只支持 NSIS 安装版；`--pack` 的免安装目录不会自动更新
- 1.0.0 版本没有更新器，需手动安装一次
- 安装包未签名，自动更新只校验 `latest.yml` 的 sha512 完整性，不校验发布者身份
- 用量页的费用是「按单价估算」，未收录单价的模型会单列「未计价」，不混进总额
```

---

## 2. 发布步骤（**合并落地、`v1.0.20` tag 已在远端之后**再执行）

> 本版**不预先创建 tag**：tag 由平台在 PR 合并后打在 `master` 的合并提交上。
> `electron-builder --publish always` 在 release 不存在时会**自己建 release 并连带打 tag**，
> 因此这里不用它，改用 `gh release create --verify-tag`——tag 不存在时它直接失败，不会误建。

### 2.1 前提

1. PR 已合并到 `master`，远端已存在 `v1.0.20` tag，且 tag 指向合并提交：

   ```powershell
   git ls-remote --tags origin | Select-String 'v1\.0\.20'
   gh api repos/guanmuya/wiki-rag-knowledge/git/ref/tags/v1.0.20
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
   > 换工作区后产物必然重建，**sha512 与体积会变**，上传前请以
   > `docs/evidence/release-v1.0.20-build.txt` §5 或现场重算的值为准，并逐项核对 `latest.yml`。

### 2.2 创建 Release 并上传三个资产

**Release 正文只取 §1 的 markdown 代码块，不要传整份文件**：本文件 §2 的上传命令与 §3 的
内部说明不适合对外（上一版发布实测即按此口径，发布前后逐字比对过正文与 §1 相同）。
先把 §1 代码块另存为无 BOM 的 UTF-8 临时文件，再把它交给 `--notes-file`：

```powershell
# 在仓库根目录；提取 §1 的 ```markdown 代码块正文（不含围栏），写成无 BOM 的 UTF-8
$doc = Get-Content -Raw -Encoding utf8 docs/RELEASE_NOTES_v1.0.20.md
$m = [regex]::Match($doc, '(?s)## 1\. Release 正文草稿.*?```markdown\r?\n(.*?)\r?\n```')
if (-not $m.Success) { throw '未找到 §1 的 markdown 代码块' }
[System.IO.File]::WriteAllText((Join-Path (Get-Location) '.tmp-release-body.md'),
  $m.Groups[1].Value, (New-Object System.Text.UTF8Encoding($false)))

# --verify-tag 保证 tag 已存在才发布，不会顺手建 tag
gh release create v1.0.20 --verify-tag `
  --title "v1.0.20" `
  --notes-file .tmp-release-body.md `
  frontend/release/updates/latest.yml `
  frontend/release/updates/wiki-rag-knowledge-1.0.20-x64-setup.exe `
  frontend/release/updates/wiki-rag-knowledge-1.0.20-x64-setup.exe.blockmap
```

等价写法（Release 已存在、只补传资产）：

```powershell
gh release upload v1.0.20 --clobber `
  frontend/release/updates/latest.yml `
  frontend/release/updates/wiki-rag-knowledge-1.0.20-x64-setup.exe `
  frontend/release/updates/wiki-rag-knowledge-1.0.20-x64-setup.exe.blockmap
```

### 2.3 发布后必做的三项校验

1. **三个资产都在**：

   ```powershell
   gh release view v1.0.20 --json tagName,isDraft,assets --jq '.tagName, .isDraft, (.assets[] | "\(.name) \(.size)")'
   ```

2. **匿名可下载**（`latest.yml`、安装包、`.blockmap` 三个都应是 `200`；仓库是公开的，不需要令牌）：

   ```powershell
   $base = 'https://github.com/guanmuya/wiki-rag-knowledge/releases/download/v1.0.20'
   foreach ($f in @('latest.yml','wiki-rag-knowledge-1.0.20-x64-setup.exe','wiki-rag-knowledge-1.0.20-x64-setup.exe.blockmap')) {
     $code = curl.exe -sIL -o NUL -w '%{http_code}' "$base/$f"
     "$f -> $code"
   }
   ```

   > 注意：首次上传后 GitHub 的 `release-assets.githubusercontent.com` 有 1–2 分钟 CDN 延迟，
   > 刚上传就 `404` 属正常，等一会儿再验（见 `docs/UPDATE.md` §2.1 的实测记录）。

3. **`latest.yml` 与资产对得上**：`latest.yml` 里的 `version` 必须是 `1.0.20`，
   `files[].url` / `path` 必须与上传的安装包**文件名完全一致**，`sha512` 与本地构建出的
   安装包一致（本轮构建的值见 `docs/evidence/release-v1.0.20-build.txt` §5；
   若发布前重建过安装包，以发布那次现场重算的值为准）：

   ```powershell
   curl.exe -sL https://github.com/guanmuya/wiki-rag-knowledge/releases/download/v1.0.20/latest.yml
   ```

### 2.4 客户端侧确认（可选，但测过才算闭环）

用已装 v1.0.19 的机器打开「关于 → 软件更新 → 检查更新」，应检出 1.0.20 并下载；
下载完成状态应变成「待安装」，主进程日志无 `update:error`。
判据与 v1.0.2 那次实测相同（`docs/UPDATE.md` §2.1）。

---

## 3. 本轮构建没做的事（后续在发布步骤补上）

- **没有打 tag**（构建轮）：`v1.0.20` 由平台在合并落地后创建，构建轮不建。
- **没有上传 Release**（构建轮）：tag 还不存在时先建 Release 会把 tag 指到错误提交；
  上传动作按 §2 在 tag 出现后执行。
- **没有代码签名**：见 `docs/UPDATE.md` §6.2，属外部凭据，需用户单独授权。
- **业务代码零改动**：本版只 bump 版本号三处、补交付说明与构建记录、构建安装包；
  `backend/app/**`、`frontend/src/**`、`frontend/electron/**` 的逻辑一行未动
  （`backend/app/main.py` 只有 `VERSION` 一行）。本版的用户可感知改动（#32 思考过程与
  回答分区）已在此前合入 `master`。
