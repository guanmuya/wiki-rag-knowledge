import { useState } from 'react'
import { agentApi } from '../../api'
import type { AgentCore, AgentCoreList, AgentPluginError } from '../../types'
import { errorText } from '../../utils'

export interface AgentPluginsState {
  cores: AgentCore[]
  coreErrors: AgentPluginError[]
  pluginDir: string
  busyPlugin: string
  applyCoreList: (data: AgentCoreList) => void
  togglePlugin: (core: AgentCore) => Promise<void>
  rescanPlugins: () => Promise<void>
}

/**
 * 本页只跑本机 CLI：内置检索核心（kind 为 kb）即便后端仍返回，也不展示、不可选——
 * 检索问答由「智能问答」页负责。过滤只放在这一处，免得每个列表、下拉各自判断。
 */
function cliCores(items: AgentCore[]): AgentCore[] {
  return items.filter((core) => core.kind !== 'kb')
}

/** 核心与插件：清单列表、加载错误、插件目录与启停操作。 */
export function useAgentPlugins(notify: (text: string) => void): AgentPluginsState {
  const [cores, setCores] = useState<AgentCore[]>([])
  const [coreErrors, setCoreErrors] = useState<AgentPluginError[]>([])
  const [pluginDir, setPluginDir] = useState('')
  const [busyPlugin, setBusyPlugin] = useState('')

  function applyCoreList(data: AgentCoreList) {
    setCores(cliCores(data.items))
    setCoreErrors(data.errors)
    setPluginDir(data.plugin_dir)
  }

  async function refreshCores(): Promise<AgentCore[]> {
    const data = await agentApi.cores()
    applyCoreList(data)
    return cliCores(data.items)
  }

  async function togglePlugin(core: AgentCore) {
    setBusyPlugin(core.id)
    try {
      await agentApi.togglePlugin(core.id, !core.enabled)
      await refreshCores()
    } catch (err) {
      notify(errorText(err))
    } finally {
      setBusyPlugin('')
    }
  }

  async function rescanPlugins() {
    setBusyPlugin('__rescan__')
    try {
      await refreshCores()
      notify('已重新扫描核心与插件。')
    } catch (err) {
      notify(errorText(err))
    } finally {
      setBusyPlugin('')
    }
  }

  return {
    cores,
    coreErrors,
    pluginDir,
    busyPlugin,
    applyCoreList,
    togglePlugin,
    rescanPlugins,
  }
}
