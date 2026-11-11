import { getToken } from '../../api'
import type {
  AskResponse,
  AskStreamEvent,
  ChatMessage,
  ConversationDetail,
  PipelineStep,
  ThinkingStream,
} from '../../types'

// Keep only this page's outstanding requests across an ordinary route unmount.
// The server remains authoritative after a reload or in another browser tab.
export type AskJob = {
  key: number
  userId: number | null
  session: string | null
  visit: number
  intent: number
  targetId: number | null
  text: string
  kbIds: number[]
  baselineId: number
  initialDetail: ConversationDetail | null
  phase: 'creating' | 'asking' | 'success' | 'error'
  response?: AskResponse
  /** 流式问答期间的思考步骤与答案增量；完成后清空，由最终消息接管。 */
  stream?: ThinkingStream
  error?: string
  ambiguous?: boolean
  matchingTurnSeen?: boolean
}
export type ChatJobEvent = {
  kind: 'change' | 'list' | 'settled'
  userId: number | null
  session: string | null
  job?: AskJob
}
export const MAX_CHAT_JOBS = 32
// 模块级单例：提问注册表跨路由卸载保留本页未完成的请求，全应用只有这一份。
export const askJobs = new Map<number, AskJob>()
export const visibleSuccessJobs = new Set<number>()
export const jobListeners = new Set<(event: ChatJobEvent) => void>()
let nextJobKey = 0
let nextVisitKey = 0

/** 取下一个提问键；计数器仍留在本模块，保证自增只有一处。 */
export function newJobKey(): number {
  return ++nextJobKey
}

/** 取下一次页面访问序号。 */
export function newVisitKey(): number {
  return ++nextVisitKey
}

export function publishJob(kind: ChatJobEvent['kind'], job: AskJob) {
  jobListeners.forEach((listener) => listener({ kind, userId: job.userId, session: job.session, job }))
}

export function publishList(userId: number | null, session: string | null) {
  jobListeners.forEach((listener) => listener({ kind: 'list', userId, session }))
}

export function forgetJob(job: AskJob) {
  if (askJobs.get(job.key) !== job) return
  askJobs.delete(job.key)
  publishJob('change', job)
}

export function retainSettledJob(job: AskJob) {
  // Retain a visible successful fallback; clean up abandoned records eventually.
  const expire = () => {
    if (askJobs.get(job.key) !== job) return
    if (job.phase === 'success' && visibleSuccessJobs.has(job.key)) {
      window.setTimeout(expire, 60 * 1000)
    } else if (job.phase === 'success') {
      forgetJob(job)
    }
  }
  window.setTimeout(expire, 5 * 60 * 1000)
}

export function jobMatchesSession(job: AskJob, userId: number | null) {
  return job.userId === userId && (job.session === getToken() || job.phase === 'error')
}

export function serverHasQuestion(messages: ChatMessage[], job: AskJob) {
  return messages.some((message, index) =>
    message.id > job.baselineId &&
    message.role === 'user' &&
    message.content === job.text &&
    Boolean(message.kb_ids?.length) &&
    message.kb_ids?.every((id) => job.kbIds.includes(id)) &&
    messages[index + 1]?.role === 'assistant',
  )
}

export function withJobAnswer(messages: ChatMessage[], job: AskJob): ChatMessage[] {
  const res = job.response
  if (!res) return messages
  const assistant: ChatMessage = {
    ...res.message,
    evidence: res.evidence,
    route: res.route,
    latency_ms: res.usage?.latency_ms ?? null,
    kb_ids: job.kbIds,
    insufficient: res.insufficient,
    usage: res.usage,
  }
  const existingAnswer = messages.findIndex((message) => message.id === assistant.id)
  if (existingAnswer >= 0) {
    const next = [...messages]
    next[existingAnswer] = assistant
    return next
  }
  const userMessage: ChatMessage = {
    id: -job.key,
    role: 'user',
    content: job.text,
    kb_ids: job.kbIds,
    created_at: assistant.created_at ?? new Date().toISOString(),
  }
  return [...messages, userMessage, assistant]
}

function upsertThinkingStep(steps: PipelineStep[], step: PipelineStep): PipelineStep[] {
  const index = steps.findIndex((item) => item.id === step.id)
  if (index < 0) return [...steps, step]
  const next = [...steps]
  next[index] = { ...next[index], ...step }
  return next
}

/** 把一条 SSE 事件合并进当前提问的流式状态。 */
export function applyStreamEvent(job: AskJob, event: AskStreamEvent) {
  const stream = job.stream ?? (job.stream = { steps: [], answer: '', phase: 'thinking' })
  if (event.type === 'step') {
    stream.steps = upsertThinkingStep(stream.steps, {
      id: event.id,
      title: event.title,
      status: event.status,
      detail: event.detail,
    })
    return
  }
  if (event.type === 'answer') {
    stream.answer += event.delta
    stream.phase = 'answering'
    return
  }
  if (event.type === 'answer_replace') {
    stream.answer = event.content
    stream.phase = 'answering'
    return
  }
  if (event.type === 'done') {
    stream.phase = 'done'
  }
}
