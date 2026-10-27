import { useEffect } from 'react'
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import Layout from './components/Layout'
import BackendGate from './components/BackendGate'
import { UNAUTHORIZED_EVENT, useAuth } from './auth'
import { LoadingBlock } from './components/ui'
import LoginPage from './pages/LoginPage'
import SetupPage from './pages/SetupPage'
import KbListPage from './pages/KbListPage'
import KbDetailPage from './pages/KbDetailPage'
import ChatPage from './pages/ChatPage'
import UsersPage from './pages/UsersPage'
import ModelsPage from './pages/ModelsPage'
import TasksPage from './pages/TasksPage'
import SearchPage from './pages/SearchPage'
import AboutPage from './pages/AboutPage'

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth()
  const location = useLocation()
  if (loading) {
    return (
      <div className="boot-screen">
        <LoadingBlock text="正在恢复登录状态…" />
      </div>
    )
  }
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname }} />
  // 强制改密：未改密前不允许进入任何业务页面
  if (user.must_change_password) {
    return <Navigate to="/login" replace state={{ from: location.pathname, forcePassword: true }} />
  }
  return <>{children}</>
}

function RequireAdmin({ children }: { children: React.ReactNode }) {
  const { isAdmin, loading } = useAuth()
  if (loading) return <LoadingBlock text="正在校验权限…" />
  if (!isAdmin) return <Navigate to="/kbs" replace />
  return <>{children}</>
}

export default function App() {
  const navigate = useNavigate()

  useEffect(() => {
    const handler = () => navigate('/login', { replace: true })
    window.addEventListener(UNAUTHORIZED_EVENT, handler)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, handler)
  }, [navigate])

  return (
    <BackendGate>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/setup" element={<SetupPage />} />
        <Route
          element={
            <RequireAuth>
              <Layout />
            </RequireAuth>
          }
        >
          <Route path="/" element={<Navigate to="/kbs" replace />} />
          <Route path="/kbs" element={<KbListPage />} />
          <Route path="/kbs/:id" element={<KbDetailPage />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/chat/:conversationId" element={<ChatPage />} />
          <Route path="/search" element={<SearchPage />} />
          <Route path="/tasks" element={<TasksPage />} />
          <Route path="/about" element={<AboutPage />} />
          <Route
            path="/admin/users"
            element={
              <RequireAdmin>
                <UsersPage />
              </RequireAdmin>
            }
          />
          <Route
            path="/admin/models"
            element={
              <RequireAdmin>
                <ModelsPage />
              </RequireAdmin>
            }
          />
        </Route>
        <Route path="*" element={<Navigate to="/kbs" replace />} />
      </Routes>
    </BackendGate>
  )
}
