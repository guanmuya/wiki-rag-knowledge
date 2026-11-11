import type { AgentCore, EvidenceItem } from '../../types'

export interface LiveRun {
  runId: number
  sessionId: number
  text: string
  logs: string[]
  evidence: EvidenceItem[]
  stage: string
  status: 'running' | 'ok' | 'failed' | 'stopped'
  error: string
}

export const EXAMPLE_PLUGIN = `{
  "schema": 1,
  "id": "my-agent",
  "name": "My Agent",
  "kind": "cli",
  "protocols": ["openai"],
  "bins": ["my-agent"],
  "run_args": ["run", "--prompt", "{prompt}", "--model", "{model}"],
  "env": {
    "OPENAI_BASE_URL": "{base_url}",
    "OPENAI_API_KEY": "{api_key}"
  },
  "output": "text"
}`

export function coreUsable(core: AgentCore): boolean {
  if (!core.available) return false
  return (core.compatible_model_count ?? 0) > 0
}

export function coreHint(core: AgentCore): string {
  if (!core.installed) return core.unavailable_reason || '未安装'
  if (!core.enabled) return '已停用'
  if (!coreUsable(core)) return '还没有协议兼容的模型，请先在「模型设置」里添加'
  return core.description
}
