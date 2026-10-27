import { useCallback, useEffect, useState } from 'react'
import { useToast } from '../auth'
import { kbApi, settingsApi } from '../api'
import type {
  AuditEntry,
  DiscoveredModel,
  EmbeddingStatus,
  KnowledgeBase,
  ModelConfig,
  ModelPayload,
  ModelPurpose,
  ModelTestResult,
  Provider,
  ProviderProtocol,
} from '../types'
import { AUDIT_ACTION_LABEL, PROTOCOL_OPTIONS } from '../types'
import { errorText, formatMs, formatTime } from '../utils'
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
  Spinner,
  TextInput,
} from '../components/ui'

const PURPOSE_LABEL: Record<ModelPurpose, string> = {
  llm: '对话模型',
  embedding: '向量模型',
  vision: '视觉模型',
}

const PURPOSE_ORDER: ModelPurpose[] = ['llm', 'embedding', 'vision']

/** 供应商协议中文短名，表格里用；完整说明在弹窗里由 PROTOCOL_OPTIONS 提供。 */
const PROTOCOL_SHORT: Record<ProviderProtocol, string> = {
  openai: 'OpenAI 兼容',
  'openai-responses': 'Responses',
  anthropic: 'Anthropic',
}

const PROTOCOL_HINT: Record<ProviderProtocol, string> = {
  openai: '/chat/completions + /embeddings，兼容绝大多数中转与自建网关。',
  'openai-responses': '/responses 新协议；不支持 Embedding。',
  anthropic: '/messages，system 独立字段；不支持 Embedding。',
}

function protocolOf(p: Provider): ProviderProtocol {
  // 老库回填/异常值统一按 openai 处理，与后端 provider_protocol() 一致
  const value = p.protocol
  if (value === 'openai-responses' || value === 'anthropic' || value === 'openai') return value
  return 'openai'
}

/** 探测结果 + 用户勾选状态，导入弹窗用。 */
interface DiscoverRow extends DiscoveredModel {
  checked: boolean
  purpose: ModelPurpose
}

