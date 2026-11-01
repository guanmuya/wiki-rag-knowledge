<#
.SYNOPSIS
  验证「关闭应用后没有残留 wrk-backend.exe」——sidecar 孤儿进程的回归检查。

.DESCRIPTION
  背景（桌面版真实故障）：Electron 的 stopBackend() 只杀掉了 PyInstaller 的
  bootloader，真正跑 FastAPI + worker 的 python 进程活了下来。孤儿进程继续抢任务、
  长期占用 SQLite 写锁，主程序这边所有写请求就卡在 database is locked 上
  —— 用户看到的是「embedding 解析文档时整个软件卡住」。

  两种模式：

  1) 默认：只做静态检查，不启动任何程序
     - 系统里现在有没有 wrk-backend.exe 残留（历史孤儿）；
     - frontend/electron/main.cjs 是否还留着 `backend.killed` 这种
       「kill() 之后立刻为 true → SIGKILL 兜底永不执行」的旧判断；
     - 是否用 taskkill /T /F 结束进程树、启动前是否 killStaleBackends()；
     - backend/app/services/procguard.py（数据目录单实例锁 + 父进程守护）是否在位。

  2) -StartApp：实机验证（需要已安装的桌面版，或 -Exe 指定免安装目录里的 exe）
     拉起应用 → 等 sidecar 就绪 → **只强杀 Electron 主进程**
     （模拟用户从任务管理器结束进程，这正是留孤儿的场景）→
     断言 $TimeoutSec 秒内 wrk-backend.exe 全部消失。
     跑完自动清理自己拉起的进程，不留垃圾。

.PARAMETER StartApp
  真正拉起应用做一次「强杀 Electron → 后端必须自毁」的验证。不加就是静态检查。

.PARAMETER Exe
  应用可执行文件路径。缺省按安装版常见位置自动找：
  %LOCALAPPDATA%\Programs\wiki-rag-desktop\企业知识库.exe、Program Files 下、
  以及仓库里的 frontend\release\win-unpacked\企业知识库.exe。

.PARAMETER DataDir
  应用数据目录（只用来打印/核对，不会被写）。默认 %APPDATA%\wiki-rag-desktop\data。

.PARAMETER TimeoutSec
  强杀 Electron 之后，等后端消失的秒数。默认 3（验收标准就是 3 秒内）。

.PARAMETER BootWaitSec
  -StartApp 时等后端就绪的最长秒数。默认 90（首次启动要初始化数据库）。

.PARAMETER KeepRunning
  验证结束后不清理自己拉起的进程（排查现场时用）。默认清理。

.EXAMPLE
  pwsh -File scripts/verify/no-orphan.ps1
  只做静态检查：打印当前残留 + 代码里几个关键点是否还在。

.EXAMPLE
  pwsh -File scripts/verify/no-orphan.ps1 -StartApp
  拉起安装版 → 强杀 Electron → 断言 3s 内没有 wrk-backend.exe。

.EXAMPLE
  pwsh -File scripts/verify/no-orphan.ps1 -StartApp -Exe ".\frontend\release\win-unpacked\企业知识库.exe" -TimeoutSec 5
  用免安装目录里的产物验证，容忍 5 秒。

.NOTES
  用 PowerShell 7（pwsh）运行；Windows PowerShell 5.1 需要文件带 UTF-8 BOM。
  脚本不会修改应用数据目录，也不会安装/卸载任何东西。
