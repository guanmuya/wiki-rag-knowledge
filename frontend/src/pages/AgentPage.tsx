import { useEffect, useMemo, useState } from 'react'
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  Field,
  LoadingBlock,
  Modal,
  TextInput,
} from '../components/ui'
import { IconAgent, IconAlert, IconPlug, IconPlus, IconTerminal } from '../components/icons'
import { AgentChat } from './agent/AgentChat'
import { PluginPanel } from './agent/PluginPanel'
import { SessionList } from './agent/SessionList'
import { coreHint, coreUsable } from './agent/constants'
import { useAgentPlugins } from './agent/useAgentPlugins'
import { useAgentSessions } from './agent/useAgentSessions'

export default function AgentPage() {
  const [notice, setNotice] = useState('')
  const [pluginsOpen, setPluginsOpen] = useState(false)

  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(''), 4000)
    return () => window.clearTimeout(timer)
  }, [notice])

  const {
    cores,
    coreErrors,
    pluginDir,
    busyPlugin,
    applyCoreList,
    togglePlugin,
    rescanPlugins,
  } = useAgentPlugins(setNotice)

  const {
    sessions,
    active,
    models,
    boot,
    loadError,
    draft,
    setDraft,
    sending,
    live,
    renaming,
    setRenaming,
    renameText,
    setRenameText,
    workspaceOpen,
    setWorkspaceOpen,
    workspaceText,
    setWorkspaceText,
    deleteTarget,
    setDeleteTarget,
    preferredCore,
    isBusy,
    scrollRef,
    textareaRef,
    openSession,
    createSession,
    send,
    stopRun,
    changeCore,
    changeModel,
    saveTitle,
    saveWorkspace,
    removeSession,
  } = useAgentSessions({ cores, applyCoreList, notify: setNotice, setPluginsOpen })

  const activeCore = useMemo(() => {
    const found = cores.find((c) => c.id === active?.core_id)
    if (found) return found
    // 清单里没有这个核心时沿用服务端返回的描述（插件被删掉的 CLI 核心），
    // 但旧版内置检索核心不再当作本页的核心。
    return active?.core && active.core.kind !== 'kb' ? active.core : null
  }, [cores, active])
  // 会话用的核心已不在清单里：旧版的内置检索核心、或插件清单被删掉的核心。
  // 这类会话不报错，但也不再执行，提示用户先换个核心。
  const coreUnavailable = Boolean(active && !cores.some((c) => c.id === active.core_id))
  const compatibleModels = useMemo(
    () => models.filter((m) => m.compatible || m.id === active?.model_config_id),
    [models, active],
  )

  if (boot) {
    return <div className="state-block"><LoadingBlock text="正在加载 Agent 核心…" /></div>
  }
  if (loadError) {
    return (
      <div className="state-block">
        <ErrorState error={loadError} onRetry={() => window.location.reload()} />
      </div>
    )
  }

  return (
    <div className="chat-shell agent-shell">
      <SessionList
        sessions={sessions}
        activeSessionId={active?.id ?? null}
        activeCore={activeCore}
        cores={cores}
        modelLabel={active?.model_label ?? ''}
        canCreate={Boolean(preferredCore)}
        onNew={() => void createSession()}
        onOpenPlugins={() => setPluginsOpen(true)}
        onOpenSession={(id) => void openSession(id)}
        onDelete={setDeleteTarget}
      />

      <section className="chat-main">
        {notice ? (
          <div className="agent-notice" role="status">
            <IconAlert size={14} />
            <span>{notice}</span>
            <button type="button" className="btn-link" onClick={() => setNotice('')}>关闭</button>
          </div>
        ) : null}

        {!active ? (
          <div className="chat-scroll">
            <div className="agent-onboarding">
              <div className="agent-hero">
                <div className="agent-hero-icon"><IconAgent size={26} /></div>
                <h1>本地个人 Agent</h1>
                <p>
                  选一个本机 CLI 核心开始：Claude Code、Codex 等以本机命令运行，
                  模型与密钥由本应用注入，随时可以换核心。
                </p>
                <div className="row row-wrap" style={{ justifyContent: 'center' }}>
                  <Button variant="primary" onClick={() => void createSession()} disabled={!preferredCore}>
                    <IconPlus size={14} /> 用 {preferredCore?.name ?? '可用核心'} 开始
                  </Button>
                  <Button variant="ghost" onClick={() => setPluginsOpen(true)}>
                    <IconPlug size={14} /> 核心与插件
                  </Button>
                </div>
              </div>

              <div className="agent-core-grid">
                {cores.map((core) => (
                  <button
                    key={core.id}
                    type="button"
                    className={`agent-core-card ${coreUsable(core) ? '' : 'disabled'}`}
                    onClick={() => void createSession(core)}
                    disabled={!coreUsable(core)}
                    title={coreHint(core)}
                  >
                    <div className="row">
                      <span className="agent-core-icon" data-kind={core.kind}>
                        <IconTerminal size={16} />
                      </span>
                      <strong>{core.name}</strong>
                      <span className="spacer" />
                      {core.builtin ? <Badge tone="muted">内置</Badge> : <Badge tone="info">插件</Badge>}
                    </div>
                    <div className="agent-core-desc">{core.description}</div>
                    <div className="agent-core-meta">
                      {core.installed
                        ? <span className="ok">{core.version || '已安装'}</span>
                        : <span className="muted">未安装 · {core.bins.join(' / ')}</span>}
                      <span className="spacer" />
                      {core.installed && !coreUsable(core)
                        ? <span className="warn-text">无兼容模型</span>
                        : <span className="muted">{core.protocol_labels.join(' / ') || '未声明协议'}</span>}
                    </div>
                  </button>
                ))}
              </div>
              {cores.length === 0 ? (
                <EmptyState
                  title="还没有可用的核心"
                  hint="先在本机安装 Claude Code / Codex 等 CLI，或在核心管理里放置插件清单。"
                  action={<Button onClick={() => setPluginsOpen(true)}>打开核心管理</Button>}
                />
              ) : null}
            </div>
          </div>
        ) : (
          <AgentChat
            active={active}
            activeCore={activeCore}
            cores={cores}
            compatibleModels={compatibleModels}
            coreUnavailable={coreUnavailable}
            isBusy={isBusy}
            live={live}
            draft={draft}
            sending={sending}
            scrollRef={scrollRef}
            textareaRef={textareaRef}
            onRename={() => {
              setRenameText(active.title)
              setRenaming(true)
            }}
            onChangeCore={(coreId) => void changeCore(coreId)}
            onChangeModel={(value) => void changeModel(value)}
            onOpenWorkspace={() => setWorkspaceOpen(true)}
            onStop={() => void stopRun()}
            onOpenPlugins={() => setPluginsOpen(true)}
            onSend={() => void send()}
            onDraftChange={setDraft}
          />
        )}
      </section>

      <PluginPanel
        open={pluginsOpen}
        cores={cores}
        coreErrors={coreErrors}
        pluginDir={pluginDir}
        busyPlugin={busyPlugin}
        onClose={() => setPluginsOpen(false)}
        onRescan={() => void rescanPlugins()}
        onTogglePlugin={(core) => void togglePlugin(core)}
        onNotify={setNotice}
      />

      <Modal
        open={renaming}
        title="重命名会话"
        width={420}
        onClose={() => setRenaming(false)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setRenaming(false)}>取消</Button>
            <Button variant="primary" onClick={() => void saveTitle()}>保存</Button>
          </>
        }
      >
        <Field label="标题">
          <TextInput
            value={renameText}
            maxLength={120}
            onChange={(e) => setRenameText(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void saveTitle() }}
            autoFocus
          />
        </Field>
      </Modal>

      <Modal
        open={workspaceOpen}
        title="CLI 工作目录"
        width={520}
        onClose={() => setWorkspaceOpen(false)}
        footer={
          <>
            <Button variant="ghost" onClick={() => setWorkspaceOpen(false)}>取消</Button>
            <Button variant="primary" onClick={() => void saveWorkspace()}>保存</Button>
          </>
        }
      >
        <Field
          label="目录绝对路径"
          hint="Agent 能读写这个目录；建议指到单独的项目文件夹，不要用仓库根目录。"
        >
          <TextInput
            value={workspaceText}
            onChange={(e) => setWorkspaceText(e.target.value)}
            placeholder="例如 C:\\Users\\me\\projects\\demo"
          />
        </Field>
      </Modal>

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title="删除会话"
        message={<>确定删除「{deleteTarget?.title}」及其全部消息？此操作不可恢复。</>}
        confirmText="删除"
        danger
        onConfirm={() => void removeSession()}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  )
}
