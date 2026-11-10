// preload：只暴露必要的只读信息，不把 Node 能力交给页面。
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('wrkDesktop', {
  isDesktop: true,
  getInfo: () => ipcRenderer.invoke('wrk:info'),
  getLogs: () => ipcRenderer.invoke('wrk:logs'),
  /** 启动失败的结构化说明：{ id, title, action, detail }，无失败时为 null */
  getFailure: () => ipcRenderer.invoke('wrk:failure'),
  /** P1-9：首次改密成功后清除主进程内存里的初始凭据（幂等）。 */
  clearInitialCredential: () => ipcRenderer.invoke('wrk:clear-initial-credential'),
  /** kind: 'data' | 'logs' | 'install' */
  openPath: (kind) => ipcRenderer.invoke('wrk:openPath', kind),
  restartBackend: () => ipcRenderer.invoke('wrk:restartBackend'),
  // ---- 自动更新 ----
  updateState: () => ipcRenderer.invoke('wrk:update-state'),
  checkUpdate: () => ipcRenderer.invoke('wrk:update-check'),
  installUpdate: () => ipcRenderer.invoke('wrk:update-install'),
  /** 订阅更新状态推送，返回取消订阅函数 */
  onUpdate: (cb) => {
    const handler = (_e, state) => cb(state)
    ipcRenderer.on('wrk:update', handler)
    return () => ipcRenderer.removeListener('wrk:update', handler)
  },
})