export default function ModelsPage() {
  const { push } = useToast()

  const [tab, setTab] = useState<'models' | 'audit'>('models')

  const [providers, setProviders] = useState<Provider[]>([])
  const [models, setModels] = useState<ModelConfig[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<unknown>(null)

  const [embedStatus, setEmbedStatus] = useState<EmbeddingStatus | null>(null)
  const [embedLoading, setEmbedLoading] = useState(false)
  const [embedError, setEmbedError] = useState<unknown>(null)
  const [kbs, setKbs] = useState<KnowledgeBase[]>([])

  const [providerModal, setProviderModal] = useState<'create' | Provider | null>(null)
  const [pName, setPName] = useState('')
  const [pBase, setPBase] = useState('')
  const [pKey, setPKey] = useState('')
  const [pEnabled, setPEnabled] = useState(true)
  const [pProtocol, setPProtocol] = useState<ProviderProtocol>('openai')
  const [pBusy, setPBusy] = useState(false)
  const [pError, setPError] = useState('')
  const [clearKeyOpen, setClearKeyOpen] = useState(false)

  // ---- 探测模型
  const [discoverTarget, setDiscoverTarget] = useState<Provider | null>(null)
  const [discoverRows, setDiscoverRows] = useState<DiscoverRow[]>([])
  const [discoverSource, setDiscoverSource] = useState<'api' | 'builtin'>('api')
  const [discoverError, setDiscoverError] = useState('')
  const [discoverBusy, setDiscoverBusy] = useState(false)
  const [importBusy, setImportBusy] = useState(false)

  const [modelModal, setModelModal] = useState<'create' | ModelConfig | null>(null)
  const [mProvider, setMProvider] = useState<number>(0)
  const [mPurpose, setMPurpose] = useState<ModelPurpose>('llm')
  const [mModel, setMModel] = useState('')
  const [mLabel, setMLabel] = useState('')
  const [mDim, setMDim] = useState('')
  const [mDefault, setMDefault] = useState(false)
  const [mEnabled, setMEnabled] = useState(true)
  const [mBusy, setMBusy] = useState(false)
  const [mError, setMError] = useState('')
  const [mParamsOpen, setMParamsOpen] = useState(false)
  const [mParams, setMParams] = useState('{}')
  const [mParamsError, setMParamsError] = useState('')

  const [deleteProvider, setDeleteProvider] = useState<Provider | null>(null)
  const [deleteModel, setDeleteModel] = useState<ModelConfig | null>(null)
  const [deleteBusy, setDeleteBusy] = useState(false)

  const [testing, setTesting] = useState<number | null>(null)
  const [testResults, setTestResults] = useState<Record<number, ModelTestResult>>({})

  const [switchTarget, setSwitchTarget] = useState<ModelConfig | null>(null)
  const [switchConfirm, setSwitchConfirm] = useState(false)
  const [switchBusy, setSwitchBusy] = useState(false)

  // ---- 审计日志
  const [auditItems, setAuditItems] = useState<AuditEntry[]>([])
  const [auditTotal, setAuditTotal] = useState(0)
  const [auditPage, setAuditPage] = useState(1)
  const [auditLoading, setAuditLoading] = useState(false)
  const [auditError, setAuditError] = useState<unknown>(null)
  const [auditLoaded, setAuditLoaded] = useState(false)
  const AUDIT_PAGE_SIZE = 20

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [p, m] = await Promise.all([settingsApi.providers(), settingsApi.models()])
      setProviders(p.items ?? [])
      setModels(m.items ?? [])
      setError(null)
    } catch (err) {
      setError(err)
    } finally {
      setLoading(false)
    }
  }, [])

  const loadEmbed = useCallback(async () => {
    setEmbedLoading(true)
    setEmbedError(null)
    try {
      const [status, kbList] = await Promise.all([
        settingsApi.embeddingStatus(),
        kbApi.list(),
      ])
      setEmbedStatus(status)
      setKbs(kbList.items ?? [])
    } catch (err) {
      setEmbedError(err)
      setEmbedStatus(null)
    } finally {
      setEmbedLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
    void loadEmbed()
  }, [load, loadEmbed])

  const loadAudit = useCallback(
    async (page: number) => {
      setAuditLoading(true)
      setAuditError(null)
      try {
        const data = await settingsApi.audit(page, AUDIT_PAGE_SIZE)
        setAuditItems(data.items ?? [])
        setAuditTotal(data.total ?? 0)
        setAuditPage(data.page ?? page)
      } catch (err) {
        setAuditError(err)
        setAuditItems([])
      } finally {
        setAuditLoading(false)
        setAuditLoaded(true)
      }
    },
    [],
  )

  useEffect(() => {
    if (tab === 'audit' && !auditLoaded) void loadAudit(1)
  }, [tab, auditLoaded, loadAudit])

  const openProvider = (target: 'create' | Provider) => {
    setProviderModal(target)
    setPError('')
    if (target === 'create') {
      setPName('')
      setPBase('')
      setPKey('')
      setPEnabled(true)
      setPProtocol('openai')
    } else {
      setPName(target.name)
      setPBase(target.base_url)
      setPKey('')
      setPEnabled(target.enabled)
      setPProtocol(protocolOf(target))
    }
  }

  const submitProvider = async () => {
    setPError('')
    if (!pName.trim() || !pBase.trim()) {
      setPError('名称与 Base URL 均为必填')
      return
    }
    setPBusy(true)
    try {
      if (providerModal === 'create') {
        await settingsApi.createProvider({
          name: pName.trim(),
          base_url: pBase.trim(),
          api_key: pKey,
          enabled: pEnabled,
          protocol: pProtocol,
        })
        push('供应商已创建', 'success')
      } else if (providerModal) {
        await settingsApi.updateProvider(providerModal.id, {
          name: pName.trim(),
          base_url: pBase.trim(),
          api_key: pKey,
          enabled: pEnabled,
          protocol: pProtocol,
        })
        push('供应商已更新', 'success')
      }
      setProviderModal(null)
      await load()
      await loadEmbed()
    } catch (err) {
      setPError(errorText(err))
    } finally {
      setPBusy(false)
    }
  }

  /** 清空供应商 API Key：后端约定传 "__clear__"。 */
  const doClearKey = async () => {
    if (providerModal === 'create' || !providerModal) return
    setPBusy(true)
    try {
      await settingsApi.updateProvider(providerModal.id, { api_key: '__clear__' })
      push('API Key 已清空', 'success')
      setClearKeyOpen(false)
      setProviderModal(null)
      await load()
    } catch (err) {
      push(errorText(err), 'error')
    } finally {
      setPBusy(false)
    }
  }

  const openModel = (target: 'create' | ModelConfig, purpose?: ModelPurpose) => {
    setModelModal(target)
    setMError('')
    setMParamsError('')
    if (target === 'create') {
      setMProvider(providers[0]?.id ?? 0)
      setMPurpose(purpose ?? 'llm')
      setMModel('')
      setMLabel('')
      setMDim('')
      setMDefault(false)
      setMEnabled(true)
      setMParams('{}')
      setMParamsOpen(false)
    } else {
      setMProvider(target.provider_id)
      setMPurpose(target.purpose)
      setMModel(target.model)
      setMLabel(target.label)
      setMDim(target.embed_dim ? String(target.embed_dim) : '')
      setMDefault(target.is_default)
      setMEnabled(target.enabled)
      setMParams(JSON.stringify(target.params ?? {}, null, 2))
      setMParamsOpen(false)
    }
  }

  const submitModel = async () => {
    setMError('')
    setMParamsError('')
    if (!mProvider) {
      setMError('请先选择供应商')
      return
    }
    if (!mModel.trim()) {
      setMError('模型名不能为空')
      return
    }
    // 高级参数必须是 JSON 对象
    let params: Record<string, unknown> = {}
    const rawParams = mParams.trim()
    if (rawParams) {
      try {
        const parsed: unknown = JSON.parse(rawParams)
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
          setMParamsError('高级参数必须是 JSON 对象，例如 {"temperature": 0.2}')
          setMParamsOpen(true)
          return
        }
        params = parsed as Record<string, unknown>
      } catch (err) {
        setMParamsError(`JSON 解析失败：${err instanceof Error ? err.message : '格式不正确'}`)
        setMParamsOpen(true)
        return
      }
    }
    setMBusy(true)
    const payload: ModelPayload = {
      provider_id: mProvider,
      purpose: mPurpose,
      model: mModel.trim(),
      label: mLabel.trim(),
      params,
      is_default: mDefault,
      enabled: mEnabled,
      embed_dim: mPurpose === 'embedding' ? Number(mDim) || 0 : 0,
    }
    try {
      if (modelModal === 'create') {
        await settingsApi.createModel(payload)
        push('模型已添加', 'success')
      } else if (modelModal) {
        await settingsApi.updateModel(modelModal.id, payload)
        push('模型已更新', 'success')
      }
      setModelModal(null)
      await load()
      await loadEmbed()
    } catch (err) {
      setMError(errorText(err))
    } finally {
      setMBusy(false)
    }
  }

  const toggleModel = async (m: ModelConfig) => {
    try {
      await settingsApi.updateModel(m.id, { enabled: !m.enabled })
      await load()
    } catch (err) {
      push(errorText(err), 'error')
    }
  }

  const setDefaultModel = async (m: ModelConfig) => {
    try {
      await settingsApi.updateModel(m.id, { is_default: true })
      push(`已将「${m.label || m.model}」设为默认`, 'success')
      await load()
      await loadEmbed()
    } catch (err) {
      push(errorText(err), 'error')
    }
  }

  const runTest = async (m: ModelConfig) => {
    setTesting(m.id)
    try {
      const res = await settingsApi.testModel(m.id)
      setTestResults((prev) => ({ ...prev, [m.id]: res }))
      push(res.ok ? `连接成功（${formatMs(res.latency_ms)}）` : `连接失败：${res.message}`, res.ok ? 'success' : 'error')
      await load()
    } catch (err) {
      push(errorText(err), 'error')
    } finally {
      setTesting(null)
    }
  }

  /** 打开探测弹窗并立即发起一次探测。 */
  const openDiscover = async (p: Provider) => {
    setDiscoverTarget(p)
    setDiscoverRows([])
    setDiscoverError('')
    setDiscoverSource('api')
    setDiscoverBusy(true)
    try {
      const res = await settingsApi.discoverModels(p.id)
      setDiscoverSource(res.source)
      setDiscoverError(res.error || '')
      // 已导入的默认不勾选：避免用户「全选导入」后看到一堆「已存在」跳过项
      setDiscoverRows(
        (res.items ?? []).map((item) => ({
          ...item,
          checked: !item.already_imported,
          purpose: item.purpose_hint,
        })),
      )
    } catch (err) {
      // 后端承诺不 500，这里兜的是网络层（断网/401 等）
      setDiscoverError(errorText(err))
    } finally {
      setDiscoverBusy(false)
    }
  }

  const closeDiscover = () => {
    setDiscoverTarget(null)
    setDiscoverRows([])
    setDiscoverError('')
  }

  const toggleDiscoverRow = (model: string) => {
    setDiscoverRows((prev) =>
      prev.map((r) => (r.model === model ? { ...r, checked: !r.checked } : r)),
    )
  }

  const setDiscoverPurpose = (model: string, purpose: ModelPurpose) => {
    setDiscoverRows((prev) => prev.map((r) => (r.model === model ? { ...r, purpose } : r)))
  }

  const toggleDiscoverAll = (checked: boolean) => {
    setDiscoverRows((prev) => prev.map((r) => ({ ...r, checked })))
  }

  const doImport = async () => {
    if (!discoverTarget) return
    const picked = discoverRows.filter((r) => r.checked)
    if (picked.length === 0) {
      setDiscoverError('请至少勾选一个模型')
      return
    }
    setImportBusy(true)
    setDiscoverError('')
    try {
      const res = await settingsApi.importModels(
        discoverTarget.id,
        picked.map((r) => ({ model: r.model, purpose: r.purpose, label: r.label })),
      )
      const created = res.created_count ?? res.created?.length ?? 0
      const skipped = res.skipped_count ?? res.skipped?.length ?? 0
      push(
        skipped
          ? `已导入 ${created} 个模型，跳过 ${skipped} 个（已存在）`
          : `已导入 ${created} 个模型`,
        created > 0 ? 'success' : 'info',
      )
      closeDiscover()
      await load()
      await loadEmbed()
    } catch (err) {
      setDiscoverError(errorText(err))
    } finally {
      setImportBusy(false)
    }
  }

  const doDeleteProvider = async () => {
    if (!deleteProvider) return
    setDeleteBusy(true)
    try {
      await settingsApi.removeProvider(deleteProvider.id)
      push('供应商及其模型已删除', 'success')
      setDeleteProvider(null)
      await load()
      await loadEmbed()
    } catch (err) {
      push(errorText(err), 'error')
    } finally {
      setDeleteBusy(false)
    }
  }

  const doDeleteModel = async () => {
    if (!deleteModel) return
    setDeleteBusy(true)
    try {
      await settingsApi.removeModel(deleteModel.id)
      push('模型已删除', 'success')
      setDeleteModel(null)
      await load()
      await loadEmbed()
    } catch (err) {
      push(errorText(err), 'error')
    } finally {
      setDeleteBusy(false)
    }
  }

  const doSwitchEmbedding = async () => {
    if (!switchTarget) return
    setSwitchBusy(true)
    try {
      const res = await settingsApi.switchEmbedding(switchTarget.id, true)
      push(
        res.message || (res.task_id ? `已提交全量重建任务 #${res.task_id}` : 'Embedding 已切换'),
        'success',
      )
      setSwitchTarget(null)
      await load()
      await loadEmbed()
    } catch (err) {
      push(errorText(err), 'error')
    } finally {
      setSwitchBusy(false)
    }
  }

  const currentEmbed =
    models.find((m) => m.purpose === 'embedding' && m.is_default) ??
    models.find((m) => m.purpose === 'embedding')
  const embeddingModels = models.filter((m) => m.purpose === 'embedding' && m.enabled)
  const inconsistent =
    embedStatus?.consistent === false ||
    (embedStatus?.kbs ?? []).some((k) => k.consistent === false)

  return (
    <>
      <div className="page-head">
        <div>
          <h1>模型设置</h1>
          <div className="sub">
            {tab === 'models'
              ? '配置模型供应商与用途模型。API Key 仅写入不读取，留空表示不修改。'
              : '记录登录、上传、授权、模型变更等管理操作，按时间倒序。'}
          </div>
        </div>
        <div className="spacer" />
        <div className="page-head-actions">
          <Button variant="ghost" onClick={() => void load()}>
            刷新
          </Button>
          {tab === 'models' ? (
            <>
              <Button onClick={() => openProvider('create')} disabled={loading}>
                + 供应商
              </Button>
              <Button
                variant="primary"
                onClick={() => openModel('create')}
                disabled={loading || providers.length === 0}
                title={providers.length === 0 ? '请先创建供应商' : undefined}
              >
                + 模型
              </Button>
            </>
          ) : (
            <Button variant="primary" loading={auditLoading} onClick={() => void loadAudit(auditPage)}>
              刷新日志
            </Button>
          )}
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="tabs" style={{ padding: '0 12px', borderBottom: 'none' }}>
          <button
            type="button"
            className={`tab ${tab === 'models' ? 'active' : ''}`}
            onClick={() => setTab('models')}
          >
            供应商与模型
          </button>
          <button
            type="button"
            className={`tab ${tab === 'audit' ? 'active' : ''}`}
            onClick={() => setTab('audit')}
          >
            审计日志
            {auditLoaded ? <span className="tab-badge">{auditTotal}</span> : null}
          </button>
        </div>
      </div>

      {tab === 'audit' ? (
        <AuditPanel
          items={auditItems}
          total={auditTotal}
          page={auditPage}
          pageSize={AUDIT_PAGE_SIZE}
          loading={auditLoading}
          error={auditError}
          onPage={(p) => void loadAudit(p)}
        />
      ) : loading ? (
        <div className="card card-pad">
          <SkeletonLines rows={6} />
        </div>
      ) : error ? (
        <div className="card">
          <ErrorState error={error} onRetry={() => void load()} />
        </div>
      ) : (
        <div className="col" style={{ gap: 18 }}>
          <div className="card">
            <div className="card-head">
              <h3>供应商</h3>
              <span className="tiny muted">{providers.length} 个</span>
            </div>
            <div className="card-body card-body-flush">
              {providers.length === 0 ? (
                <EmptyState
                  title="还没有配置供应商"
                  hint="先添加 OpenAI 兼容的 API 供应商（base_url + API Key），再添加具体模型。"
                  action={
                    <Button size="sm" variant="primary" onClick={() => openProvider('create')}>
                      添加供应商
                    </Button>
                  }
                />
              ) : (
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>名称</th>
                        <th>协议</th>
                        <th>Base URL</th>
                        <th>API Key</th>
                        <th>状态</th>
                        <th>创建时间</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {providers.map((p) => (
                        <tr key={p.id}>
                          <td className="strong">{p.name}</td>
                          <td>
                            <span title={PROTOCOL_HINT[protocolOf(p)]}>
                              <Badge tone={protocolOf(p) === 'openai' ? 'muted' : 'info'}>
                                {PROTOCOL_SHORT[protocolOf(p)]}
                              </Badge>
                            </span>
                          </td>
                          <td className="mono small">{p.base_url}</td>
                          <td>
                            {p.has_key ? (
                              <span className="mono small">{p.api_key_masked || '****'}</span>
                            ) : (
                              <span className="small muted">未配置</span>
                            )}
                          </td>
                          <td>
                            <Badge tone={p.enabled ? 'success' : 'muted'}>
                              {p.enabled ? '启用' : '停用'}
                            </Badge>
                          </td>
                          <td className="small muted nowrap">{formatTime(p.created_at)}</td>
                          <td className="actions">
                            <div className="table-actions">
                              <button
                                type="button"
                                className="btn-link"
                                onClick={() => void openDiscover(p)}
                              >
                                探测模型
                              </button>
                              <button
                                type="button"
                                className="btn-link"
                                onClick={() => openProvider(p)}
                              >
                                编辑
                              </button>
                              <button
                                type="button"
                                className="btn-link danger"
                                onClick={() => setDeleteProvider(p)}
                              >
                                删除
                              </button>
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <h3>模型</h3>
              <span className="tiny muted">{models.length} 个</span>
            </div>
            <div className="card-body">
              {models.length === 0 ? (
                <EmptyState
                  title="还没有配置模型"
                  hint="按用途添加：对话模型（llm）、向量模型（embedding）、视觉模型（vision）。"
                />
              ) : (
                PURPOSE_ORDER.map((purpose) => {
                  const group = models.filter((m) => m.purpose === purpose)
                  if (group.length === 0) return null
                  return (
                    <div className="model-group" key={purpose}>
                      <div className="model-group-title">
                        {PURPOSE_LABEL[purpose]}
                        <span className="tiny muted">{group.length}</span>
                        <button
                          type="button"
                          className="btn-link"
                          onClick={() => openModel('create', purpose)}
                        >
                          + 添加
                        </button>
                      </div>
                      <div className="table-wrap">
                        <table className="table">
                          <thead>
                            <tr>
                              <th>模型</th>
                              <th>供应商</th>
                              {purpose === 'embedding' ? <th>维度</th> : null}
                              <th>默认</th>
                              <th>启用</th>
                              <th>最近测试</th>
                              <th />
                            </tr>
                          </thead>
                          <tbody>
                            {group.map((m) => {
                              const res = testResults[m.id]
                              return (
                                <tr key={m.id}>
                                  <td>
                                    <div className="strong">{m.label || m.model}</div>
                                    <div className="mono tiny muted">{m.model}</div>
                                  </td>
                                  <td className="small">{m.provider_name}</td>
                                  {purpose === 'embedding' ? (
                                    <td className="small">{m.embed_dim || '—'}</td>
                                  ) : null}
                                  <td>
                                    {m.is_default ? (
                                      <Badge tone="info">默认</Badge>
                                    ) : (
                                      <button
                                        type="button"
                                        className="btn-link"
                                        onClick={() => void setDefaultModel(m)}
                                      >
                                        设为默认
                                      </button>
                                    )}
                                  </td>
                                  <td>
                                    <label className="switch">
                                      <input
                                        type="checkbox"
                                        checked={m.enabled}
                                        onChange={() => void toggleModel(m)}
                                      />
                                      <span className="switch-track" />
                                    </label>
                                  </td>
                                  <td>
                                    <div className="row" style={{ gap: 8 }}>
                                      <Button
                                        size="sm"
                                        variant="ghost"
                                        loading={testing === m.id}
                                        onClick={() => void runTest(m)}
                                      >
                                        连接测试
                                      </Button>
                                      {res ? (
                                        <span
                                          className={`test-result ${
                                            res.ok ? 'test-ok' : 'test-fail'
                                          }`}
                                        >
                                          {res.ok ? '✓' : '✕'} {res.message}
                                          {res.ok ? ` · ${formatMs(res.latency_ms)}` : ''}
                                        </span>
                                      ) : m.last_test_at ? (
                                        <span
                                          className={`test-result ${
                                            m.last_test_ok ? 'test-ok' : 'test-fail'
                                          }`}
                                          title={m.last_test_message}
                                        >
                                          {m.last_test_ok ? '✓' : '✕'} {formatTime(m.last_test_at)}
                                        </span>
                                      ) : (
                                        <span className="tiny muted">未测试</span>
                                      )}
                                    </div>
                                  </td>
                                  <td className="actions">
                                    <div className="table-actions">
                                      <button
                                        type="button"
                                        className="btn-link"
                                        onClick={() => openModel(m)}
                                      >
                                        编辑
                                      </button>
                                      <button
                                        type="button"
                                        className="btn-link danger"
                                        onClick={() => setDeleteModel(m)}
                                      >
                                        删除
                                      </button>
                                    </div>
                                  </td>
                                </tr>
                              )
                            })}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  )
                })
              )}
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <h3>Embedding 切换</h3>
              <div className="spacer" />
              <Button size="sm" variant="ghost" onClick={() => void loadEmbed()}>
                刷新
              </Button>
            </div>
            <div className="card-body">
              {embedLoading ? (
                <div className="state-block inline">
                  <Spinner size={14} /> 状态加载中…
                </div>
              ) : embedError ? (
                <ErrorState error={embedError} onRetry={() => void loadEmbed()} />
              ) : (
                <>
                  <dl className="info-list">
                    <dt>当前 Embedding</dt>
                    <dd>
                      {embedStatus?.current_label || embedStatus?.current_model || currentEmbed?.label || currentEmbed?.model || (
                        <span className="muted">未配置</span>
                      )}
                      {embedStatus?.current_provider ? (
                        <span className="tiny muted"> · {embedStatus.current_provider}</span>
                      ) : null}
                    </dd>
                    <dt>当前签名</dt>
                    <dd>
                      {embedStatus?.current_signature || currentEmbed?.embed_signature ? (
                        <span className="sig-box">
                          {embedStatus?.current_signature || currentEmbed?.embed_signature}
                        </span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </dd>
                    <dt>各库一致性</dt>
                    <dd>
                      {inconsistent ? (
                        <Badge tone="warn">存在签名不一致的知识库</Badge>
                      ) : (
                        <Badge tone="success">一致</Badge>
                      )}
                    </dd>
                    <dt>重建状态</dt>
                    <dd>
                      {embedStatus?.rebuilding ? (
                        <Badge tone="warn">全量重建进行中</Badge>
                      ) : (
                        <span className="muted">无进行中的重建任务</span>
                      )}
                    </dd>
                  </dl>

                  {(embedStatus?.kbs ?? []).length > 0 ? (
                    <div className="embed-status-grid">
                      {(embedStatus?.kbs ?? []).map((k) => (
                        <div className={`embed-kb ${k.consistent ? '' : 'bad'}`} key={k.kb_id}>
                          <div className="row" style={{ gap: 6 }}>
                            <span className="truncate strong" style={{ fontSize: 12.5 }}>
                              {k.kb_name}
                            </span>
                            <span className="spacer" />
                            <Badge tone={k.consistent ? 'success' : 'warn'}>
                              {k.consistent ? '一致' : '需重建'}
                            </Badge>
                          </div>
                          <div className="mono tiny muted" style={{ marginTop: 4 }}>
                            {k.embed_signature || '未建立索引'} · {k.embed_dim || 0} 维
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : kbs.length > 0 ? (
                    <div className="embed-status-grid">
                      {kbs.map((kb) => (
                        <div className="embed-kb" key={kb.id}>
                          <div className="row" style={{ gap: 6 }}>
                            <span className="truncate strong" style={{ fontSize: 12.5 }}>
                              {kb.name}
                            </span>
                            <span className="spacer" />
                            <Badge
                              tone={
                                !kb.embed_signature
                                  ? 'muted'
                                  : kb.embed_signature ===
                                    (embedStatus?.current_signature ?? currentEmbed?.embed_signature)
                                  ? 'success'
                                  : 'warn'
                              }
                            >
                              {!kb.embed_signature
                                ? '无索引'
                                : kb.embed_signature ===
                                  (embedStatus?.current_signature ?? currentEmbed?.embed_signature)
                                ? '一致'
                                : '需重建'}
                            </Badge>
                          </div>
                          <div className="mono tiny muted" style={{ marginTop: 4 }}>
                            {kb.embed_signature || '未建立索引'} · {kb.embed_dim || 0} 维
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : null}

                  <div className="divider" />
                  <Field
                    label="切换 Embedding 模型"
                    hint="切换后已有索引的向量维度不兼容，需要全量重建所有知识库的分块索引。"
                  >
                    <div className="row row-wrap">
                      <Select
                        value={String(currentEmbed?.id ?? '')}
                        onChange={(e) => {
                          const target = embeddingModels.find((m) => String(m.id) === e.target.value)
                          if (target) {
                            setSwitchTarget(target)
                            setSwitchConfirm(false)
                          }
                        }}
                        style={{ maxWidth: 320 }}
                      >
                        <option value="">
                          {currentEmbed ? `${currentEmbed.label || currentEmbed.model}（当前）` : '选择向量模型'}
                        </option>
                        {embeddingModels
                          .filter((m) => m.id !== currentEmbed?.id)
                          .map((m) => (
                            <option key={m.id} value={m.id}>
                              {m.label || m.model} · {m.provider_name} · {m.embed_dim || '?'} 维
                            </option>
                          ))}
                      </Select>
                    </div>
                  </Field>

                  {inconsistent ? (
                    <div className="insufficient-banner">
                      <span>⚠</span>
                      <div style={{ flex: 1 }}>
                        检测到部分知识库的向量签名与当前 Embedding 不一致，这些库的向量检索可能失效。
                        建议切换到目标模型时执行全量重建。
                      </div>
                    </div>
                  ) : null}
                </>
              )}
            </div>
          </div>
        </div>
      )}

      <Modal
        open={providerModal !== null}
        title={providerModal === 'create' ? '添加供应商' : '编辑供应商'}
        onClose={() => setProviderModal(null)}
        width={520}
        footer={
          <>
            <Button variant="ghost" onClick={() => setProviderModal(null)} disabled={pBusy}>
              取消
            </Button>
            <Button variant="primary" loading={pBusy} onClick={() => void submitProvider()}>
              保存
            </Button>
          </>
        }
      >
        {pError ? <div className="login-error">{pError}</div> : null}
        <Field label="名称" required>
          <TextInput
            value={pName}
            onChange={(e) => setPName(e.target.value)}
            placeholder="例如：OpenAI"
            autoFocus
          />
        </Field>
        <Field label="Base URL" required hint="接口根地址，例如 https://api.openai.com/v1">
          <TextInput
            value={pBase}
            onChange={(e) => setPBase(e.target.value)}
            placeholder="https://api.openai.com/v1"
            className="mono"
          />
        </Field>
        <Field label="API 协议" required hint={PROTOCOL_HINT[pProtocol]}>
          <Select
            value={pProtocol}
            onChange={(e) => setPProtocol(e.target.value as ProviderProtocol)}
          >
            {PROTOCOL_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </Select>
        </Field>
        <Field
          label="API Key"
          hint={
            providerModal !== 'create' && providerModal?.has_key
              ? `当前：${providerModal.api_key_masked || '已配置'}，留空表示不修改`
              : '留空表示不设置'
          }
        >
          <TextInput
            type="password"
            value={pKey}
            onChange={(e) => setPKey(e.target.value)}
            placeholder={providerModal !== 'create' ? '留空不修改' : 'sk-...'}
            autoComplete="new-password"
          />
        </Field>
        <label className="checkbox">
          <input type="checkbox" checked={pEnabled} onChange={(e) => setPEnabled(e.target.checked)} />
          启用该供应商
        </label>
        {providerModal !== 'create' && providerModal?.has_key ? (
          <div style={{ marginTop: 12 }}>
            <button type="button" className="btn-link danger" onClick={() => setClearKeyOpen(true)}>
              清空已保存的 API Key
            </button>
            <div className="tiny muted" style={{ marginTop: 4 }}>
              清空后该供应商下的模型将无法调用，需要重新填写 Key。
            </div>
          </div>
        ) : null}
      </Modal>

      <Modal
        open={Boolean(discoverTarget)}
        title={`探测模型 · ${discoverTarget?.name ?? ''}`}
        onClose={closeDiscover}
        width={640}
        footer={
          <>
            <Button variant="ghost" onClick={closeDiscover} disabled={importBusy}>
              取消
            </Button>
            <Button
              variant="primary"
              loading={importBusy}
              disabled={discoverBusy || discoverRows.every((r) => !r.checked)}
              onClick={() => void doImport()}
            >
              导入所选（{discoverRows.filter((r) => r.checked).length}）
            </Button>
          </>
        }
      >
        {discoverBusy ? (
          <div className="state-block inline">
            <Spinner size={14} /> 正在向供应商请求模型列表…
          </div>
        ) : (
          <>
            {discoverError ? (
              <div className="insufficient-banner" style={{ marginBottom: 12 }}>
                <span>⚠</span>
                <div style={{ flex: 1 }}>
                  探测失败：{discoverError}
                  <div className="tiny muted" style={{ marginTop: 4 }}>
                    可改用「+ 模型」手动填写模型名；接口不支持的供应商不会影响已有配置。
                  </div>
                </div>
              </div>
            ) : null}
            {discoverSource === 'builtin' ? (
              <div className="insufficient-banner" style={{ marginBottom: 12 }}>
                <span>ℹ</span>
                <div style={{ flex: 1 }}>
                  该供应商没有可用的模型列表接口，下面显示的是内置常用清单，请核对后导入。
                </div>
              </div>
            ) : null}

            {discoverRows.length === 0 ? (
              <EmptyState
                title="没有探测到可用模型"
                hint="确认 Base URL、API Key 与协议是否匹配；也可以直接手动添加模型。"
              />
            ) : (
              <>
                <div className="row" style={{ gap: 10, marginBottom: 8 }}>
                  <button
                    type="button"
                    className="btn-link"
                    onClick={() => toggleDiscoverAll(true)}
                  >
                    全选
                  </button>
                  <button
                    type="button"
                    className="btn-link"
                    onClick={() => toggleDiscoverAll(false)}
                  >
                    全不选
                  </button>
                  <span className="spacer" />
                  <span className="tiny muted">共 {discoverRows.length} 个</span>
                </div>
                <div className="table-wrap" style={{ maxHeight: 380, overflowY: 'auto' }}>
                  <table className="table">
                    <thead>
                      <tr>
                        <th style={{ width: 40 }} />
                        <th>模型</th>
                        <th style={{ width: 150 }}>用途</th>
                      </tr>
                    </thead>
                    <tbody>
                      {discoverRows.map((r) => (
                        <tr key={r.model}>
                          <td>
                            <input
                              type="checkbox"
                              checked={r.checked}
                              onChange={() => toggleDiscoverRow(r.model)}
                              aria-label={`选择 ${r.model}`}
                            />
                          </td>
                          <td>
                            <div className="mono small">{r.model}</div>
                            {r.label && r.label !== r.model ? (
                              <div className="tiny muted">{r.label}</div>
                            ) : null}
                            {r.already_imported ? (
                              <div className="tiny muted">已存在同用途配置</div>
                            ) : null}
                          </td>
                          <td>
                            <Select
                              value={r.purpose}
                              onChange={(e) =>
                                setDiscoverPurpose(r.model, e.target.value as ModelPurpose)
                              }
                              style={{ minWidth: 120 }}
                            >
                              <option value="llm">对话 llm</option>
                              <option value="embedding">向量 embedding</option>
                              <option value="vision">视觉 vision</option>
                            </Select>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </>
        )}
      </Modal>

      <ConfirmDialog
        open={clearKeyOpen}
        title="清空 API Key"
        message={`确认清空供应商「${
          providerModal !== 'create' ? providerModal?.name ?? '' : ''
        }」的 API Key？清空后其下所有模型将无法调用，需重新填写。`}
        confirmText="确认清空"
        danger
        busy={pBusy}
        onConfirm={() => void doClearKey()}
        onCancel={() => setClearKeyOpen(false)}
      />

      <Modal
        open={modelModal !== null}
        title={modelModal === 'create' ? '添加模型' : '编辑模型'}
        onClose={() => setModelModal(null)}
        width={520}
        footer={
          <>
            <Button variant="ghost" onClick={() => setModelModal(null)} disabled={mBusy}>
              取消
            </Button>
            <Button variant="primary" loading={mBusy} onClick={() => void submitModel()}>
              保存
            </Button>
          </>
        }
      >
        {mError ? <div className="login-error">{mError}</div> : null}
        <Field label="供应商" required>
          <Select
            value={String(mProvider)}
            onChange={(e) => setMProvider(Number(e.target.value))}
            disabled={modelModal !== 'create'}
          >
            {providers.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="用途" required>
          <Select
            value={mPurpose}
            onChange={(e) => setMPurpose(e.target.value as ModelPurpose)}
            disabled={modelModal !== 'create'}
          >
            <option value="llm">对话模型 llm</option>
            <option value="embedding">向量模型 embedding</option>
            <option value="vision">视觉模型 vision</option>
          </Select>
        </Field>
        <Field label="模型名" required hint="供应商侧的真实模型标识，例如 gpt-4o-mini">
          <TextInput
            value={mModel}
            onChange={(e) => setMModel(e.target.value)}
            placeholder="gpt-4o-mini"
            className="mono"
          />
        </Field>
        <Field label="显示名" hint="用于界面展示，留空则显示模型名">
          <TextInput value={mLabel} onChange={(e) => setMLabel(e.target.value)} placeholder="例如：主对话模型" />
        </Field>
        {mPurpose === 'embedding' ? (
          <Field label="向量维度" hint="与模型输出维度一致，例如 text-embedding-3-small 为 1536">
            <TextInput
              value={mDim}
              onChange={(e) => setMDim(e.target.value.replace(/[^0-9]/g, ''))}
              placeholder="1536"
              className="mono"
            />
          </Field>
        ) : null}

        <div className="params-section">
          <button
            type="button"
            className="params-toggle"
            onClick={() => setMParamsOpen((v) => !v)}
            aria-expanded={mParamsOpen}
          >
            <span className="params-caret">{mParamsOpen ? '▾' : '▸'}</span>
            高级参数（JSON）
            {!mParamsOpen && mParams.trim() && mParams.trim() !== '{}' ? (
              <span className="tiny muted">已设置</span>
            ) : null}
          </button>
          {mParamsOpen ? (
            <div className="params-body">
              <div className="tiny muted" style={{ marginBottom: 6 }}>
                透传给供应商的额外参数，例如 temperature、max_tokens、top_p、dimensions。
                留空或 {'{}'} 表示不设置。
              </div>
              <textarea
                className="input textarea code params-textarea"
                value={mParams}
                onChange={(e) => {
                  setMParams(e.target.value)
                  setMParamsError('')
                }}
                spellCheck={false}
                placeholder={'{\n  "temperature": 0.2,\n  "max_tokens": 1500\n}'}
              />
              {mParamsError ? <div className="field-error">{mParamsError}</div> : null}
              <div className="row" style={{ gap: 8, marginTop: 6 }}>
                <button
                  type="button"
                  className="btn-link"
                  onClick={() => {
                    setMParams('{}')
                    setMParamsError('')
                  }}
                >
                  清空
                </button>
                <button
                  type="button"
                  className="btn-link"
                  onClick={() => {
                    try {
                      const parsed: unknown = JSON.parse(mParams || '{}')
                      setMParams(JSON.stringify(parsed, null, 2))
                      setMParamsError('')
                    } catch (err) {
                      setMParamsError(
                        `JSON 解析失败：${err instanceof Error ? err.message : '格式不正确'}`,
                      )
                    }
                  }}
                >
                  格式化
                </button>
              </div>
            </div>
          ) : null}
        </div>
        <div className="row" style={{ gap: 18, marginTop: 4 }}>
          <label className="checkbox">
            <input type="checkbox" checked={mDefault} onChange={(e) => setMDefault(e.target.checked)} />
            设为该用途默认模型
          </label>
          <label className="checkbox">
            <input type="checkbox" checked={mEnabled} onChange={(e) => setMEnabled(e.target.checked)} />
            启用
          </label>
        </div>
      </Modal>

      <ConfirmDialog
        open={Boolean(deleteProvider)}
        title="删除供应商"
        message={`确认删除供应商「${deleteProvider?.name}」？其下所有模型会一并删除，相关索引将无法继续使用。`}
        confirmText="删除"
        danger
        busy={deleteBusy}
        onConfirm={() => void doDeleteProvider()}
        onCancel={() => setDeleteProvider(null)}
      />

      <ConfirmDialog
        open={Boolean(deleteModel)}
        title="删除模型"
        message={`确认删除模型「${deleteModel?.label || deleteModel?.model}」？`}
        confirmText="删除"
        danger
        busy={deleteBusy}
        onConfirm={() => void doDeleteModel()}
        onCancel={() => setDeleteModel(null)}
      />

      <Modal
        open={Boolean(switchTarget)}
        title="切换 Embedding 模型"
        onClose={() => {
          setSwitchTarget(null)
          setSwitchConfirm(false)
        }}
        width={520}
        footer={
          <>
            <Button
              variant="ghost"
              onClick={() => {
                setSwitchTarget(null)
                setSwitchConfirm(false)
              }}
              disabled={switchBusy}
            >
              取消
            </Button>
            <Button
              variant="primary"
              loading={switchBusy}
              onClick={() => {
                if (switchConfirm) void doSwitchEmbedding()
                else setSwitchConfirm(true)
              }}
            >
              {switchConfirm ? '确认切换并全量重建' : '下一步'}
            </Button>
          </>
        }
      >
        <div className="confirm-text">
          将 Embedding 切换为「{switchTarget?.label || switchTarget?.model}」（{switchTarget?.provider_name}，
          {switchTarget?.embed_dim || '?'} 维）。
        </div>
        {switchConfirm ? (
          <div className="insufficient-banner" style={{ marginTop: 12 }}>
            <span>⚠</span>
            <div>
              {embedStatus?.has_index === false
                ? '当前还没有任何分块索引，切换后无需重建，可立即生效。'
                : '已有索引的向量与新模型不兼容，切换后将触发所有知识库的全量重建任务，期间问答结果可能不完整。请确认继续。'}
            </div>
          </div>
        ) : null}
      </Modal>
    </>
  )
}

function AuditPanel({
  items,
  total,
  page,
  pageSize,
  loading,
  error,
  onPage,
}: {
  items: AuditEntry[]
  total: number
  page: number
  pageSize: number
  loading: boolean
  error: unknown
  onPage: (page: number) => void
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize))

  return (
    <div className="card">
      <div className="card-head">
        <h3>审计日志</h3>
        <span className="tiny muted">共 {total} 条</span>
      </div>
      <div className="card-body card-body-flush">
        {loading ? (
          <div style={{ padding: 16 }}>
            <SkeletonLines rows={8} />
          </div>
        ) : error ? (
          <ErrorState error={error} onRetry={() => onPage(page)} />
        ) : items.length === 0 ? (
          <EmptyState
            title="暂无审计记录"
            hint="登录、上传文档、调整授权、修改模型等操作都会记录在这里。"
          />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: 150 }}>时间</th>
                  <th style={{ width: 140 }}>用户</th>
                  <th style={{ width: 120 }}>操作</th>
                  <th style={{ width: 200 }}>对象</th>
                  <th>详情</th>
                </tr>
              </thead>
              <tbody>
                {items.map((row) => (
                  <tr key={row.id}>
                    <td className="small muted nowrap">{formatTime(row.created_at)}</td>
                    <td className="small">{row.username || '—'}</td>
                    <td>
                      <Badge tone="muted">{AUDIT_ACTION_LABEL[row.action] ?? row.action}</Badge>
                    </td>
                    <td className="small truncate" style={{ maxWidth: 200 }} title={row.target}>
                      {row.target || '—'}
                    </td>
                    <td className="small muted" style={{ wordBreak: 'break-word' }}>
                      {row.detail || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <div className="pager">
        <span className="tiny muted">
          第 {page} / {pages} 页 · 每页 {pageSize} 条
        </span>
        <div className="spacer" />
        <Button size="sm" disabled={page <= 1 || loading} onClick={() => onPage(1)}>
          首页
        </Button>
        <Button size="sm" disabled={page <= 1 || loading} onClick={() => onPage(page - 1)}>
          上一页
        </Button>
        <Button size="sm" disabled={page >= pages || loading} onClick={() => onPage(page + 1)}>
          下一页
        </Button>
        <Button size="sm" disabled={page >= pages || loading} onClick={() => onPage(pages)}>
          末页
        </Button>
      </div>
    </div>
  )
}
