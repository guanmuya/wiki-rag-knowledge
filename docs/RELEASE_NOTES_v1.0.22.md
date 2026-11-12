# v1.0.22 交付说明与 Release 发布步骤

> 本文件是 **v1.0.22 的交付说明**：上半是 GitHub Release 正文草稿（可直接粘贴），
> 下半是**合并落地后**要执行的上传与校验命令。
> 发布链路的背景与风险留档见 [`docs/UPDATE.md`](UPDATE.md)；
> 构建环境的实测数字（产物大小、`latest.yml` 原文、安装包 sha512、门禁输出）见
> [`docs/evidence/release-v1.0.22-build.txt`](evidence/release-v1.0.22-build.txt)。

> **发布状态：已发布（2026-11-12 04:10 UTC）** —— Release 页面
> <https://github.com/guanmuya/wiki-rag-knowledge/releases/tag/v1.0.22>；
> `v1.0.22` tag 由平台创建并打在 `master` 的合并提交 `0601afd` 上，三件资产已上传、
> 匿名可下载（均 200），GitHub 列表已标 `Latest`。发布的是**重建件**：体积与 sha512
> 与构建记录 `release-v1.0.22-build.txt` §5 那轮不同，属正常。本次发布的前置核对、上传与
> 三项校验的实测数据见
> [`docs/evidence/release-v1.0.22-publish.txt`](evidence/release-v1.0.22-publish.txt)
> （该文件是发布时的权威数字）；构建轮的门禁实跑输出仍见
> [`docs/evidence/release-v1.0.22-build.txt`](evidence/release-v1.0.22-build.txt)。

- 版本：`1.0.22`（`backend/app/main.py` 的 `VERSION` 与 `frontend/package.json` 的 `version` 同步递增）
- 更新源：GitHub Releases（`guanmuya/wiki-rag-knowledge`，仓库公开）
- 资产三件套：`latest.yml`、`wiki-rag-knowledge-1.0.22-x64-setup.exe`、`wiki-rag-knowledge-1.0.22-x64-setup.exe.blockmap`
- 安装包**无代码签名证书**：Windows 首次安装会提示 SmartScreen「未知发布者」，选择「更多信息 → 仍要运行」即可；残余风险见 `docs/UPDATE.md` §6.2

---

## 1. Release 正文草稿（可直接粘贴）

```markdown
## 企业知识库 v1.0.22

Windows x64 桌面版安装包。已装 v1.0.21 及更早版本的用户可在「关于 → 软件更新 → 检查更新」里自动升级。

### 本版变化（v1.0.21 之后）

- **新增深色外观与「跟随系统」**：侧栏底部、用户信息上方多了一个「外观」三档开关
  ——**跟随系统 / 浅色 / 深色**。默认「跟随系统」，系统在浅色与深色之间切换时界面**实时**跟着变，
  不用刷新、不用重启；点过的档位记在本机，下次打开还是它
- **深色下不闪白、窗口一起变**：第一帧就按档位上色（深色下不会先闪一帧白），窗口底色、
  原生控件（下拉弹层、滚动条、复选框）也跟着走；下拉框箭头、输入框、主按钮在深色下都重新配过色，
  正文对比度 14.9:1，没有一档掉到 4.5:1 以下
- **浅色观感与上一版一致**：浅色档的配色取值一个都没改，唯一可见的新东西就是侧栏底部这个开关；
  登录页与初始化页没有开关，但照样跟随已保存的档位
- **勾选框旁边的文字恢复正常横排**：上一版把复选框的宽高压到了包住文字的整行上，
  花名册里的勾选项被挤成了竖排；本版复选框样式只作用于勾选框本身，文字回到它右边
- **下拉框统一并跟随主题**：用户页两处原生下拉框改用全站共用的那个控件，箭头颜色跟着主题走
  （深色下不再是浅底上的死灰色）
- **用量页删掉一行多余说明**：「个人 Agent 按核心」表上方那行灰字口径说明已移除，
  页面更干净，表格标题与无障碍关联都保留

### 安装

1. 下载 `wiki-rag-knowledge-1.0.22-x64-setup.exe`
2. Windows 会提示「未知发布者」（安装包未做代码签名）→ 更多信息 → 仍要运行
3. 安装向导可改安装目录；卸载**不会**删除 `%APPDATA%\wiki-rag-desktop\data` 里的知识库数据

### 已知限制

- 只支持 NSIS 安装版；`--pack` 的免安装目录不会自动更新
- 1.0.0 版本没有更新器，需手动安装一次
- 安装包未签名，自动更新只校验 `latest.yml` 的 sha512 完整性，不校验发布者身份
- 深色是「换一套颜色取值」实现的：界面自身的元素都会变，但文档里的图片、PDF 预览等
  外部内容保持原色，不做反色处理
- 用量页的费用是「按单价估算」，未收录单价的模型会单列「未计价」，不混进总额
```

---

## 2. 发布步骤（**合并落地、`v1.0.22` tag 已在远端之后**再执行）

> 本版**不预先创建 tag**：tag 由平台在 PR 合并后打在 `master` 的合并提交上。
> `electron-builder --publish always` 在 release 不存在时会**自己建 release 并连带打 tag**，
> 因此这里不用它，改用 `gh release create --verify-tag`——tag 不存在时它直接失败，不会误建。

### 2.1 前提

