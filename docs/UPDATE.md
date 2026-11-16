# 自动更新（Windows / NSIS）

桌面版用 [electron-updater](https://www.electron.build/auto-update) 做自动更新：主进程检查 `latest.yml` → 下载新安装包（有 `.blockmap` 时走差量）→ 校验 sha512 → 提示重启安装。更新不会动 `%APPDATA%\wiki-rag-desktop\data` 里的知识库数据。

## 1. 组成

| 文件 | 作用 |
|---|---|
| `frontend/electron/updater.cjs` | 主进程更新器：定时检查、下载进度、错误翻译、IPC |
| `frontend/electron/update-config.json` | 更新源配置（打包进 `resources/update-config.json`） |
| `frontend/electron-builder.yml` 的 `publish` | 决定 electron-builder 生成 `app-update.yml` 与 `latest.yml` |
| `frontend/src/components/UpdateCard.tsx` | 关于页「软件更新」卡片：状态、进度、检查/安装按钮 |
| `frontend/scripts/build.mjs` | 构建后把 `latest.yml` + 安装包 + `.blockmap` 收集到 `release/updates/` |

## 2. 更新源两种

### A. GitHub Releases（默认配置）

```json
{
  "provider": "github",
  "owner": "guanmuya",
  "repo": "wiki-rag-knowledge"
}
```

**仓库必须公开**。私有仓库的 Release 资产需要鉴权，终端用户拿不到，更新检查会返回 404。

正式版本使用下方 [发版流程](#3-发版流程)：推送版本标签后，GitHub Actions 自动测试、
打包、验证并发布三个资产。旧的 `node scripts/build.mjs --publish` 是直接发布入口，
不会执行完整的自动发布门禁，不作为日常发版方式。

### B. 自建静态源（generic）

任意能公开读的 HTTPS 目录（对象存储、Nginx、CDN 都行），把 `release/updates/` 整个传上去：

```json
{
  "provider": "generic",
  "url": "https://your-cdn.example.com/wiki-rag/updates"
}
```

目录里至少要有：

```
latest.yml
wiki-rag-knowledge-1.0.1-x64-setup.exe
wiki-rag-knowledge-1.0.1-x64-setup.exe.blockmap   # 可选，用于差量下载
```

> 私有对象存储需要签名 URL 时，把 `url` 指向一个会 302 到签名地址的接口即可（electron-updater 跟随重定向）。

## 2.1 实测记录（2026-10-31，v1.0.2）

- 仓库 `guanmuya/wiki-rag-knowledge` 之前是 **private**，`releases/download/...` 匿名访问返回 404；改成 public 后匿名 200（切换后 CDN 有 1–2 分钟延迟）。
- 已发布 **v1.0.2**（安装包 / `.blockmap` / `latest.yml` 三个资产）。
- 用 **1.0.1 的打包产物**驱动 Playwright 验证：关于页 →「检查更新」→ 检出 `v1.0.2` → 下载 133 MB → 状态变「待安装」（`badge=待安装`），主进程日志无 `update:error`。**6/6 通过**。
- 结论：从 1.0.1 起的自动更新链路可用；1.0.0 仍需手动装一次。

## 2.2 更新速度：全量 vs 差分（实测）

electron-updater 的 NSIS 更新会优先走**差分下载**：读新包的 `.blockmap`，和
`%LOCALAPPDATA%\wiki-rag-desktop-updater\` 里缓存的旧安装包比对，只下变化的块。

| 场景 | 实测 |
|---|---|
| 1.0.1 → 1.0.2（旧安装包没缓存，手动装的） | **全量 135.8 MB** |
| 1.0.2 → 1.0.3（缓存了 1.0.2 安装包） | 1473 个变化块，**只下 33.9 MB（25%）** |

- 缓存目录：`%LOCALAPPDATA%\wiki-rag-desktop-updater\`（`installer.exe` + `current.blockmap`）。
  **删掉它下次就退回全量下载**，别当垃圾清。
- 首次更新（或跨大版本）必然是全量：用户手动装的旧包不在缓存里，没有比对基线。

### 包体积构成（决定全量下载要多久）

| 部分 | 解包体积 |
|---|---|
| Electron 运行时 + locales | 约 220 MB（其中 locales 41 MB / 55 个语言包） |
| Python 后端 sidecar（PyInstaller onefile） | 56.7 MB |
| 前端 asar | 8.9 MB |
| 合计 `win-unpacked` | 331 MB → NSIS 压缩后 133 MB |

已做的体积优化（下一次发版生效）：

- `electronLanguages: [zh-CN, en-US]`：locales 从 41 MB 降到 1 MB（只影响解包体积和启动，不影响界面语言）；
- `compression: maximum`：实测 **133 MB → 125.6 MB**（-5.5%，构建多花约 1 分钟）。

### 想更快：换更新源

GitHub Release 资产走 `release-assets.githubusercontent.com`，国内速度不稳定。
把 `release/updates/`（`latest.yml` + 安装包 + `.blockmap`）传到国内对象存储
（阿里云 OSS / 腾讯云 COS / Cloudflare R2），`update-config.json` 改成：

```json
{ "provider": "generic", "url": "https://your-bucket.oss-cn-hangzhou.aliyuncs.com/wrk-updates/" }
```

一般能到 5–20 MB/s，133 MB 的全量包十几秒下完。注意 `url` 必须能匿名读，且目录里
同时有 `latest.yml`、安装包、`.blockmap` 三个文件。

## 2.3 国内加速：三种可落地的方案

GitHub Release 资产走 `release-assets.githubusercontent.com`，国内速度从几十 KB/s 到几 MB/s 波动很大。要稳定快，只有把**同样三个文件**（`latest.yml` + 安装包 + `.blockmap`）放到国内能高速访问的地方。

### 方案 A：国内对象存储（最稳，推荐）

阿里云 OSS / 腾讯云 COS / 七牛都行。用**默认域名**即可（`https://<bucket>.oss-cn-hangzhou.aliyuncs.com/`），不需要备案；绑自定义域名且服务大陆用户才需要 ICP。

```bash
# 目录结构保持扁平：latest.yml、安装包、.blockmap 三个文件放同一层
# 用 rclone（S3 兼容）或 ossutil 上传，ACL 必须是「公共读」
ossutil cp -r frontend/release/updates/ oss://wrk-updates/ --acl public-read
# 或
rclone copy frontend/release/updates/ oss:wrk-updates/ --progress
```

成本量级：125 MB 存储 + 流量约 0.5 元/GB，100 次全量更新 ≈ 6 元。

### 方案 B：Cloudflare R2（有免费额度，无需备案）

10 GB 存储、出网不收流量费，自定义域名走 Cloudflare CDN（国内一般 1–5 MB/s，比 GitHub 稳）。rclone 配 S3 兼容 endpoint 后同样是 `rclone copy` 三个文件，桶设公共读或挂自定义域名。

### 方案 C：Cloudflare Worker 反代 GitHub（零存储）

写一个 Worker 把 `releases/download/...` 代理出来并缓存，得到一个稳定域名。注意大文件（125 MB）可能受 Worker 响应限制，**优先用 R2**，Worker 只适合应急。

### 配好之后：多源自动回落

`frontend/electron/update-config.json` 支持 `sources` 数组，按顺序尝试，前一个 404/超时就自动换下一个（下载只走命中的那个源）：

```json
{
  "sources": [
    { "provider": "generic", "url": "https://wrk-updates.oss-cn-hangzhou.aliyuncs.com/", "label": "阿里云 OSS" },
    { "provider": "github", "owner": "guanmuya", "repo": "wiki-rag-knowledge", "label": "GitHub" }
  ],
  "checkOnStart": true,
  "checkIntervalHours": 4
}
```

- 两个源都要放**同一版本**的三个文件，否则会出现「国内源说 1.0.5、GitHub 说 1.0.4」的错乱。
- `sources` 与旧的 `provider/owner/repo` 二选一，不要同时写。
- 配置随安装包发布，所以**装了这个版本的用户下一次更新才会用上新源**；已装旧版的用户仍走旧配置。
- 版本号必须递增，`latest.yml` 里的 `version` 与安装包文件名要一致。

## 3. 发版流程

1. 在 PR 中将 `frontend/package.json` 的 `version` 和 `backend/app/main.py` 的 `VERSION`
   同步改为比所有已发布正式版本都大的三段版本号，例如 `1.0.23`。更新 `CHANGELOG.md`，
   并添加 `docs/RELEASE_NOTES_v1.0.23.md`：第 1 节标题使用
   `## 1. Release 正文草稿`，该节内放一个非空且正确闭合的 `markdown` 围栏代码块作为公开正文；
   随后保留 `## 2. 发布步骤` 等第 2 节标题。公开正文中请勿使用 `## 2. ...` 这类带数字的二级标题，
   以便校验器识别章节边界。工作流只提取该代码块，不会把内部发布步骤作为正文。
2. 等 PR 的 CI 通过并合并到 `master`。仓库管理员应在 GitHub Settings → Branches 或
   Rulesets 设置必需状态检查；这项保护是 GitHub 仓库设置，并非 YAML 自动生效。
3. 在与远端 `master` 同步的本地仓库中，为准备发布的提交创建并推送一个标签：

   ```bash
   git switch master
   git pull --ff-only origin master
   git tag -a v1.0.23 -m "v1.0.23"
   git push origin v1.0.23
   ```

   标签只是例子，请换成第 1 步准备好的新版本。推送标签会触发自动正式发布；不要为了
   演练而推送正式标签。由其他 workflow 使用默认 `GITHUB_TOKEN` 推送的标签通常不会触发
   新的 push workflow。
4. 在 GitHub 的 **Actions → CI** 打开该标签对应的运行。CI、版本校验、打包、实际安装
   冒烟测试和资产复核全部通过后，工作流会自动发布正式 Release；不需要再手动点击发布。
   已安装用户可能随后检查并下载更新。
5. 演练：在 **Actions → CI → Run workflow** 选择要验证的分支并手动运行。手动运行会
   执行 CI 和安装包检查，生成临时 artifact，但不会发布正式版本。首次部署新 workflow 前，
   可对其分支执行此演练。
6. 失败时，打开红色步骤查看日志。若仅发布任务失败，优先选择 **Re-run failed jobs**，
   以复用本次已检查的安装包；重新运行全部任务可能重建出不同的安装包，若存在旧草稿，
   工作流会停止以避免混用。已有正式 Release 或不属于本次运行的草稿不会被自动覆盖。
   排查后再决定如何处理草稿；不要覆盖已发布版本。
7. 客户端启动后及每 4 小时检查更新，也可以在「关于 → 软件更新 → 检查更新」手动触发。
   下载后选「稍后」时，退出应用也可能自动安装。当前更新器不允许降级：已发布版本出现
   问题时应修复并发布更高版本，不能依赖将旧版本重新设为 Latest 来回退已升级客户端。

> 打包依赖维护：`frontend/patches/app-builder-lib@25.1.8.patch` 将
> [electron-builder 上游修复 #9769](https://github.com/electron-userland/electron-builder/pull/9769)
> 回移到当前锁定的安装器模板，避免首次安装时读取路径越界。升级 `electron-builder` 时应确认
> 新版本已包含修复，再移除该补丁并重新验证首次安装、已有安装目录与自动更新。

## 4. 配置项

`frontend/electron/update-config.json`：

| 字段 | 默认 | 说明 |
|---|---|---|
| `provider` | `github` | `github` 或 `generic` |
| `owner` / `repo` | `guanmuya` / `wiki-rag-knowledge` | GitHub 源必填 |
| `url` | — | generic 源必填，指向放 `latest.yml` 的目录 |
| `checkOnStart` | `true` | 启动 8 秒后自动检查 |
| `checkIntervalHours` | `4` | 自动检查间隔 |
| `allowPrerelease` | `false` | 是否接受预发布版本 |

环境变量（联调/运维用）：

| 变量 | 作用 |
|---|---|
| `WRK_UPDATE_URL` | 强制使用 generic 源（覆盖配置文件），本地联调用；打包版必须 HTTPS，开发版仅放行本机回环明文源（见 §6.3） |
| `WRK_UPDATE_DEV=1` | 开发模式下也启用更新检查 |
| `WRK_UPDATE_DISABLE=1` | 完全禁用更新检查 |

## 5. 本地联调（不装两遍）

1. 构建出安装包与 `latest.yml`：
   ```bash
   cd frontend && node scripts/build.mjs --skip-backend
   ```
2. 造一个「更高版本」的 `latest.yml` 指向现有安装包（sha512 用 base64）：
   ```bash
   python - <<'PY'
   import base64, hashlib, pathlib
   exe = pathlib.Path('release/wiki-rag-knowledge-1.0.1-x64-setup.exe')
   digest = base64.b64encode(hashlib.sha512(exe.read_bytes()).digest()).decode()
   pathlib.Path('release/updates/latest.yml').write_text(f"""version: 1.0.2
   files:
     - url: {exe.name}
       sha512: {digest}
       size: {exe.stat().st_size}
   path: {exe.name}
   sha512: {digest}
   releaseDate: '2026-10-28T00:00:00.000Z'
   """, encoding='utf-8')
   print(digest)
   PY
   ```
3. 起静态服务：
   ```bash
   cd release/updates && python -m http.server 8899
   ```
4. 用**未打包的开发模式**指向本地源启动（打包版会拒绝明文 HTTP 源，含回环地址，见 §6.3）：
   ```bash
   cd frontend
   WRK_UPDATE_URL=http://127.0.0.1:8899 pnpm start
   # 打包版会拒绝明文 HTTP 源：改用自签 HTTPS 静态服务，并把 URL 填 https://127.0.0.1:8899（见 §6.3）
   ```
   日志（`%APPDATA%\wiki-rag-desktop\logs\main.log`）应出现「发现新版本 1.0.2」和「下载完成」。

## 6. 签名与安全（P1-9 风险书面记录）

> 本节是 `docs/HEALTH_CHECK.md` §4.2 P1-9 要求的风险留档：**本项目当前没有代码签名证书，且更新链路允许「开发模式下的本机明文回环源」**。签名校验依赖证书，属于外部凭据，需用户单独授权后才能引入；在此之前按下面口径交付。

### 6.1 当前防线（已落地）

- 所有 generic 更新源（`update-config.json` / `sources` / `WRK_UPDATE_URL` / `app-update.yml` 兜底）在 `frontend/electron/update-url.cjs` 统一校验：**打包版必须 HTTPS**，明文源被拒绝后直接禁用更新器，不会触发自动下载安装。
- `frontend/dev-app-update.yml` 不再预置明文 HTTP 地址，交付配置里没有可直连的明文更新源。
- electron-updater 仍校验 `latest.yml` 里的 sha512，下载内容被改动会直接失败。
- 渲染进程开启 `sandbox: true` 与 CSP；初始管理员口令不再落 `main.log`（见 §6.4）。

### 6.2 风险一：无发布者签名

`frontend/electron-builder.yml` 的 `forceCodeSigning: false`、`win.signAndEditExecutable: true` 表示安装包没有 Authenticode 签名。electron-updater 只校验 sha512（完整性），**不校验发布者身份**：只要有人能替换更新源上的 `latest.yml` 与其指向的安装包，就能把任意程序推给客户端；而 `autoDownload=true` + `autoInstallOnAppQuit=true` 会让这个过程自动完成（下载完弹一次「立即重启安装 / 稍后」，选稍后也会在退出时安装）。此外未签名安装包会触发 Windows SmartScreen 提示。

**残余风险等级**：中/高。缓解依赖「更新源是可信 HTTPS 且源服务器不被攻破」这一前提，不能替代签名。

**后续步骤（需用户点头，涉及新增外部凭据）**：

1. 申请代码签名证书（OV/EV）或使用组织的签名服务；
2. 在 `electron-builder.yml` 配 `win.certificateFile` / `certificatePassword`（或 `CSC_LINK` / `CSC_KEY_PASSWORD` 环境变量），把 `forceCodeSigning` 改为 `true`；
3. 在 `updater.cjs` 打开 electron-updater 的签名校验（`verifyUpdateCodeSignature`），并用一次真实发版验证「篡改安装包 → 更新被拒绝」；
4. 复测 §3 发版流程与 §2.1 的更新链路。

在此之前**不要**把 `autoDownload` 关掉：关掉后 `update-downloaded` 永不触发，关于页「安装」按钮（要求 `status==='ready'`）会彻底失效，反而破坏更新链路。

### 6.3 风险二：开发模式允许明文 HTTP 回环源

开发（未打包）模式下，`WRK_UPDATE_URL=http://127.0.0.1:8899` 这类**本机回环**明文源仍被放行，用于 `python -m http.server` 本地联调（见 §5）。回环流量不经过外部网络，中间人风险局限于开发机本身；**打包版一律拒绝任何明文源，包括回环地址**。

**残余风险**：若开发机上已有恶意进程，可劫持回环端口喂给开发版客户端一个伪造的 `latest.yml`；仅影响开发机，不影响交付给用户的数据。

**后续步骤（无需凭据）**：

1. 本地联调改用自签 HTTPS 静态服务，`WRK_UPDATE_URL` 填 `https://127.0.0.1:8899`；
2. 或保持回环明文，但开发机上不要同时运行不可信进程；
3. 若将来确实需要在交付环境放行非 HTTPS 源，须先在此处补一条书面风险说明并经用户确认，不要直接改 `update-url.cjs` 的校验规则。

### 6.4 其它

- 不要把 GitHub PAT 打进客户端：私有仓库 + token 的方案等于把 token 发给所有用户。
- 初始管理员明文口令已不再写 `main.log`：`main.cjs` 的 `log()` 统一脱敏，明文只在首次部署弹窗出现一次，弹窗关闭或首次改密成功后清除内存中的凭据；兜底页 `getLogs` 因此也读不到口令。

## 7. 回滚

electron-updater 默认 `allowDowngrade = false`，不会自动降级。要回滚：

1. 把旧版本安装包重新上传，并把 `latest.yml` 的 `version` 改成**比当前更高**的版本号（例如当前 1.0.3，回滚包标成 1.0.4）；
2. 或让用户手动下载旧安装包覆盖安装（数据目录不受影响）。

## 8. 已知限制

- 1.0.0 版本没有更新器，用户必须先手动装一次 1.0.1，之后才能自动更新（1.0.1 → 1.0.2 已实测通过）。
- 只支持 NSIS 安装版；`--pack` 的免安装目录不会自动更新。
- 更新包下载到 `%LOCALAPPDATA%\<app>-updater\pending`，失败可手动清掉重试。
- 私有 GitHub 仓库无法作为更新源（见 §2A）。
