// preload：只暴露必要的只读信息，不把 Node 能力交给页面。
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('wrkDesktop', {
  isDesktop: true,
  getInfo: () => ipcRenderer.invoke('wrk:info'),
  getLogs: () => ipcRenderer.invoke('wrk:logs'),
  /** 启动失败的结构化说明：{ id, title, action, detail }，无失败时为 null */
  getFailure: () => ipcRenderer.invoke('wrk:failure'),
  /** kind: 'data' | 'logs' | 'install' */
  openPath: (kind) => ipcRenderer.invoke('wrk:openPath', kind),
  restartBackend: () => ipcRenderer.invoke('wrk:restartBackend'),
})
