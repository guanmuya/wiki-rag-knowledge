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

发布：

```bash
cd frontend
GH_TOKEN=$(gh auth token) node scripts/build.mjs --publish
```

`--publish` 会把安装包、`latest.yml`、`.blockmap` 传到当前版本对应的 GitHub Release。

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

## 3. 发版流程

1. 改 `frontend/package.json` 的 `version`（例如 `1.0.1`），**必须比线上大**。
2. 构建：
   ```bash
   cd frontend
   node scripts/build.mjs            # 安装包 + release/updates/
   # 或 GitHub：GH_TOKEN=$(gh auth token) node scripts/build.mjs --publish
   ```
3. 上传 `release/updates/` 到更新源（GitHub 方式已自动完成）。
4. 客户端行为：启动 8 秒后检查一次，之后每 4 小时一次；也可以在「关于 → 软件更新 → 检查更新」手动触发。
5. 下载完成后弹窗「立即重启安装 / 稍后」。选稍后，退出应用时也会自动安装（`autoInstallOnAppQuit`）。

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
| `WRK_UPDATE_URL` | 强制使用 generic 源（覆盖配置文件），本地联调用 |
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
4. 用**已打包**的 1.0.1 程序指向本地源启动：
   ```bash
   cd release/win-unpacked
   WRK_UPDATE_URL=http://127.0.0.1:8899 ./企业知识库.exe
   ```
   日志（`%APPDATA%\wiki-rag-desktop\logs\main.log`）应出现「发现新版本 1.0.2」和「下载完成」。

## 6. 签名与安全

- 未做代码签名时安装包能更新，但 SmartScreen 会提示；正式交付建议买代码签名证书，并在 `electron-builder.yml` 配 `win.certificateFile` / `certificatePassword`。
- electron-updater 会校验 `latest.yml` 里的 sha512，下载被篡改会直接失败；但它不校验发布者签名，所以更新源本身必须是可信 HTTPS。
- 不要把 GitHub PAT 打进客户端：私有仓库 + token 的方案等于把 token 发给所有用户。

## 7. 回滚

electron-updater 默认 `allowDowngrade = false`，不会自动降级。要回滚：

1. 把旧版本安装包重新上传，并把 `latest.yml` 的 `version` 改成**比当前更高**的版本号（例如当前 1.0.3，回滚包标成 1.0.4）；
2. 或让用户手动下载旧安装包覆盖安装（数据目录不受影响）。

## 8. 已知限制

- 1.0.0 版本没有更新器，用户必须先手动装一次 1.0.1，之后才能自动更新。
- 只支持 NSIS 安装版；`--pack` 的免安装目录不会自动更新。
- 更新包下载到 `%LOCALAPPDATA%\<app>-updater\pending`，失败可手动清掉重试。
- 私有 GitHub 仓库无法作为更新源（见 §2A）。
