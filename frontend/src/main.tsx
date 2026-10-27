import React from 'react'
import ReactDOM from 'react-dom/client'
import { HashRouter } from 'react-router-dom'
import App from './App'
import { AuthProvider, ToastProvider } from './auth'
import { initApiBase } from './api'
import './styles.css'

/**
 * 桌面应用必须用 HashRouter：
 * Electron 用 file:// 加载 dist/index.html，BrowserRouter 的 history 路由
 * 在 file:// 下无法回退，刷新或深链会直接 404。
 * 浏览器开发模式也用同一个 Router，保证两种模式行为一致，避免"开发能跑打包不能跑"。
 */
const Router = HashRouter

async function bootstrap(): Promise<void> {
  // 桌面模式：先拿到 sidecar 的真实端口，否则首个请求会打到空地址
  await initApiBase()

  ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
    <React.StrictMode>
      <Router>
        <ToastProvider>
          <AuthProvider>
            <App />
          </AuthProvider>
        </ToastProvider>
      </Router>
    </React.StrictMode>,
  )
}

void bootstrap()