1. PR 已合并到 `master`，远端已存在 `v1.0.22` tag，且 tag 指向合并提交：

   ```powershell
   git ls-remote --tags origin | Select-String 'v1\.0\.22'
   gh api repos/guanmuya/wiki-rag-knowledge/git/ref/tags/v1.0.22
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
   > `docs/evidence/release-v1.0.22-build.txt` §5 或现场重算的值为准，并逐项核对 `latest.yml`。

### 2.2 创建 Release 并上传三个资产

**Release 正文只取 §1 的 markdown 代码块，不要传整份文件**：本文件 §2 的上传命令与 §3 的
内部说明不适合对外（前两版发布实测即按此口径，发布前后逐字比对过正文与 §1 相同）。
先把 §1 代码块另存为无 BOM 的 UTF-8 临时文件，再把它交给 `--notes-file`：

```powershell
# 在仓库根目录；提取 §1 的 ```markdown 代码块正文（不含围栏），写成无 BOM 的 UTF-8
$doc = Get-Content -Raw -Encoding utf8 docs/RELEASE_NOTES_v1.0.22.md
$m = [regex]::Match($doc, '(?s)## 1\. Release 正文草稿.*?```markdown\r?\n(.*?)\r?\n```')
if (-not $m.Success) { throw '未找到 §1 的 markdown 代码块' }
[System.IO.File]::WriteAllText((Join-Path (Get-Location) '.tmp-release-body.md'),
  $m.Groups[1].Value, (New-Object System.Text.UTF8Encoding($false)))

# --verify-tag 保证 tag 已存在才发布，不会顺手建 tag
gh release create v1.0.22 --verify-tag `
  --title "v1.0.22" `
  --notes-file .tmp-release-body.md `
  frontend/release/updates/latest.yml `
  frontend/release/updates/wiki-rag-knowledge-1.0.22-x64-setup.exe `
  frontend/release/updates/wiki-rag-knowledge-1.0.22-x64-setup.exe.blockmap
```

等价写法（Release 已存在、只补传资产）：

```powershell
gh release upload v1.0.22 --clobber `
  frontend/release/updates/latest.yml `
  frontend/release/updates/wiki-rag-knowledge-1.0.22-x64-setup.exe `
  frontend/release/updates/wiki-rag-knowledge-1.0.22-x64-setup.exe.blockmap
```

### 2.3 发布后必做的三项校验

1. **三个资产都在**：

   ```powershell
   gh release view v1.0.22 --json 'tagName,isDraft,assets' | ConvertFrom-Json |
     ForEach-Object { $_.tagName; $_.isDraft; $_.assets | ForEach-Object { "$($_.name) $($_.size)" } }
   ```

   > PowerShell 5.1 下别把 `--json a,b,c` 与带引号的 `--jq` 一起用：字段列表会被当成数组拆开，
   > jq 表达式里的逗号也会被剥引号后拆参数。要么像上面那样用单引号包字段名再 `ConvertFrom-Json`，
   > 要么在 `pwsh`（PowerShell 7）里跑。

2. **匿名可下载**（`latest.yml`、安装包、`.blockmap` 三个都应是 `200`；仓库是公开的，不需要令牌）：

   ```powershell
   $base = 'https://github.com/guanmuya/wiki-rag-knowledge/releases/download/v1.0.22'
   foreach ($f in @('latest.yml','wiki-rag-knowledge-1.0.22-x64-setup.exe','wiki-rag-knowledge-1.0.22-x64-setup.exe.blockmap')) {
     $code = curl.exe -sIL -o NUL -w '%{http_code}' "$base/$f"
     "$f -> $code"
   }
   ```

   > 注意：首次上传后 GitHub 的 `release-assets.githubusercontent.com` 有 1–2 分钟 CDN 延迟，
   > 刚上传就 `404` 属正常，等一会儿再验（见 `docs/UPDATE.md` §2.1 的实测记录）。

3. **`latest.yml` 与资产对得上**：`latest.yml` 里的 `version` 必须是 `1.0.22`，
   `files[].url` / `path` 必须与上传的安装包**文件名完全一致**，`sha512` 与本地构建出的
   安装包一致（本轮构建的值见 `docs/evidence/release-v1.0.22-build.txt` §5；
   若发布前重建过安装包，以发布那次现场重算的值为准）：

   ```powershell
   curl.exe -sL https://github.com/guanmuya/wiki-rag-knowledge/releases/download/v1.0.22/latest.yml
   ```

### 2.4 客户端侧确认（可选，但测过才算闭环）

用已装 v1.0.21 的机器打开「关于 → 软件更新 → 检查更新」，应检出 1.0.22 并下载；
下载完成状态应变成「待安装」，主进程日志无 `update:error`。
判据与 v1.0.2 那次实测相同（`docs/UPDATE.md` §2.1）。

---

## 3. 本轮（构建轮）没做的事

- **没有打 tag**：`v1.0.22` 由平台在合并落地后创建，构建轮不建；构建轮建会指到非合并提交。
- **没有上传 Release**：tag 还不存在时先建 Release 会把 tag 指到错误提交；
  上传动作按 §2 在 tag 出现后执行。
- **没有代码签名**：见 `docs/UPDATE.md` §6.2，属外部凭据，需用户单独授权。
- **业务代码零改动**：本版只 bump 版本号三处、补交付说明与构建记录、构建安装包；
  `backend/app/**` 只有 `main.py` 的 `VERSION` 一行，`frontend/src/**`、`frontend/electron/**`
  一行未动。本版唯一的用户可感知改动（深色外观与「跟随系统」、复选框/下拉框修正、
  用量页多余说明删除）此前已随 #77 / #78 合入 `master`，本发版任务不碰。
- **修正了 `CHANGELOG.md` 的一处归档位置**：`[1.0.21]` 段里误放的一节「深色外观与「跟随系统」」
  是在 `v1.0.21` tag（`d039f35`）之后才合入的（`903899e`），已整块移进 `[1.0.22]`。
