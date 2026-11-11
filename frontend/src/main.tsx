import React from 'react'
import ReactDOM from 'react-dom/client'
import { HashRouter } from 'react-router-dom'
import App from './App'
import { AuthProvider, ToastProvider } from './auth'
import { initApiBase } from './api'
import ErrorBoundary from './components/ErrorBoundary'
import './styles.css'

/**
 * 桌面应用必须用 HashRouter：
 * Electron 用 file:// 加载 dist/index.html，BrowserRouter 的 history 路由
 * 在 file:// 下无法回退，刷新或深链会直接 404。
 * 浏览器开发模式也用同一个 Router，保证两种模式行为一致，避免"开发能跑打包不能跑"。
 */
const Router = HashRouter

async function bootstrap(): Promise<void> {
  // 桌面模式：先拿到 sidecar 的真实端口，否则首个请求会打到空地址。
  // initApiBase 内部已兜住异常，这里再包一层是防它未来改动后把整棵树立马打挂。
  try {
    await initApiBase()
  } catch {
    /* 拿不到端口就让首个请求自己报「后端不可用」，BackendGate 会给出可读兜底页 */
  }

  ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
    <React.StrictMode>
      {/* 最外层兜底：页面级异常由 Layout 里的 ErrorBoundary 接住，
          这里接的是门（BackendGate）/ Provider 等更外层组件抛错的情况，
          目的是任何情况下都不出现白屏。 */}
      <ErrorBoundary title="应用没有正常启动" fullscreen>
        <Router>
          <ToastProvider>
            <AuthProvider>
              <App />
            </AuthProvider>
          </ToastProvider>
        </Router>
      </ErrorBoundary>
    </React.StrictMode>,
  )
}

void bootstrap()
