import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { ApiError, authApi, configureApiHandlers, getToken, setToken } from './api'
import type { GrantLevel, MeResponse, Permissions, User } from './types'

export interface ToastItem {
  id: number
  kind: 'info' | 'success' | 'error'
  text: string
}

interface AuthState {
  user: User | null
  permissions: Permissions | null
  loading: boolean
  isAdmin: boolean
  levelOf: (kbId: number) => GrantLevel | null
  canManage: (kbId: number) => boolean
  login: (username: string, password: string) => Promise<User>
  logout: () => Promise<void>
  refreshMe: () => Promise<MeResponse | null>
  mustChangePassword: boolean
  clearMustChange: () => void
}

const AuthContext = createContext<AuthState | null>(null)

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth 必须在 AuthProvider 内使用')
  return ctx
}

interface ToastState {
  toasts: ToastItem[]
  push: (text: string, kind?: ToastItem['kind']) => void
  dismiss: (id: number) => void
}

const ToastContext = createContext<ToastState | null>(null)

export function useToast(): ToastState {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast 必须在 ToastProvider 内使用')
  return ctx
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([])
  const seq = useRef(0)

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  const push = useCallback(
    (text: string, kind: ToastItem['kind'] = 'info') => {
      seq.current += 1
      const id = seq.current
      setToasts((prev) => {
        // 同一文案在短时间内重复（例如 StrictMode 双请求）只保留一条
        if (prev.some((t) => t.text === text)) return prev
        return [...prev, { id, kind, text }]
      })
      window.setTimeout(() => dismiss(id), kind === 'error' ? 6000 : 3200)
    },
    [dismiss],
  )

  const value = useMemo<ToastState>(() => ({ toasts, push, dismiss }), [toasts, push, dismiss])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toast-stack" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast-${t.kind}`} onClick={() => dismiss(t.id)}>
            <span className="toast-dot" />
            <span className="toast-text">{t.text}</span>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

/** 读取 401 后要跳转的登录地址，交给 AuthProvider 处理。 */
const UNAUTHORIZED_EVENT = 'wrk:unauthorized'

export function emitUnauthorized(): void {
  window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [permissions, setPermissions] = useState<Permissions | null>(null)
  const [loading, setLoading] = useState(true)
  const { push } = useToast()

  const refreshMe = useCallback(async (): Promise<MeResponse | null> => {
    if (!getToken()) {
      setUser(null)
      setPermissions(null)
      return null
    }
    try {
      const data = await authApi.me()
      setUser(data.user)
      setPermissions(data.permissions)
      return data
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setToken(null)
      }
      setUser(null)
      setPermissions(null)
      return null
    }
  }, [])

  useEffect(() => {
    configureApiHandlers({
      onUnauthorized: () => {
        setToken(null)
        setUser(null)
        setPermissions(null)
        window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
      },
      onForbidden: (detail: string) => {
        push(detail || '没有操作权限', 'error')
      },
    })
  }, [push])

  useEffect(() => {
    let alive = true
    ;(async () => {
      await refreshMe()
      if (alive) setLoading(false)
    })()
    return () => {
      alive = false
    }
  }, [refreshMe])

  const login = useCallback(async (username: string, password: string): Promise<User> => {
    const res = await authApi.login(username, password)
    setToken(res.token)
    setUser(res.user)
    try {
      const me = await authApi.me()
      setUser(me.user)
      setPermissions(me.permissions)
    } catch {
      setPermissions({ is_admin: res.user.role === 'admin', kb_grants: [] })
    }
    return res.user
  }, [])

  const logout = useCallback(async () => {
    try {
      await authApi.logout()
    } catch {
      /* 无状态令牌，失败也继续本地登出 */
    }
    setToken(null)
    setUser(null)
    setPermissions(null)
  }, [])

  const levelOf = useCallback(
    (kbId: number): GrantLevel | null => {
      if (permissions?.is_admin) return 'manage'
      const g = permissions?.kb_grants.find((x) => x.kb_id === kbId)
      return g ? g.level : null
    },
    [permissions],
  )

  const canManage = useCallback((kbId: number) => levelOf(kbId) === 'manage', [levelOf])

  const clearMustChange = useCallback(() => {
    setUser((prev) => (prev ? { ...prev, must_change_password: false } : prev))
  }, [])

  const value = useMemo<AuthState>(
    () => ({
      user,
      permissions,
      loading,
      isAdmin: Boolean(permissions?.is_admin),
      levelOf,
      canManage,
      login,
      logout,
      refreshMe,
      mustChangePassword: Boolean(user?.must_change_password),
      clearMustChange,
    }),
    [user, permissions, loading, levelOf, canManage, login, logout, refreshMe, clearMustChange],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export { UNAUTHORIZED_EVENT }
