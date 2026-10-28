import { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth'
import { errorText } from '../utils'
import { Field, LoadingBlock, Modal, TextInput } from '../components/ui'
import { authApi, metaApi } from '../api'
import type { MetaInfo } from '../types'

export default function LoginPage() {
  const { user, loading, login, mustChangePassword, clearMustChange, refreshMe } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [meta, setMeta] = useState<MetaInfo | null>(null)

  const [pwdOpen, setPwdOpen] = useState(false)
  const [oldPwd, setOldPwd] = useState('')
  const [newPwd, setNewPwd] = useState('')
  const [confirmPwd, setConfirmPwd] = useState('')
  const [pwdBusy, setPwdBusy] = useState(false)
  const [pwdError, setPwdError] = useState('')

  const from = (location.state as { from?: string } | null)?.from || '/kbs'

  useEffect(() => {
    let alive = true
    metaApi
      .get()
      .then((m) => {
        if (alive) setMeta(m)
      })
      .catch(() => {
        /* 元信息失败不影响登录 */
      })
    return () => {
      alive = false
    }
  }, [])

  useEffect(() => {
    if (!loading && user && !mustChangePassword) navigate(from, { replace: true })
  }, [loading, user, mustChangePassword, navigate, from])

  useEffect(() => {
    if (mustChangePassword) {
      setPwdOpen(true)
      setOldPwd('')
      setNewPwd('')
      setConfirmPwd('')
      setPwdError('')
    }
  }, [mustChangePassword])

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!username.trim() || !password) {
      setError('请输入用户名和密码')
      return
    }
    setBusy(true)
    setError('')
    try {
      const u = await login(username.trim(), password)
      setPassword('')
      if (!u.must_change_password) navigate(from, { replace: true })
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  const submitPassword = async (e: React.FormEvent) => {
    e.preventDefault()
    setPwdError('')
    if (newPwd.length < 10) {
      setPwdError('新密码至少 10 位')
      return
    }
    if (newPwd !== confirmPwd) {
      setPwdError('两次输入的新密码不一致')
      return
    }
    if (newPwd === oldPwd) {
      setPwdError('新密码不能与原密码相同')
      return
    }
    setPwdBusy(true)
    try {
      await authApi.changePassword(oldPwd, newPwd)
      clearMustChange()
      setPwdOpen(false)
      await refreshMe()
      navigate(from, { replace: true })
    } catch (err) {
      setPwdError(errorText(err))
    } finally {
      setPwdBusy(false)
    }
  }

  if (loading) {
    return (
      <div className="boot-screen">
        <LoadingBlock text="正在检查登录状态…" />
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
              <strong style={{ fontSize: 15 }}>{meta?.app_name || '企业知识库'}</strong>
              <span>Wiki + RAG</span>
            </div>
          </div>
        </div>
        <h1>把企业文档，变成能对话的知识</h1>
        <p>
          统一收纳制度、手册与流程文档，自动分块建索引，或归纳成结构化 Wiki。
          提问时给出答案，同时把出处、版本与页码一并交回。
        </p>
        <div className="login-features">
          <div className="login-feature">
            <span className="mark">1</span>
            <span>多知识库分权管理，按人授予可读 / 可管理等级</span>
          </div>
          <div className="login-feature">
            <span className="mark">2</span>
            <span>基础分块与 Wiki 增强双模式，同一批文档两种检索形态</span>
          </div>
          <div className="login-feature">
            <span className="mark">3</span>
            <span>回答附带证据原文、文档版本与页码，可回溯可校对</span>
          </div>
        </div>
      </aside>

      <section className="login-panel">
        <div className="login-card">
        <h2>登录</h2>
        <div className="sub">使用管理员分配的账号进入系统</div>
        {error ? <div className="login-error">{error}</div> : null}
        <form onSubmit={submit}>
          <Field label="用户名" required>
            <TextInput
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="请输入用户名"
              autoComplete="username"
              autoFocus
            />
          </Field>
          <Field label="密码" required>
            <TextInput
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="请输入密码"
              autoComplete="current-password"
            />
          </Field>
          <button type="submit" className="btn btn-primary full" disabled={busy}>
            {busy ? <span className="spinner" style={{ width: 14, height: 14 }} /> : null}
            {busy ? '登录中…' : '登录'}
          </button>
        </form>
        </div>
        <div className="login-foot">
          {meta ? `${meta.app_name} · v${meta.version}` : '企业知识库文档整理与智能问答系统'}
          {meta?.offline_models ? ' · 离线模型模式' : ''}
        </div>
      </section>

      <Modal
        open={pwdOpen}
        title="首次登录，请修改密码"
        onClose={() => undefined}
        dismissable={false}
        width={440}
        footer={
          <button type="submit" form="pwd-form" className="btn btn-primary" disabled={pwdBusy}>
            {pwdBusy ? <span className="spinner" style={{ width: 14, height: 14 }} /> : null}
            保存并进入系统
          </button>
        }
      >
        <div className="confirm-text" style={{ marginBottom: 14 }}>
          为保障账号安全，首次登录必须设置新密码后才能使用系统。
        </div>
        {pwdError ? <div className="login-error">{pwdError}</div> : null}
        <form id="pwd-form" onSubmit={submitPassword}>
          <Field label="原密码" required>
            <TextInput
              type="password"
              value={oldPwd}
              onChange={(e) => setOldPwd(e.target.value)}
              autoComplete="current-password"
            />
          </Field>
          <Field label="新密码" hint="至少 10 位，包含大写、小写、数字、符号中的三类" required>
            <TextInput
              type="password"
              value={newPwd}
              onChange={(e) => setNewPwd(e.target.value)}
              autoComplete="new-password"
            />
          </Field>
          <Field label="确认新密码" required>
            <TextInput
              type="password"
              value={confirmPwd}
              onChange={(e) => setConfirmPwd(e.target.value)}
              autoComplete="new-password"
            />
          </Field>
        </form>
      </Modal>
    </div>
  )
}
