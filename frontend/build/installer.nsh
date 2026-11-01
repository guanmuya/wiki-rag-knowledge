; 安装包自定义 NSIS 片段（electron-builder.yml 的 nsis.include 引入）。
;
; 为什么需要：默认的 CHECK_APP_RUNNING 只会 `taskkill`（不带 /F）请求应用退出，
; 然后弹「无法关闭，请手动关闭后重试」。如果应用正好停在一个原生模态框上
; （例如旧版本的启动失败对话框），WM_CLOSE 会被吞掉，安装就卡在那一步。
;
; 这里改成：先礼貌请它退出 → 等 2 秒 → 还在就强杀（连同 Python sidecar）。
; 强杀是安全的：数据都在 SQLite 里，未完成的任务在下次启动时由
; worker.recover_interrupted() 重新排队（1.0.5 起）。
!macro customCheckAppRunning
  DetailPrint "正在关闭运行中的 ${PRODUCT_NAME}…"
  ; 1) 先请求正常退出（Electron 收到 WM_CLOSE 会走 before-quit 停 sidecar）
  nsExec::Exec `"$SYSDIR\cmd.exe" /c taskkill /im "${APP_EXECUTABLE_FILENAME}" /fi "USERNAME eq %USERNAME%"`
  Sleep 2000
  ; 2) 仍在运行就强杀，避免卡在「无法关闭」对话框
  nsExec::Exec `"$SYSDIR\cmd.exe" /c taskkill /F /IM "${APP_EXECUTABLE_FILENAME}" /fi "USERNAME eq %USERNAME%"`
  ; 3) sidecar 是独立进程，必须一起结束，否则 resources\backend\wrk-backend.exe 会被占用
  nsExec::Exec `"$SYSDIR\cmd.exe" /c taskkill /F /IM "wrk-backend.exe" /fi "USERNAME eq %USERNAME%"`
  Sleep 800
!macroend
