import { useEffect, useRef, useState } from 'react'
import type { Dispatch, MutableRefObject, SetStateAction } from 'react'
import { getToken } from '../../api'
import { askJobs, jobListeners, jobMatchesSession, visibleSuccessJobs } from './jobs'
import type { ChatJobEvent } from './jobs'
import type { ChatView } from './useChatView'

/**
 * 订阅模块级任务注册表，并派生出本页要渲染的提问：
 * 进行中的提问、待处理的失败提问、已成功但还没并入会话的提问。
 */
export function useAskJobs({
  userId,
  visit,
  activeId,
  isCurrentView,
  viewRef,
  loadConversations,
  loadDetail,
  detailReady,
  currentDetailError,
  reconciling,
  restoredDrafts,
  recoveredJobKey,
  setQuestion,
}: {
  userId: number | null
  visit: number
  activeId: number | null
  isCurrentView: (expectedVisit: number, expectedId?: number | null) => boolean
  viewRef: MutableRefObject<ChatView | null>
  loadConversations: () => Promise<void>
  loadDetail: (id: number, silent?: boolean, force?: boolean, blocking?: boolean) => Promise<void>
  detailReady: boolean
  currentDetailError: unknown
  reconciling: boolean
  restoredDrafts: MutableRefObject<Set<number>>
  recoveredJobKey: MutableRefObject<number | null>
  setQuestion: Dispatch<SetStateAction<string>>
}) {
  const [, setJobVersion] = useState(0)
  const restoredErrors = useRef(new Set<number>())

  useEffect(() => {
    const listener = ({ kind, job, userId: eventUser, session: eventSession }: ChatJobEvent) => {
      if (eventUser !== userId || (eventSession !== getToken() && job?.phase !== 'error')) return
      if (job && !askJobs.has(job.key)) {
        restoredErrors.current.delete(job.key)
        restoredDrafts.current.delete(job.key)
        if (recoveredJobKey.current === job.key) recoveredJobKey.current = null
      }
      setJobVersion((version) => version + 1)
      if (kind === 'list' || kind === 'settled') void loadConversations()
      const view = viewRef.current
      if (kind === 'settled' && job?.targetId && view?.id === job.targetId &&
          isCurrentView(view.visit, job.targetId)) {
        void loadDetail(job.targetId, true, true, job.phase === 'error')
      }
    }
    jobListeners.add(listener)
    return () => { jobListeners.delete(listener) }
  }, [userId, isCurrentView, loadConversations, loadDetail])

  const currentJobs = [...askJobs.values()].filter((job) => jobMatchesSession(job, userId) &&
    (activeId ? job.targetId === activeId :
      job.phase === 'error' || (job.targetId === null && job.visit === visit && job.intent === viewRef.current?.intent)))
  const activeJob = currentJobs.find((job) => job.phase === 'creating' || job.phase === 'asking')
  const errorJobs = currentJobs.filter((job) => job.phase === 'error')
  const errorJob = activeJob ? undefined : [...errorJobs].reverse().find((job) =>
    activeId ? job.targetId === activeId : job.targetId === null)
  const successfulJobs = currentJobs.filter((job) => job.phase === 'success')
  const userJobCount = [...askJobs.values()].filter((job) => jobMatchesSession(job, userId)).length
  const successfulJobKeys = successfulJobs.map((job) => job.key).join(',')

  useEffect(() => {
    for (const job of successfulJobs) visibleSuccessJobs.add(job.key)
    return () => { for (const job of successfulJobs) visibleSuccessJobs.delete(job.key) }
  }, [visit, successfulJobKeys])

  useEffect(() => {
    if (!errorJob || errorJob.matchingTurnSeen || restoredErrors.current.has(errorJob.key) ||
        !isCurrentView(visit, activeId) || reconciling ||
        (activeId && !detailReady && !currentDetailError)) return
    restoredErrors.current.add(errorJob.key)
    setQuestion((previous) => {
      if (previous || !isCurrentView(visit, activeId)) return previous
      restoredDrafts.current.add(errorJob.key)
      recoveredJobKey.current = errorJob.key
      return errorJob.text
    })
  }, [errorJob, activeId, detailReady, currentDetailError, reconciling, isCurrentView, visit])

  return { activeJob, errorJobs, errorJob, successfulJobs, userJobCount, setJobVersion }
}
