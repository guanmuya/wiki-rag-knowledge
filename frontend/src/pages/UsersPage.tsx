import { useCallback, useEffect, useState } from 'react'
import { useAuth, useToast } from '../auth'
import { kbApi, usersApi } from '../api'
import type { GrantLevel, KnowledgeBase, Role, User, UserStatus } from '../types'
import { errorText, formatTime, taskStatusClass } from '../utils'
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Field,
  Modal,
  Select,
  SkeletonLines,
  TextInput,
} from '../components/ui'

interface GrantDraft {
  kb_id: number
  level: GrantLevel
}

export default function UsersPage() {
  const { push } = useToast()
  const { user: me } = useAuth()

  const [users, setUsers] = useState<User[]>([])
  const [kbs, setKbs] = useState<KnowledgeBase[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<unknown>(null)

  const [createOpen, setCreateOpen] = useState(false)
  const [username, setUsername] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<Role>('employee')
  const [grants, setGrants] = useState<GrantDraft[]>([])
  const [createBusy, setCreateBusy] = useState(false)
  const [createError, setCreateError] = useState('')

  const [editTarget, setEditTarget] = useState<User | null>(null)
  const [editDisplay, setEditDisplay] = useState('')
  const [editRole, setEditRole] = useState<Role>('employee')
  const [editStatus, setEditStatus] = useState<UserStatus>('active')
  const [editBusy, setEditBusy] = useState(false)
  const [editError, setEditError] = useState('')

  const [grantTarget, setGrantTarget] = useState<User | null>(null)
  const [grantDraft, setGrantDraft] = useState<GrantDraft[]>([])
  const [grantBusy, setGrantBusy] = useState(false)

  const [resetTarget, setResetTarget] = useState<User | null>(null)
  const [resetPwd, setResetPwd] = useState('')
  const [resetBusy, setResetBusy] = useState(false)
  const [resetError, setResetError] = useState('')

  const [disableTarget, setDisableTarget] = useState<User | null>(null)
  const [disableBusy, setDisableBusy] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [userData, kbData] = await Promise.all([usersApi.list(), kbApi.list()])
      setUsers(userData.items ?? [])
      setKbs(kbData.items ?? [])
      setError(null)
    } catch (err) {
      setError(err)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const openCreate = () => {
    setUsername('')
    setDisplayName('')
    setPassword('')
    setRole('employee')
    setGrants([])
    setCreateError('')
    setCreateOpen(true)
  }

  const submitCreate = async () => {
    setCreateError('')
    if (username.trim().length < 3) {
      setCreateError('用户名至少 3 个字符')
      return
    }
    if (password.length < 10) {
      setCreateError('初始密码至少 10 位')
      return
    }
    setCreateBusy(true)
    try {
      await usersApi.create({
        username: username.trim(),
        display_name: displayName.trim() || username.trim(),
        password,
        role,
        grants: role === 'admin' ? [] : grants,
      })
      push('用户已创建，首次登录需修改密码', 'success')
      setCreateOpen(false)
      await load()
    } catch (err) {
      setCreateError(errorText(err))
    } finally {
      setCreateBusy(false)
    }
  }

  const openEdit = (u: User) => {
    setEditTarget(u)
    setEditDisplay(u.display_name)
    setEditRole(u.role)
    setEditStatus(u.status)
    setEditError('')
  }

  const submitEdit = async () => {
    if (!editTarget) return
    setEditBusy(true)
    setEditError('')
    try {
      await usersApi.update(editTarget.id, {
        display_name: editDisplay.trim(),
        role: editRole,
        status: editStatus,
      })
      push('用户信息已更新', 'success')
      setEditTarget(null)
      await load()
    } catch (err) {
      setEditError(errorText(err))
    } finally {
      setEditBusy(false)
    }
  }

  const openGrants = (u: User) => {
    setGrantTarget(u)
    setGrantDraft(
      (u.grants ?? []).map((g) => ({ kb_id: g.kb_id, level: g.level })),
    )
  }

  const submitGrants = async () => {
    if (!grantTarget) return
    setGrantBusy(true)
    try {
      await usersApi.setGrants(grantTarget.id, grantDraft)
      push('授权已更新', 'success')
      setGrantTarget(null)
      await load()
    } catch (err) {
      push(errorText(err), 'error')
    } finally {
      setGrantBusy(false)
    }
  }

  const submitReset = async () => {
    if (!resetTarget) return
    setResetError('')
    if (resetPwd.length < 10) {
      setResetError('新密码至少 10 位')
      return
    }
    setResetBusy(true)
    try {
      await usersApi.resetPassword(resetTarget.id, resetPwd)
      push('密码已重置，该用户下次登录需修改密码', 'success')
      setResetTarget(null)
      setResetPwd('')
      await load()
    } catch (err) {
      setResetError(errorText(err))
    } finally {
      setResetBusy(false)
    }
  }

  const doDisable = async () => {
    if (!disableTarget) return
    setDisableBusy(true)
    try {
      await usersApi.remove(disableTarget.id)
      push('账号已停用', 'success')
      setDisableTarget(null)
      await load()
    } catch (err) {
      push(errorText(err), 'error')
    } finally {
      setDisableBusy(false)
    }
  }

  const toggleGrant = (kbId: number) => {
    setGrants((prev) =>
      prev.some((g) => g.kb_id === kbId)
        ? prev.filter((g) => g.kb_id !== kbId)
        : [...prev, { kb_id: kbId, level: 'read' }],
    )
  }

  const setGrantLevel = (kbId: number, level: GrantLevel) => {
    setGrants((prev) => prev.map((g) => (g.kb_id === kbId ? { ...g, level } : g)))
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>用户管理</h1>
          <div className="sub">创建员工账号、分配知识库权限、重置密码。管理员默认拥有全部知识库的管理权限。</div>
        </div>
        <div className="spacer" />
        <div className="page-head-actions">
          <Button variant="ghost" onClick={() => void load()}>
            刷新
          </Button>
          <Button variant="primary" onClick={openCreate}>
            + 新建用户
          </Button>
        </div>
      </div>

      <div className="card">
        {loading ? (
          <div className="card-pad">
            <SkeletonLines rows={6} />
          </div>
        ) : error ? (
          <ErrorState error={error} onRetry={() => void load()} />
        ) : users.length === 0 ? (
          <EmptyState title="暂无用户" hint="点击右上角「新建用户」创建第一个员工账号。" />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>用户名</th>
                  <th>显示名</th>
                  <th>角色</th>
                  <th>状态</th>
                  <th>授权知识库</th>
                  <th>最近登录</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr key={u.id}>
                    <td>
                      <div className="strong">{u.username}</div>
                      {u.must_change_password ? (
                        <div className="tiny" style={{ color: 'var(--c-warn)' }}>
                          待修改初始密码
                        </div>
                      ) : null}
                    </td>
                    <td>{u.display_name || '—'}</td>
                    <td>
                      <Badge tone={u.role === 'admin' ? 'info' : 'muted'}>
                        {u.role_label ?? (u.role === 'admin' ? '管理员' : '普通员工')}
                      </Badge>
                    </td>
                    <td>
                      <Badge tone={taskStatusClass(u.status)}>
                        {u.status_label ?? (u.status === 'active' ? '正常' : '已停用')}
                      </Badge>
                    </td>
                    <td>
                      {u.role === 'admin' ? (
                        <span className="small muted">全部（隐式 manage）</span>
                      ) : u.grants && u.grants.length > 0 ? (
                        <span className="small">
                          {u.grants.length} 个
                          <span className="tiny muted">
                            （
                            {u.grants
                              .slice(0, 2)
                              .map((g) => `${g.kb_name ?? `#${g.kb_id}`}:${g.level}`)
                              .join('、')}
                            {u.grants.length > 2 ? ' 等' : ''}
                            ）
                          </span>
                        </span>
                      ) : (
                        <span className="small muted">未授权</span>
                      )}
                    </td>
                    <td className="small muted nowrap">{formatTime(u.last_login_at)}</td>
                    <td className="actions">
                      <div className="table-actions">
                        <button type="button" className="btn-link" onClick={() => openEdit(u)}>
                          编辑
                        </button>
                        {u.role !== 'admin' ? (
                          <button type="button" className="btn-link" onClick={() => openGrants(u)}>
                            授权
                          </button>
                        ) : null}
                        <button
                          type="button"
                          className="btn-link"
                          onClick={() => {
                            setResetTarget(u)
                            setResetPwd('')
                            setResetError('')
                          }}
                        >
                          重置密码
                        </button>
                        {u.status === 'active' && u.id !== me?.id ? (
                          <button
                            type="button"
                            className="btn-link danger"
                            onClick={() => setDisableTarget(u)}
                          >
                            停用
                          </button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <Modal
        open={createOpen}
        title="新建用户"
        onClose={() => setCreateOpen(false)}
        width={640}
        footer={
          <>
            <Button variant="ghost" onClick={() => setCreateOpen(false)} disabled={createBusy}>
              取消
            </Button>
            <Button variant="primary" loading={createBusy} onClick={() => void submitCreate()}>
              创建用户
            </Button>
          </>
        }
      >
        {createError ? <div className="login-error">{createError}</div> : null}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0 14px' }}>
          <Field label="用户名" required hint="登录账号，创建后不可修改">
            <TextInput
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="例如：zhangsan"
              autoFocus
            />
          </Field>
          <Field label="显示名">
            <TextInput
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="例如：张三"
            />
          </Field>
          <Field label="初始密码" required hint="至少 10 位，含大小写/数字/符号三类">
            <TextInput
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="new-password"
            />
          </Field>
          <Field label="角色" required>
            <Select value={role} onChange={(e) => setRole(e.target.value as Role)}>
              <option value="employee">普通员工</option>
              <option value="admin">管理员</option>
            </Select>
          </Field>
        </div>

        {role === 'employee' ? (
          <Field label="知识库授权" hint="勾选后可选择 read（可读）或 manage（可管理）">
            {kbs.length === 0 ? (
              <div className="small muted">暂无可授权的知识库</div>
            ) : (
              <div className="grants-grid">
                {kbs.map((kb) => {
                  const g = grants.find((x) => x.kb_id === kb.id)
                  return (
                    <div className={`grant-row ${g ? 'on' : ''}`} key={kb.id}>
                      <input
                        type="checkbox"
                        checked={Boolean(g)}
                        onChange={() => toggleGrant(kb.id)}
                        style={{ accentColor: 'var(--c-primary)' }}
                      />
                      <span className="grant-name" title={kb.name}>
                        {kb.name}
                      </span>
                      {g ? (
                        <select
                          value={g.level}
                          onChange={(e) => setGrantLevel(kb.id, e.target.value as GrantLevel)}
                        >
                          <option value="read">可读</option>
                          <option value="manage">可管理</option>
                        </select>
                      ) : null}
                    </div>
                  )
                })}
              </div>
            )}
          </Field>
        ) : (
          <div className="tiny muted">管理员自动拥有全部知识库的管理权限，无需单独授权。</div>
        )}
      </Modal>

      <Modal
        open={Boolean(editTarget)}
        title={`编辑用户 · ${editTarget?.username ?? ''}`}
        onClose={() => setEditTarget(null)}
        width={480}
        footer={
          <>
            <Button variant="ghost" onClick={() => setEditTarget(null)} disabled={editBusy}>
              取消
            </Button>
            <Button variant="primary" loading={editBusy} onClick={() => void submitEdit()}>
              保存
            </Button>
          </>
        }
      >
        {editError ? <div className="login-error">{editError}</div> : null}
        <Field label="显示名">
          <TextInput value={editDisplay} onChange={(e) => setEditDisplay(e.target.value)} />
        </Field>
        <Field label="角色" hint="系统至少保留一个可用管理员">
          <Select value={editRole} onChange={(e) => setEditRole(e.target.value as Role)}>
            <option value="employee">普通员工</option>
            <option value="admin">管理员</option>
          </Select>
        </Field>
        <Field label="状态">
          <Select
            value={editStatus}
            onChange={(e) => setEditStatus(e.target.value as UserStatus)}
            disabled={editTarget?.id === me?.id}
          >
            <option value="active">正常</option>
            <option value="disabled">停用</option>
          </Select>
        </Field>
        {editTarget?.id === me?.id ? (
          <div className="tiny muted">不能停用自己的账号。</div>
        ) : null}
      </Modal>

      <Modal
        open={Boolean(grantTarget)}
        title={`编辑授权 · ${grantTarget?.username ?? ''}`}
        onClose={() => setGrantTarget(null)}
        width={640}
        footer={
          <>
            <Button variant="ghost" onClick={() => setGrantTarget(null)} disabled={grantBusy}>
              取消
            </Button>
            <Button variant="primary" loading={grantBusy} onClick={() => void submitGrants()}>
              保存授权
            </Button>
          </>
        }
      >
        <div className="tiny muted" style={{ marginBottom: 10 }}>
          全量替换该用户的知识库授权。未勾选的知识库将被移除访问权限。
        </div>
        {kbs.length === 0 ? (
          <EmptyState title="暂无可授权知识库" />
        ) : (
          <div className="grants-grid" style={{ maxHeight: 320 }}>
            {kbs.map((kb) => {
              const g = grantDraft.find((x) => x.kb_id === kb.id)
              return (
                <div className={`grant-row ${g ? 'on' : ''}`} key={kb.id}>
                  <input
                    type="checkbox"
                    checked={Boolean(g)}
                    onChange={() =>
                      setGrantDraft((prev) =>
                        prev.some((x) => x.kb_id === kb.id)
                          ? prev.filter((x) => x.kb_id !== kb.id)
                          : [...prev, { kb_id: kb.id, level: 'read' }],
                      )
                    }
                    style={{ accentColor: 'var(--c-primary)' }}
                  />
                  <span className="grant-name" title={kb.name}>
                    {kb.name}
                  </span>
                  {g ? (
                    <select
                      value={g.level}
                      onChange={(e) =>
                        setGrantDraft((prev) =>
                          prev.map((x) =>
                            x.kb_id === kb.id ? { ...x, level: e.target.value as GrantLevel } : x,
                          ),
                        )
                      }
                    >
                      <option value="read">可读</option>
                      <option value="manage">可管理</option>
                    </select>
                  ) : null}
                </div>
              )
            })}
          </div>
        )}
      </Modal>

      <Modal
        open={Boolean(resetTarget)}
        title={`重置密码 · ${resetTarget?.username ?? ''}`}
        onClose={() => setResetTarget(null)}
        width={440}
        footer={
          <>
            <Button variant="ghost" onClick={() => setResetTarget(null)} disabled={resetBusy}>
              取消
            </Button>
            <Button variant="primary" loading={resetBusy} onClick={() => void submitReset()}>
              重置
            </Button>
          </>
        }
      >
        {resetError ? <div className="login-error">{resetError}</div> : null}
        <Field label="新密码" required hint="至少 10 位；重置后该用户下次登录必须修改">
          <TextInput
            type="password"
            value={resetPwd}
            onChange={(e) => setResetPwd(e.target.value)}
            autoComplete="new-password"
            autoFocus
          />
        </Field>
      </Modal>

      <ConfirmDialog
        open={Boolean(disableTarget)}
        title="停用账号"
        message={`确认停用「${disableTarget?.username}」？停用后该账号无法登录，历史数据保留。`}
        confirmText="停用"
        danger
        busy={disableBusy}
        onConfirm={() => void doDisable()}
        onCancel={() => setDisableTarget(null)}
      />
    </>
  )
}
