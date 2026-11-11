import { useCallback, useEffect, useState } from 'react'
import { useToast } from '../auth'
import { kbApi, settingsApi } from '../api'
import type {
  EmbeddingStatus,
  KnowledgeBase,
  ModelConfig,
  ModelPurpose,
  ModelTestResult,
  Provider,
} from '../types'
import { errorText, formatMs } from '../utils'
import { IconAlert } from '../components/icons'
import { Button, ConfirmDialog, ErrorState, Modal, SkeletonLines } from '../components/ui'
import { COLLAPSED_GROUPS_KEY, PURPOSE_ORDER, readCollapsedGroups } from './models/constants'
import { AUDIT_PAGE_SIZE, AuditPanel, useAudit } from './models/AuditPanel'
import { ProviderTable } from './models/ProviderTable'
import { ModelGroups } from './models/ModelGroups'
import { EmbeddingCard, type EmbedKbView } from './models/EmbeddingCard'
import { ProviderFormModal, useProviderForm } from './models/ProviderFormModal'
import { ModelFormModal, useModelForm } from './models/ModelFormModal'
import { DiscoverModal, useDiscover } from './models/DiscoverModal'

export default function ModelsPage() {
  const { push } = useToast()

  const [tab, setTab] = useState<'models' | 'audit'>('models')

  const [providers, setProviders] = useState<Provider[]>([])
  const [models, setModels] = useState<ModelConfig[]>([])
  const [collapsedGroups, setCollapsedGroups] =
    useState<Record<ModelPurpose, boolean>>(readCollapsedGroups)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<unknown>(null)

  const [embedStatus, setEmbedStatus] = useState<EmbeddingStatus | null>(null)
  const [embedLoading, setEmbedLoading] = useState(false)
  const [embedError, setEmbedError] = useState<unknown>(null)
  const [kbs, setKbs] = useState<KnowledgeBase[]>([])

  const [deleteProvider, setDeleteProvider] = useState<Provider | null>(null)
  const [deleteModel, setDeleteModel] = useState<ModelConfig | null>(null)
  const [deleteBusy, setDeleteBusy] = useState(false)

  const [testing, setTesting] = useState<number | null>(null)
  const [testResults, setTestResults] = useState<Record<number, ModelTestResult>>({})

  const [switchTarget, setSwitchTarget] = useState<ModelConfig | null>(null)
  const [switchConfirm, setSwitchConfirm] = useState(false)
  const [switchBusy, setSwitchBusy] = useState(false)

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

  const {
    auditItems,
    auditTotal,
    auditPage,
    auditLoading,
    auditError,
    auditLoaded,
    loadAudit,
  } = useAudit(tab)
  const providerForm = useProviderForm({ load, loadEmbed })
  const modelForm = useModelForm({ providers, load, loadEmbed })
  const discover = useDiscover({ load, loadEmbed })

  /** 折叠/展开某个用途的模型表；状态写 localStorage，刷新后保持。 */
  const toggleGroup = (purpose: ModelPurpose) => {
    setCollapsedGroups((prev) => {
      const next = { ...prev, [purpose]: !prev[purpose] }
      try {
        localStorage.setItem(
          COLLAPSED_GROUPS_KEY,
          JSON.stringify(PURPOSE_ORDER.filter((item) => next[item])),
        )
      } catch {
        /* 隐私模式忽略 */
      }
      return next
    })
  }

  const toggleModel = async (m: ModelConfig) => {
    try {
      await settingsApi.updateModel(m.id, { enabled: !m.enabled })
      await load()
      await loadEmbed()
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

  const currentEmbedId = embedStatus?.current_model_config_id ?? null
  const currentEmbed = models.find((m) => m.purpose === 'embedding' && m.id === currentEmbedId)
  const currentEmbedName =
    embedStatus?.current_label || currentEmbed?.label || embedStatus?.current_model || currentEmbed?.model ||
    (currentEmbedId !== null ? `模型 #${currentEmbedId}` : '')
  const embeddingModels = models.filter((m) => m.purpose === 'embedding' && m.enabled)
  const embedKbs: EmbedKbView[] = (embedStatus?.kbs?.length ? embedStatus.kbs : kbs.map((kb) => ({
    kb_id: kb.id,
    kb_name: kb.name,
    embed_signature: kb.embed_signature,
    embed_dim: kb.embed_dim,
    chunk_count: kb.stats?.chunk_count,
  }))).map((kb) => ({
    ...kb,
    state: kb.chunk_count === 0
      ? 'unindexed'
      : !kb.embed_signature || !embedStatus?.current_signature
        ? 'unknown'
        : kb.embed_signature === embedStatus.current_signature ? 'consistent' : 'mismatch',
  }))
  const inconsistent = embedKbs.some((kb) => kb.state === 'mismatch')
  const unknownConsistency = embedKbs.some((kb) => kb.state === 'unknown') ||
    (embedKbs.length === 0 && embedStatus?.has_index === true)
  const hasIndexedKb = embedKbs.some((kb) => kb.state === 'consistent' || kb.state === 'mismatch')

  return (
    <>
      <div className="page-head">
        <div>
          <h1>模型设置</h1>
        </div>
        <div className="spacer" />
        <div className="page-head-actions">
          <Button variant="ghost" onClick={() => void load()}>
            刷新
          </Button>
          {tab === 'models' ? (
            <>
              <Button onClick={() => providerForm.openProvider('create')} disabled={loading}>
                + 供应商
              </Button>
              <Button
                variant="primary"
                onClick={() => modelForm.openModel('create')}
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
          <ProviderTable
            providers={providers}
            onCreate={() => providerForm.openProvider('create')}
            onDiscover={(p) => void discover.openDiscover(p)}
            onEdit={providerForm.openProvider}
            onDelete={(p) => setDeleteProvider(p)}
          />

          <ModelGroups
            models={models}
            collapsedGroups={collapsedGroups}
            onToggleGroup={toggleGroup}
            onCreate={(purpose) => modelForm.openModel('create', purpose)}
            onEdit={modelForm.openModel}
            onDelete={(m) => setDeleteModel(m)}
            onSetDefault={(m) => void setDefaultModel(m)}
            onToggle={(m) => void toggleModel(m)}
            onTest={(m) => void runTest(m)}
            testing={testing}
            testResults={testResults}
          />

          <EmbeddingCard
            embedLoading={embedLoading}
            embedError={embedError}
            embedStatus={embedStatus}
            currentEmbedId={currentEmbedId}
            currentEmbedName={currentEmbedName}
            embedKbs={embedKbs}
            inconsistent={inconsistent}
            unknownConsistency={unknownConsistency}
            hasIndexedKb={hasIndexedKb}
            embeddingModels={embeddingModels}
            onRefresh={() => void loadEmbed()}
            onPickTarget={(target) => {
              setSwitchTarget(target)
              setSwitchConfirm(false)
            }}
          />
        </div>
      )}

      <ProviderFormModal form={providerForm} />

      <DiscoverModal discover={discover} />

      <ModelFormModal form={modelForm} providers={providers} />

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
            <span className="banner-icon"><IconAlert size={15} /></span>
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
