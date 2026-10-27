import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useEffect, useState } from 'react'
import { useAuth } from '../auth'
import { isDesktop } from '../api'
import OfflineBanner from './OfflineBanner'

interface NavItem {
  to: string
  label: string
  icon: string
  adminOnly?: boolean
  match?: (path: string) => boolean
}

const NAV_ITEMS: NavItem[] = [
  { to: '/kbs', label: '知识库', icon: '▤', match: (p) => p.startsWith('/kbs') },
  { to: '/chat', label: '智能问答', icon: '✦', match: (p) => p.startsWith('/chat') },
  { to: '/search', label: '检索调试', icon: '⌕', match: (p) => p.startsWith('/search') },
  { to: '/tasks', label: '任务中心', icon: '◷', match: (p) => p.startsWith('/tasks') },
  { to: '/admin/users', label: '用户管理', icon: '☗', adminOnly: true, match: (p) => p.startsWith('/admin/users') },
  { to: '/admin/models', label: '模型设置', icon: '⚙', adminOnly: true, match: (p) => p.startsWith('/admin/models') },
  { to: '/about', label: '关于', icon: 'ⓘ', match: (p) => p.startsWith('/about') },
]

export default function Layout() {
  const { user, isAdmin, logout } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [mobileOpen, setMobileOpen] = useState(false)
  const [appInfo, setAppInfo] = useState<{ version: string; dataDir: string } | null>(null)

  useEffect(() => {
    setMobileOpen(false)
  }, [location.pathname])

  useEffect(() => {
    if (!isDesktop()) return
    void window.wrkDesktop?.getInfo().then((info) => {
      setAppInfo({ version: info.version, dataDir: info.dataDir })
    })
  }, [])

  const items = NAV_ITEMS.filter((it) => !it.adminOnly || isAdmin)
  const isFlush = location.pathname.startsWith('/chat')

  const handleLogout = async () => {
    await logout()
    navigate('/login', { replace: true })
  }

  return (
    <div className="app-shell">
      <aside className={`sidebar ${mobileOpen ? 'open' : ''}`}>
        <div className="brand">
          <div className="brand-mark">知</div>
          <div className="brand-text">
            <strong>企业知识库</strong>
            <span>文档整理 · 智能问答</span>
          </div>
        </div>

        <nav className="nav">
          {items.map((it) => (
            <NavLink
              key={it.to}
              to={it.to}
              className={({ isActive }) =>
                `nav-item ${isActive || it.match?.(location.pathname) ? 'active' : ''}`
              }
            >
              <span className="nav-icon">{it.icon}</span>
              <span>{it.label}</span>
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-foot">
          <div className="user-chip">
            <div className="avatar">{initials(user?.display_name || user?.username || '?')}</div>
            <div className="user-meta">
              <strong>{user?.display_name || user?.username}</strong>
              <span>{isAdmin ? '管理员' : '普通员工'}</span>
            </div>
          </div>
          {appInfo && (
            <button
              type="button"
              className="desktop-data-link"
              title={appInfo.dataDir}
              onClick={() => void window.wrkDesktop?.openPath('data')}
            >
              数据目录 · v{appInfo.version}
            </button>
          )}
          <button type="button" className="btn btn-ghost btn-sm full" onClick={handleLogout}>
            退出登录
          </button>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <button
            type="button"
            className="icon-btn mobile-only"
            onClick={() => setMobileOpen((v) => !v)}
            aria-label="切换菜单"
          >
            ☰
          </button>
          <div className="topbar-title">{currentTitle(location.pathname)}</div>
          <div className="topbar-right">
            <span className="topbar-user">{user?.display_name || user?.username}</span>
          </div>
        </header>
        <main className={isFlush ? 'content content-flush' : 'content'}>
          {location.pathname.startsWith('/kbs') && !location.pathname.startsWith('/kbs/') ? (
            <OfflineBanner />
          ) : null}
          <Outlet />
        </main>      </div>
    </div>
  )
}

function currentTitle(path: string): string {
  if (path.startsWith('/admin/users')) return '用户管理'
  if (path.startsWith('/admin/models')) return '模型设置'
  if (path.startsWith('/kbs/')) return '知识库详情'
  if (path.startsWith('/kbs')) return '知识库'
  if (path.startsWith('/chat')) return '智能问答'
  if (path.startsWith('/search')) return '检索调试'
  if (path.startsWith('/tasks')) return '任务中心'
  if (path.startsWith('/about')) return '关于'
  return '企业知识库'
}

function initials(name: string): string {
  const trimmed = name.trim()
  if (!trimmed) return '?'
  return trimmed.slice(0, 1).toUpperCase()
}
