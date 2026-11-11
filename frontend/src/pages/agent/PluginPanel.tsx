import type { AgentCore, AgentPluginError } from '../../types'
import { Badge, Button, Field, Modal, TextInput } from '../../components/ui'
import { IconAgent, IconRefresh, IconTerminal } from '../../components/icons'
import { EXAMPLE_PLUGIN, coreUsable } from './constants'

export function PluginPanel({
  open,
  cores,
  coreErrors,
  pluginDir,
  busyPlugin,
  onClose,
  onRescan,
  onTogglePlugin,
  onNotify,
}: {
  open: boolean
  cores: AgentCore[]
  coreErrors: AgentPluginError[]
  pluginDir: string
  busyPlugin: string
  onClose: () => void
  onRescan: () => void
  onTogglePlugin: (core: AgentCore) => void
  onNotify: (text: string) => void
}) {
  async function copyPluginExample() {
    try {
      await navigator.clipboard.writeText(EXAMPLE_PLUGIN)
      onNotify('示例插件 JSON 已复制。')
    } catch {
      onNotify('复制失败，请手动选中示例内容。')
    }
  }

  return (
    <Modal
      open={open}
      title="Agent 核心与插件"
      width={760}
      onClose={onClose}
      footer={
        <>
          <span className="tiny muted" style={{ marginRight: 'auto' }}>
            插件只描述「怎么启动 CLI」，应用不会加载第三方代码。
          </span>
          <Button variant="ghost" onClick={onClose}>关闭</Button>
          <Button
            onClick={onRescan}
            loading={busyPlugin === '__rescan__'}
          >
            <IconRefresh size={14} /> 重新扫描
          </Button>
        </>
      }
    >
      <div className="agent-plugin-body">
        {coreErrors.length > 0 ? (
          <div className="agent-plugin-errors">
            <strong>有 {coreErrors.length} 个清单未能加载</strong>
            {coreErrors.map((e) => (
              <div key={e.file} className="tiny">
                <code>{e.file}</code>：{e.error}
              </div>
            ))}
          </div>
        ) : null}

        <div className="agent-plugin-list">
          {cores.map((core) => (
            <div key={core.id} className="agent-plugin-row">
              <span className="agent-core-icon" data-kind={core.kind}>
                {core.kind === 'kb' ? <IconAgent size={16} /> : <IconTerminal size={16} />}
              </span>
              <div className="agent-plugin-main">
                <div className="row">
                  <strong>{core.name}</strong>
                  {core.builtin ? <Badge tone="muted">内置</Badge> : <Badge tone="info">插件</Badge>}
                  {core.installed
                    ? <Badge tone="success">已安装</Badge>
                    : <Badge tone="warn">未安装</Badge>}
                  {core.kind === 'cli' && core.installed && !coreUsable(core)
                    ? <Badge tone="warn">无兼容模型</Badge>
                    : null}
                  {!core.enabled ? <Badge tone="danger">已停用</Badge> : null}
                </div>
                <div className="tiny muted agent-plugin-desc">{core.description}</div>
                <div className="tiny muted">
                  {core.kind === 'cli'
                    ? `${core.bins.join(' / ')}${core.version ? ` · ${core.version}` : ''}${core.path ? ` · ${core.path}` : ''}`
                    : '内置 RAG 核心，无需安装'}
                </div>
                {core.protocol_labels.length > 0 ? (
                  <div className="tiny muted">协议：{core.protocol_labels.join(' / ')}</div>
                ) : null}
              </div>
              <Button
                variant={core.enabled ? 'ghost' : 'primary'}
                size="sm"
                loading={busyPlugin === core.id}
                disabled={core.id === 'kb'}
                title={core.id === 'kb' ? '内置知识库核心不能停用' : undefined}
                onClick={() => onTogglePlugin(core)}
              >
                {core.id === 'kb' ? '内置' : core.enabled ? '停用' : '启用'}
              </Button>
            </div>
          ))}
        </div>

        <div className="divider" />
        <Field label="插件目录" hint="把 JSON 清单放到这里，点「重新扫描」即可出现。">
          <div className="row">
            <TextInput readOnly value={pluginDir} onFocus={(e) => e.currentTarget.select()} />
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                void navigator.clipboard
                  ?.writeText(pluginDir)
                  .catch(() => onNotify('复制失败，请手动选择路径'))
              }}
            >
              复制路径
            </Button>
          </div>
        </Field>
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <span className="tiny muted">示例清单（可复制后改）</span>
          <Button variant="ghost" size="sm" onClick={() => void copyPluginExample()}>复制示例</Button>
        </div>
        <pre className="agent-plugin-example">{EXAMPLE_PLUGIN}</pre>
        <div className="tiny muted">
          占位符：<code>{'{prompt}'}</code> 提示词、<code>{'{model}'}</code> 模型 id、
          <code>{'{base_url}'}</code> 供应商地址、<code>{'{api_key}'}</code> 密钥、
          <code>{'{workspace}'}</code> 工作目录。完整字段见仓库 <code>docs/AGENT_PLUGINS.md</code>。
        </div>
      </div>
    </Modal>
  )
}
