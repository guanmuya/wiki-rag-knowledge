import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useEffect, useState, type ReactElement } from 'react'
import { useAuth } from '../auth'
import { isDesktop, setupApi } from '../api'
import OfflineBanner from './OfflineBanner'

interface NavItem {
  to: string
  label: string
  icon: NavIconName
  adminOnly?: boolean
  match?: (path: string) => boolean
}

type NavIconName = 'book' | 'chat' | 'search' | 'clock' | 'users' | 'settings' | 'info'

/**
 * 导航图标用内联 SVG，而不是 ▤ ✦ ⌕ 这类字符。
 * 字符图标的字重/基线随字体变化，七行排在一起粗细不一，是最显「廉价」的细节之一。
 */
const NAV_ICONS: Record<NavIconName, ReactElement> = {
  book: (
    <path d="M3 4.5A1.5 1.5 0 0 1 4.5 3H8a2 2 0 0 1 2 2v11a1.6 1.6 0 0 0-1.6-1.6H4.5A1.5 1.5 0 0 1 3 13V4.5Zm10 0A1.5 1.5 0 0 1 14.5 3H18a1.5 1.5 0 0 1 1.5 1.5V13A1.5 1.5 0 0 1 18 14.5h-3.4A1.6 1.6 0 0 0 13 16V5Z" />
  ),
  chat: (
    <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v7a2.5 2.5 0 0 1-2.5 2.5H9l-4.2 3.2A.7.7 0 0 1 3.7 17V5.5Z" />
  ),
  search: (
    <>
      <circle cx="9.5" cy="9.5" r="5.5" />
      <path d="m13.8 13.8 4.2 4.2" />
    </>
  ),
  clock: (
    <>
      <circle cx="10" cy="10" r="7" />
      <path d="M10 6.5V10l2.4 1.6" />
    </>
  ),
  users: (
    <>
      <circle cx="8" cy="7.5" r="2.8" />
      <path d="M3.5 16.5c.6-2.6 2.4-4 4.5-4s3.9 1.4 4.5 4" />
      <path d="M13.4 5.2a2.6 2.6 0 0 1 0 4.9M14.4 12.9c1.4.4 2.4 1.6 2.8 3.6" />
    </>
  ),
  settings: (
    <>
      <circle cx="10" cy="10" r="2.6" />
      <path d="M10 3.2v1.6M10 15.2v1.6M3.2 10h1.6M15.2 10h1.6M5.2 5.2l1.1 1.1M13.7 13.7l1.1 1.1M14.8 5.2l-1.1 1.1M6.3 13.7l-1.1 1.1" />
    </>
  ),
  info: (
    <>
      <circle cx="10" cy="10" r="7" />
      <path d="M10 9v4.2M10 6.6v.2" />
    </>
  ),
}

function NavIcon({ name }: { name: NavIconName }) {
  return (
    <svg
      viewBox="0 0 20 20"
      width="16"
      height="16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {NAV_ICONS[name]}
    </svg>
  )
}

/**
 * 导航按「工作台 / 管理 / 系统」分组。
 * 平铺七个入口时，管理员项和日常项混在一起，用户找不到重点；
 * 分组后日常高频入口在上，管理项在下，符合使用频率。
 */
const NAV_GROUPS: Array<{ label: string; items: NavItem[] }> = [
  {
    label: '工作台',
    items: [
      { to: '/kbs', label: '知识库', icon: 'book', match: (p) => p.startsWith('/kbs') },
      { to: '/chat', label: '智能问答', icon: 'chat', match: (p) => p.startsWith('/chat') },
      { to: '/search', label: '检索调试', icon: 'search', match: (p) => p.startsWith('/search') },
      { to: '/tasks', label: '任务中心', icon: 'clock', match: (p) => p.startsWith('/tasks') },
    ],
  },
  {
    label: '管理',
    items: [
      { to: '/admin/users', label: '用户管理', icon: 'users', adminOnly: true, match: (p) => p.startsWith('/admin/users') },
      { to: '/admin/models', label: '模型设置', icon: 'settings', adminOnly: true, match: (p) => p.startsWith('/admin/models') },
    ],
  },
  {
    label: '系统',
    items: [{ to: '/about', label: '关于', icon: 'info', match: (p) => p.startsWith('/about') }],
  },
]

export default function Layout() {
  const { user, isAdmin, logout } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [mobileOpen, setMobileOpen] = useState(false)
  const [appInfo, setAppInfo] = useState<{ version: string; dataDir: string } | null>(null)
  // 企业名在首次初始化时写入 app_settings，作为侧边栏标题展示；
  // 没设置就退回产品名，不让标题空着。
  const [orgName, setOrgName] = useState('')

  useEffect(() => {
    setMobileOpen(false)
  }, [location.pathname])

  useEffect(() => {
    if (!isDesktop()) return
    void window.wrkDesktop?.getInfo().then((info) => {
      setAppInfo({ version: info.version, dataDir: info.dataDir })
    })
  }, [])

  useEffect(() => {
    let alive = true
    void setupApi
      .getOrg()
      .then((r) => {
        if (alive) setOrgName((r.org_name || '').trim())
      })
      .catch(() => {
        /* 读不到企业名不影响使用，保持产品名兜底 */
      })
    return () => {
      alive = false
    }
  }, [])

  const groups = NAV_GROUPS.map((g) => ({
    label: g.label,
    items: g.items.filter((it) => !it.adminOnly || isAdmin),
  })).filter((g) => g.items.length > 0)
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
            <strong>{orgName || '企业知识库'}</strong>
            <span>文档整理 · 智能问答</span>
          </div>
        </div>

        <nav className="nav">
          {groups.map((group) => (
            <div className="nav-group" key={group.label}>
              <div className="nav-group-label">{group.label}</div>
              {group.items.map((it) => (
                <NavLink
                  key={it.to}
                  to={it.to}
                  className={({ isActive }) =>
                    `nav-item ${isActive || it.match?.(location.pathname) ? 'active' : ''}`
                  }
                >
                  <span className="nav-icon"><NavIcon name={it.icon} /></span>
                  <span>{it.label}</span>
                </NavLink>
              ))}
            </div>
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
          <div className="breadcrumb">
            <span>{orgName || '企业知识库'}</span>
            <span className="sep">/</span>
            <strong>{currentTitle(location.pathname)}</strong>
          </div>
          <div className="topbar-right">
            <span className="topbar-user">{user?.display_name || user?.username}</span>
            <span className="badge badge-muted">{isAdmin ? '管理员' : '员工'}</span>
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
