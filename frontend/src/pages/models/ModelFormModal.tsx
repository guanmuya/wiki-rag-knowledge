import { useState } from 'react'
import { useToast } from '../../auth'
import { settingsApi } from '../../api'
import type { ModelConfig, ModelPayload, ModelPurpose, Provider } from '../../types'
import { errorText } from '../../utils'
import { IconChevronDown, IconChevronRight } from '../../components/icons'
import { Button, Field, Modal, Select, TextInput } from '../../components/ui'

/** 模型新增/编辑弹窗的状态、参数 JSON 校验与保存逻辑。 */
export function useModelForm({
  providers,
  load,
  loadEmbed,
}: {
  providers: Provider[]
  load: () => Promise<void>
  loadEmbed: () => Promise<void>
}) {
  const { push } = useToast()

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

  return {
    modelModal,
    setModelModal,
    mProvider,
    setMProvider,
    mPurpose,
    setMPurpose,
    mModel,
    setMModel,
    mLabel,
    setMLabel,
    mDim,
    setMDim,
    mDefault,
    setMDefault,
    mEnabled,
    setMEnabled,
    mBusy,
    mError,
    mParamsOpen,
    setMParamsOpen,
    mParams,
    setMParams,
    mParamsError,
    setMParamsError,
    openModel,
    submitModel,
  }
}

export type ModelForm = ReturnType<typeof useModelForm>

export function ModelFormModal({
  form,
  providers,
}: {
  form: ModelForm
  providers: Provider[]
}) {
  const {
    modelModal,
    setModelModal,
    mProvider,
    setMProvider,
    mPurpose,
    setMPurpose,
    mModel,
    setMModel,
    mLabel,
    setMLabel,
    mDim,
    setMDim,
    mDefault,
    setMDefault,
    mEnabled,
    setMEnabled,
    mBusy,
    mError,
    mParamsOpen,
    setMParamsOpen,
    mParams,
    setMParams,
    mParamsError,
    setMParamsError,
    submitModel,
  } = form

  return (
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
          <span className="params-caret">
            {mParamsOpen ? <IconChevronDown size={13} /> : <IconChevronRight size={13} />}
          </span>
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
              aria-label="模型参数 JSON"
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
  )
}
