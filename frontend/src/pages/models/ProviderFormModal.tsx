import { useState } from 'react'
import { useToast } from '../../auth'
import { settingsApi } from '../../api'
import type { Provider, ProviderProtocol } from '../../types'
import { PROTOCOL_OPTIONS } from '../../types'
import { errorText } from '../../utils'
import { Button, ConfirmDialog, Field, Modal, Select, TextInput } from '../../components/ui'
import {
  PROTOCOL_HINT,
  PROVIDER_PRESETS,
  normalizedProviderBase,
  protocolOf,
} from './constants'

/** 供应商新增/编辑弹窗的状态与保存逻辑；弹窗开关放在这里，组件实例跟着页面走。 */
export function useProviderForm({
  load,
  loadEmbed,
}: {
  load: () => Promise<void>
  loadEmbed: () => Promise<void>
}) {
  const { push } = useToast()

  const [providerModal, setProviderModal] = useState<'create' | Provider | null>(null)
  const [pPreset, setPPreset] = useState('')
  const [pName, setPName] = useState('')
  const [pBase, setPBase] = useState('')
  const [pKey, setPKey] = useState('')
  const [pKeyNotice, setPKeyNotice] = useState('')
  const [pEnabled, setPEnabled] = useState(true)
  const [pProtocol, setPProtocol] = useState<ProviderProtocol>('openai')
  const [pBusy, setPBusy] = useState(false)
  const [pError, setPError] = useState('')
  const [clearKeyOpen, setClearKeyOpen] = useState(false)

  const openProvider = (target: 'create' | Provider) => {
    setProviderModal(target)
    setPError('')
    setPPreset('')
    setPKeyNotice('')
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

  return {
    providerModal,
    setProviderModal,
    pPreset,
    setPPreset,
    pName,
    setPName,
    pBase,
    setPBase,
    pKey,
    setPKey,
    pKeyNotice,
    setPKeyNotice,
    pEnabled,
    setPEnabled,
    pProtocol,
    setPProtocol,
    pBusy,
    pError,
    clearKeyOpen,
    setClearKeyOpen,
    openProvider,
    submitProvider,
    doClearKey,
  }
}

export type ProviderForm = ReturnType<typeof useProviderForm>

export function ProviderFormModal({ form }: { form: ProviderForm }) {
  const {
    providerModal,
    setProviderModal,
    pPreset,
    setPPreset,
    pName,
    setPName,
    pBase,
    setPBase,
    pKey,
    setPKey,
    pKeyNotice,
    setPKeyNotice,
    pEnabled,
    setPEnabled,
    pProtocol,
    setPProtocol,
    pBusy,
    pError,
    clearKeyOpen,
    setClearKeyOpen,
    submitProvider,
    doClearKey,
  } = form

  return (
    <>
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
        {providerModal === 'create' ? (
          <Field label="供应商预设" hint="直连供应商 API，填入后仍可修改；模型列表取决于供应商。">
            <Select
              value={pPreset}
              onChange={(e) => {
                const preset = PROVIDER_PRESETS.find((item) => item.name === e.target.value)
                setPPreset(e.target.value)
                if (preset) {
                  const oldBase = normalizedProviderBase(pBase)
                  const newBase = normalizedProviderBase(preset.baseUrl)
                  if (oldBase && oldBase !== newBase && pKey) {
                    setPKey('')
                    setPKeyNotice('供应商地址已更换，请重新填写 API Key。')
                  }
                  setPName(preset.name)
                  setPBase(preset.baseUrl)
                  setPProtocol(preset.protocol)
                }
              }}
            >
              <option value="">自定义</option>
              {PROVIDER_PRESETS.map((preset) => (
                <option key={preset.name} value={preset.name}>
                  {preset.name}
                </option>
              ))}
            </Select>
          </Field>
        ) : null}
        <Field label="名称" required>
          <TextInput
            value={pName}
            onChange={(e) => {
              setPName(e.target.value)
              setPPreset('')
            }}
            placeholder="例如：OpenAI"
            autoFocus
          />
        </Field>
        <Field label="Base URL" required hint="接口根地址，例如 https://api.openai.com/v1">
          <TextInput
            value={pBase}
            onChange={(e) => {
              setPBase(e.target.value)
              setPPreset('')
            }}
            placeholder="https://api.openai.com/v1"
            className="mono"
          />
        </Field>
        <Field label="API 协议" required hint={PROTOCOL_HINT[pProtocol]}>
          <Select
            value={pProtocol}
            onChange={(e) => {
              setPProtocol(e.target.value as ProviderProtocol)
              setPPreset('')
            }}
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
            pKeyNotice || (providerModal !== 'create' && providerModal?.has_key
              ? `当前：${providerModal.api_key_masked || '已配置'}，留空表示不修改`
              : '留空表示不设置')
          }
        >
          <TextInput
            type="password"
            value={pKey}
            onChange={(e) => {
              setPKey(e.target.value)
              setPKeyNotice('')
            }}
            placeholder={providerModal !== 'create' ? '留空不修改' : 'sk-...'}
            autoComplete="new-password"
          />
        </Field>
        <label className="check-row">
          <input
            type="checkbox"
            className="checkbox"
            checked={pEnabled}
            onChange={(e) => setPEnabled(e.target.checked)}
          />
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
    </>
  )
}
