import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Field, TextInput } from '../components/ui'
import { errorText } from '../utils'
import { notifySetupCompleted, setupApi } from '../api'
import type { SetupStatus } from '../types'

/**
 * 首次初始化引导。
 *
 * 为什么需要这个页面：桌面应用没有运维人员。早期版本在后端随机生成管理员密码
 * 并打印到日志，用户关掉弹窗就永久登录不了。现在改成让用户自己设，
 * 后端只在「无任何管理员」时接受这个请求。
 */
export default function SetupPage() {
  const navigate = useNavigate()
  const [status, setStatus] = useState<SetupStatus | null>(null)
  const [orgName, setOrgName] = useState('')
  const [username, setUsername] = useState('admin')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [checking, setChecking] = useState(true)

  useEffect(() => {
    let alive = true
    setupApi
      .status()
      .then((s) => {
        if (!alive) return
        setStatus(s)
        if (s.org_name) setOrgName(s.org_name)
        // 已初始化就不要再停在这个页面
        if (s.initialized) navigate('/login', { replace: true })
      })
      .catch((err) => {
        if (alive) setError(errorText(err))
      })
      .finally(() => {
        if (alive) setChecking(false)
      })
    return () => {
      alive = false
    }
  }, [navigate])

  const strength = passwordStrength(password)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    if (username.trim().length < 3) {
      setError('用户名至少 3 个字符')
      return
    }
    if (password.length < 10) {
      setError('密码至少 10 位')
      return
    }
    if (strength.kinds < 3) {
      setError('密码需包含大写、小写、数字、符号中的至少三类')
      return
    }
    if (password !== confirm) {
      setError('两次输入的密码不一致')
      return
    }
    setBusy(true)
    try {
      await setupApi.createAdmin({
        username: username.trim(),
        password,
        display_name: displayName.trim(),
        org_name: orgName.trim(),
      })
      // 先让启动门刷新 needs_setup，再跳转；顺序反了会被门弹回本页形成死循环
      notifySetupCompleted()
      navigate('/login', { replace: true })
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  if (checking) {
    return (
      <div className="login-page">
        <section className="login-panel">
          <div className="sub">正在检查系统状态…</div>
        </section>
      </div>
    )
  }

  return (
    <div className="login-page">
      <aside className="login-aside">
        <div>
          <div className="brand" style={{ padding: 0, border: 'none' }}>
            <div className="brand-mark">知</div>
            <div className="brand-text">
              <strong style={{ fontSize: 15 }}>{status?.app_name || '企业知识库'}</strong>
              <span>首次初始化</span>
            </div>
          </div>
        </div>
        <h1>先创建一个管理员账号</h1>
        <p>
          这是本机第一次启动。系统不会预置固定密码，管理员由你自己创建，
          创建后即可登录并配置模型、建立知识库、分配员工账号。
        </p>
        <div className="login-features">
          <div className="login-feature">
            <span className="mark">1</span>
            <span>管理员密码只保存在本机数据库，以加盐哈希存储</span>
          </div>
          <div className="login-feature">
            <span className="mark">2</span>
            <span>后续员工账号由管理员创建，系统不开放自助注册</span>
          </div>
          <div className="login-feature">
            <span className="mark">3</span>
            <span>数据目录可随时从侧边栏打开，卸载重装不会丢失</span>
          </div>
        </div>
      </aside>

      <section className="login-panel">
        <div className="login-card">
        <h2>初始化系统</h2>
        <div className="sub">创建第一个管理员账号</div>
        {error ? <div className="login-error">{error}</div> : null}

        <form onSubmit={submit}>
          <Field label="企业名称" hint="显示在侧边栏标题，可留空">
            <TextInput
              value={orgName}
              onChange={(e) => setOrgName(e.target.value)}
              placeholder="例如：某某科技有限公司"
              maxLength={64}
              autoFocus
            />
          </Field>
          <Field label="管理员用户名" required>
            <TextInput
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="至少 3 个字符"
              autoComplete="username"
            />
          </Field>
          <Field label="显示名称" hint="可留空，默认「系统管理员」">
            <TextInput
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="例如：张管理"
            />
          </Field>
          <Field label="密码" required hint="至少 10 位，包含大小写、数字、符号中的三类">
            <TextInput
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="请设置密码"
              autoComplete="new-password"
            />
          </Field>
          {password ? (
            <div className="pwd-meter" aria-live="polite">
              <div className="pwd-meter-bar">
                <span
                  className={`pwd-meter-fill level-${strength.level}`}
                  style={{ width: `${strength.percent}%` }}
                />
              </div>
              <span className="pwd-meter-text">{strength.text}</span>
            </div>
          ) : null}
          <Field label="确认密码" required>
            <TextInput
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              placeholder="再次输入密码"
              autoComplete="new-password"
            />
          </Field>

          <button type="submit" className="btn btn-primary full" disabled={busy}>
            {busy ? <span className="spinner" style={{ width: 14, height: 14 }} /> : null}
            {busy ? '创建中…' : '创建管理员并进入系统'}
          </button>
        </form>
        </div>

        <div className="login-foot">
          初始化完成后此页面将不可再用，如需新增管理员请登录后在「用户管理」中操作
        </div>
      </section>
    </div>
  )
}

function passwordStrength(pwd: string): { level: number; percent: number; text: string; kinds: number } {
  const kinds =
    (pwd.length >= 10 ? 1 : 0) +
    (/[a-z]/.test(pwd) ? 1 : 0) +
    (/[A-Z]/.test(pwd) ? 1 : 0) +
    (/[0-9]/.test(pwd) ? 1 : 0) +
    (/[^a-zA-Z0-9]/.test(pwd) ? 1 : 0)
  const percent = Math.min(100, Math.round((kinds / 5) * 100))
  if (!pwd) return { level: 0, percent: 0, text: '', kinds: 0 }
  if (kinds <= 2) return { level: 1, percent, text: '强度偏弱', kinds }
  if (kinds === 3) return { level: 2, percent, text: '强度一般', kinds }
  if (kinds === 4) return { level: 3, percent, text: '强度良好', kinds }
  return { level: 4, percent, text: '强度很好', kinds }
}