#>
[CmdletBinding()]
param(
    [switch]$StartApp,
    [string]$Exe,
    [string]$DataDir = (Join-Path $env:APPDATA 'wiki-rag-desktop\data'),
    [int]$TimeoutSec = 3,
    [int]$BootWaitSec = 90,
    [switch]$KeepRunning
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$BackendName = 'wrk-backend.exe'

$script:Results = New-Object System.Collections.Generic.List[object]

function Add-Result {
    param(
        [ValidateSet('PASS', 'WARN', 'FAIL', 'INFO')][string]$Status,
        [string]$Name,
        [string]$Detail = ''
    )
    $script:Results.Add([pscustomobject]@{ Status = $Status; Name = $Name; Detail = $Detail })
    $color = switch ($Status) {
        'PASS' { 'Green' }
        'WARN' { 'Yellow' }
        'FAIL' { 'Red' }
        default { 'Gray' }
    }
    Write-Host ("  [{0,-4}] {1}" -f $Status, $Name) -ForegroundColor $color
    if ($Detail) { Write-Host ("         {0}" -f $Detail) -ForegroundColor DarkGray }
}

function Get-BackendProcesses {
    <# 取 wrk-backend.exe 进程（带父 pid 和命令行，便于判断是不是孤儿）。 #>
    try {
        $rows = Get-CimInstance Win32_Process -Filter "Name='$BackendName'" -ErrorAction Stop
    }
    catch {
        # CIM 不可用时退化成 Get-Process（信息少一点，但不至于让检查直接失败）
        return @(Get-Process -Name 'wrk-backend' -ErrorAction SilentlyContinue |
            ForEach-Object {
                [pscustomobject]@{
                    ProcessId = $_.Id; ParentProcessId = 0
                    CommandLine = ''; ExecutablePath = $_.Path
                }
            })
    }
    return @($rows)
}

function Test-SourceHygiene {
    <# 静态检查代码里的几个关键点：这些是「留孤儿」和「15s 卡死」的直接成因。 #>
    Write-Host "`n== 代码检查 ==" -ForegroundColor Cyan

    $mainCjs = Join-Path $RepoRoot 'frontend\electron\main.cjs'
    if (-not (Test-Path $mainCjs)) {
        Add-Result FAIL 'electron/main.cjs 存在' "找不到 $mainCjs"
    }
    else {
        $code = Get-Content -Raw -Path $mainCjs
        # 去掉注释行再匹配：注释里很可能正是在解释「为什么不再用 backend.killed」，
        # 直接全文匹配会把正确的代码误判成没改。
        $codeOnly = (($code -split "`r?`n") |
            Where-Object { $_.Trim() -notmatch '^(//|/\*|\*)' }) -join "`n"
        Add-Result PASS 'electron/main.cjs 存在' $mainCjs

        # 1) 必须按进程树结束（taskkill /T /F 或 POSIX 进程组），否则 bootloader 死了、
        #    真正的 python 子进程还在
        if ($codeOnly -match 'taskkill' -and $codeOnly -match '/T') {
            Add-Result PASS '按进程树结束 sidecar' 'main.cjs 里有 taskkill /T（连子进程一起杀）'
        }
        elseif ($codeOnly -match 'process\.kill\(-|pkill') {
            Add-Result WARN '按进程树结束 sidecar' '只看到 POSIX 进程组结束，Windows 分支要确认有 taskkill /T'
        }
        else {
            Add-Result FAIL '按进程树结束 sidecar' '没找到 taskkill /T：只杀 bootloader 会留孤儿 python 进程'
        }

        # 2) 旧的 `backend.killed` 判断：kill() 之后立刻为 true，SIGKILL 兜底永远不执行
        if ($codeOnly -match 'backend\.killed') {
            Add-Result FAIL '没有 backend.killed 旧判断' 'kill() 后 killed 立刻为 true，兜底强杀永不执行（这是孤儿的主因）'
        }
        else {
            Add-Result PASS '没有 backend.killed 旧判断' '改用 exitCode / signalCode 判断存活'
        }

        # 3) 启动前清理历史残留
        if ($codeOnly -match 'killStaleBackends') {
            Add-Result PASS '启动前清理残留后端' '有 killStaleBackends()：上一次的孤儿会被收掉'
        }
        else {
            Add-Result WARN '启动前清理残留后端' '没找到 killStaleBackends()：上次崩溃留下的后端会继续抢任务'
        }
    }

    $procguard = Join-Path $RepoRoot 'backend\app\services\procguard.py'
    if (Test-Path $procguard) {
        $pg = Get-Content -Raw -Path $procguard
        Add-Result PASS 'procguard.py 存在' $procguard
        if ($pg -match 'def acquire_data_lock' -and $pg -match 'def watch_parent') {
            Add-Result PASS 'procguard 提供锁 + 父进程守护' 'acquire_data_lock / watch_parent 都在'
        }
        else {
            Add-Result FAIL 'procguard 提供锁 + 父进程守护' '缺少 acquire_data_lock 或 watch_parent'
        }
    }
    else {
        Add-Result FAIL 'procguard.py 存在' "找不到 $procguard（单实例锁与父进程守护都没有）"
    }

    $entry = Join-Path $RepoRoot 'backend\app\desktop_entry.py'
    if (Test-Path $entry) {
        $de = Get-Content -Raw -Path $entry
        if ($de -match 'procguard') {
            Add-Result PASS 'desktop_entry 接入了 procguard' '启动时抢数据目录锁 + 盯父进程'
        }
        else {
            Add-Result FAIL 'desktop_entry 接入了 procguard' 'desktop_entry.py 没有引用 procguard'
        }
    }
    else {
        Add-Result WARN 'desktop_entry.py 存在' "找不到 $entry"
    }
}

function Resolve-AppExe {
    param([string]$Given)
    if ($Given) {
        if (-not (Test-Path $Given)) { throw "指定的 exe 不存在：$Given" }
        return (Resolve-Path $Given).Path
    }
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\wiki-rag-desktop\企业知识库.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\企业知识库\企业知识库.exe'),
        (Join-Path $env:ProgramFiles '企业知识库\企业知识库.exe'),
        (Join-Path $RepoRoot 'frontend\release\win-unpacked\企业知识库.exe')
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { return (Resolve-Path $candidate).Path }
    }
    throw ("没找到应用可执行文件。请用 -Exe 指定，例如：`n" +
        "  pwsh -File scripts/verify/no-orphan.ps1 -StartApp -Exe `".\frontend\release\win-unpacked\企业知识库.exe`"")
}

function Wait-BackendGone {
    <# 轮询直到给定的 pid 集合全部消失，返回是否成功。 #>
    param([int[]]$Pids, [int]$Seconds)
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        $alive = @($Pids | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
        if ($alive.Count -eq 0) { return $true }
        Start-Sleep -Milliseconds 150
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Invoke-OrphanCheck {
    param([string]$AppExe)

    Write-Host "`n== 实机验证：强杀 Electron 后后端必须自毁 ==" -ForegroundColor Cyan
    Write-Host ("  应用：{0}" -f $AppExe) -ForegroundColor DarkGray
    Write-Host ("  数据目录：{0}" -f $DataDir) -ForegroundColor DarkGray

    $baseline = @(Get-BackendProcesses)
    if ($baseline.Count -gt 0) {
        $ids = ($baseline | ForEach-Object { $_.ProcessId }) -join ', '
        Add-Result FAIL '启动前没有残留后端' "先清干净再测：taskkill /F /IM $BackendName（当前 pid：$ids）"
        return
    }
    Add-Result PASS '启动前没有残留后端' "0 个 $BackendName"

    $app = $null
    $newBackendPids = @()
    # 开发机常把 ELECTRON_RUN_AS_NODE=1 设成全局变量，Electron 会退化成纯 Node：
    # 进程一起来就以 exit=0 立刻退出、连 main.log 都不写，看起来像「应用启动失败」。
    # 这里是 GUI 启动，必须先摘掉它；测完在 finally 里还原，不改用户环境。
    $savedElectronNode = $env:ELECTRON_RUN_AS_NODE
    Remove-Item Env:\ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
    try {
        $app = Start-Process -FilePath $AppExe -PassThru
        Write-Host ("  已启动应用 pid={0}，等后端就绪（最多 {1}s）…" -f $app.Id, $BootWaitSec) -ForegroundColor DarkGray

        # 「wrk-backend.exe 出现」不等于「后端就绪」：PyInstaller 单文件启动时
        # bootloader 先出现，真正的 python 进程要解包 5-10s 才把父进程守护装上。
        # 这期间强杀 Electron，测到的是「守护还没装」，不是缺陷——会假红。
        # 所以必须等本次启动的 WRK_READY（Electron 把 sidecar 的 stderr 写进 main.log）。
        $logPath = Join-Path (Split-Path -Parent $DataDir) 'logs\main.log'
        $logStart = 0
        if (Test-Path $logPath) { $logStart = @(Get-Content $logPath -Encoding UTF8 -ErrorAction SilentlyContinue).Count }

        $deadline = (Get-Date).AddSeconds($BootWaitSec)
        $ready = $false
        while ((Get-Date) -lt $deadline) {
            $newBackendPids = @(Get-BackendProcesses | ForEach-Object { $_.ProcessId })
            if ($newBackendPids.Count -gt 0) {
                if (Test-Path $logPath) {
                    $fresh = @(Get-Content $logPath -Encoding UTF8 -ErrorAction SilentlyContinue | Select-Object -Skip $logStart)
                    if (@($fresh | Select-String 'WRK_READY').Count -gt 0) { $ready = $true; break }
                }
                else {
                    # 自定义 userData 时可能没有 main.log：退化成「进程出现后再等 3s」
                    if (-not $script:_fellBackReadyAt) { $script:_fellBackReadyAt = Get-Date }
                    if (((Get-Date) - $script:_fellBackReadyAt).TotalSeconds -ge 3) { $ready = $true; break }
                }
            }
            if ($app.HasExited) { throw "应用启动后立刻退出了（exit=$($app.ExitCode)）。常见原因：① 机器上已有同款实例在跑（单实例锁生效，先关掉它，安装版和免安装版共用同一个 userData）；② 环境变量 ELECTRON_RUN_AS_NODE=1（本脚本已自动摘掉）。日志：$DataDir\..\logs\main.log" }
            Start-Sleep -Milliseconds 400
        }
        if ($newBackendPids.Count -eq 0) {
            Add-Result FAIL '后端已拉起' "$BootWaitSec s 内没看到 $BackendName（应用是否真的启动成功？）"
            return
        }
        if (-not $ready) {
            Add-Result FAIL '后端已就绪' ("{0}s 内没在 {1} 里等到 WRK_READY；后端可能还在解包，此时强杀 Electron 会假红" -f $BootWaitSec, $logPath)
            return
        }
        Add-Result PASS '后端已拉起' ("pid：{0}（已就绪）" -f ($newBackendPids -join ', '))

        # 关键：**只杀 Electron 主进程**，不带 /T。
        # 带 /T 会把子进程一起杀掉，这个测试就永远绿，测不出「留孤儿」。
        # 有些安装形态是「启动器 exe 再拉起真正的应用」，所以拿不到原进程时按名字找。
        $appName = [System.IO.Path]::GetFileNameWithoutExtension($AppExe)
        $victim = $app
        if (-not $victim -or $victim.HasExited) {
            $victim = Get-Process -Name $appName -ErrorAction SilentlyContinue | Select-Object -First 1
        }
        if (-not $victim) {
            Add-Result FAIL '强杀 Electron 主进程' "找不到运行中的应用进程（$appName），无法验证"
            return
        }
        Write-Host ("  强杀 Electron 主进程 pid={0}（不杀进程树，模拟任务管理器结束进程）…" -f $victim.Id) -ForegroundColor DarkGray
        & taskkill /PID $victim.Id /F 2>&1 | Out-Null

        $started = Get-Date
        $gone = Wait-BackendGone -Pids $newBackendPids -Seconds $TimeoutSec
        $elapsed = ((Get-Date) - $started).TotalSeconds

        if ($gone) {
            Add-Result PASS ("{0}s 内后端自毁" -f $TimeoutSec) ("实测 {0:N2}s，无孤儿" -f $elapsed)
        }
        else {
            # 超时后不要立刻判死：再等一会儿，把「慢了几秒」和「永远不退出」分开。
            # 两种情况都是 FAIL，但下一步动作完全不同（前者查守护线程/解包窗口，后者查进程树）。
            $late = Wait-BackendGone -Pids $newBackendPids -Seconds ($TimeoutSec * 3)
            $elapsedLate = ((Get-Date) - $started).TotalSeconds
            if ($late) {
                Add-Result FAIL ("{0}s 内后端自毁" -f $TimeoutSec) ("实际 {0:N2}s 才退出（慢于验收标准，但不是孤儿）—— 查父进程守护是否在「后端就绪」前就已装上" -f $elapsedLate)
            }
            else {
                $left = @(Get-CimInstance Win32_Process -Filter "Name='$BackendName'" -ErrorAction SilentlyContinue)
                $detail = ($left | ForEach-Object { "pid=$($_.ProcessId) ppid=$($_.ParentProcessId)" }) -join '; '
                Add-Result FAIL ("{0}s 内后端自毁" -f $TimeoutSec) ("{0:N1}s 后仍在运行：{1} —— 父进程守护没生效" -f $elapsedLate, $detail)
            }
        }
    }
    finally {
        if ($null -ne $savedElectronNode -and $savedElectronNode -ne '') {
            $env:ELECTRON_RUN_AS_NODE = $savedElectronNode
        }
        if (-not $KeepRunning) {
            # 清理：不留现场垃圾（否则下一次检查会先看到残留）
            foreach ($procId in @($newBackendPids)) {
                if (Get-Process -Id $procId -ErrorAction SilentlyContinue) {
                    & taskkill /PID $procId /T /F 2>&1 | Out-Null
                }
            }
            if ($app -and -not $app.HasExited) {
                & taskkill /PID $app.Id /F 2>&1 | Out-Null
            }
            $leftover = @(Get-BackendProcesses)
            if ($leftover.Count -gt 0) {
                Write-Host ("  清理后仍有 {0} 个 {1}：{2}" -f $leftover.Count, $BackendName,
                    (($leftover | ForEach-Object { $_.ProcessId }) -join ', ')) -ForegroundColor Yellow
            }
        }
    }
}

# ------------------------------------------------------------------ 主流程
Write-Host "== 孤儿 sidecar 检查 ==" -ForegroundColor Cyan
Write-Host ("  仓库：{0}" -f $RepoRoot) -ForegroundColor DarkGray

Write-Host "`n== 当前进程 ==" -ForegroundColor Cyan
$current = @(Get-BackendProcesses)
if ($current.Count -eq 0) {
    Add-Result PASS "现在没有残留 $BackendName" '系统干净'
}
else {
    $lines = $current | ForEach-Object {
        $parentAlive = if ($_.ParentProcessId) { [bool](Get-Process -Id $_.ParentProcessId -ErrorAction SilentlyContinue) } else { $true }
        "pid=$($_.ProcessId) ppid=$($_.ParentProcessId) 父进程存活=$parentAlive"
    }
    Add-Result FAIL "现在有残留 $BackendName" (($lines -join '; ') + "  → 先 taskkill /F /IM $BackendName")
}

Test-SourceHygiene

if ($StartApp) {
    try {
        $appExe = Resolve-AppExe -Given $Exe
        Invoke-OrphanCheck -AppExe $appExe
    }
    catch {
        Add-Result FAIL '实机验证' $_.Exception.Message
    }
}
else {
    Write-Host "`n== 说明 ==" -ForegroundColor Cyan
    Write-Host '  这次只做了静态检查（没有启动应用）。' -ForegroundColor DarkGray
    Write-Host '  要验证「关闭应用后后端真的自毁」，加 -StartApp：' -ForegroundColor DarkGray
    Write-Host '    pwsh -File scripts/verify/no-orphan.ps1 -StartApp' -ForegroundColor DarkGray
    Write-Host ("  它会拉起安装版 → 只强杀 Electron 主进程 → 断言 {0}s 内 {1} 消失。" -f $TimeoutSec, $BackendName) -ForegroundColor DarkGray
    Write-Host '  注意：会真的启动/结束一次应用；请先关掉正在用的应用实例。' -ForegroundColor DarkGray
}

Write-Host "`n== 结论 ==" -ForegroundColor Cyan
$fail = @($script:Results | Where-Object { $_.Status -eq 'FAIL' })
$warn = @($script:Results | Where-Object { $_.Status -eq 'WARN' })
$pass = @($script:Results | Where-Object { $_.Status -eq 'PASS' })
Write-Host ("  PASS {0} / WARN {1} / FAIL {2}" -f $pass.Count, $warn.Count, $fail.Count)
if ($fail.Count -gt 0) {
    Write-Host '  结论：不通过 —— 见上面 FAIL 项（每项都写了下一步该做什么）。' -ForegroundColor Red
    exit 1
}
if ($warn.Count -gt 0) {
    Write-Host '  结论：通过（有 WARN 项，建议确认）。' -ForegroundColor Yellow
    exit 0
}
Write-Host '  结论：通过 —— 关闭应用后不应残留 wrk-backend.exe。' -ForegroundColor Green
exit 0
