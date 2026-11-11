import { useCallback, useLayoutEffect, useRef } from 'react'
import { getToken } from '../../api'
import { newVisitKey } from './jobs'

/** 一次「页面访问」的标识：路由、登录身份和会话 id 任一变化都算新访问。 */
export type ChatView = {
  locationKey: string
  pathname: string
  visit: number
  intent: number
  id: number | null
  userId: number | null
  session: string | null
}

/**
 * 本页的「当前视图」判断：路由访问序号 + 登录态 + 挂载状态。
 * 所有请求回调都靠 isCurrentView 丢弃过期响应。
 */
export function useChatView({
  locationKey,
  pathname,
  id,
  userId,
  session,
}: {
  locationKey: string
  pathname: string
  id: number | null
  userId: number | null
  session: string | null
}) {
  const viewRef = useRef<ChatView | null>(null)
  if (!viewRef.current || viewRef.current.locationKey !== locationKey ||
      viewRef.current.pathname !== pathname || viewRef.current.userId !== userId ||
      viewRef.current.session !== session) {
    viewRef.current = {
      locationKey, pathname, visit: newVisitKey(),
      intent: 0, id, userId, session,
    }
  }
  const visit = viewRef.current.visit
  const mountedRef = useRef(false)
  const detailSequence = useRef(0)
  const detailInFlight = useRef<{ sequence: number; visit: number } | null>(null)
  const hydratedVisit = useRef<number | null>(null)
  const listSequence = useRef(0)
  const kbSequence = useRef(0)
  useLayoutEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      detailSequence.current += 1
      listSequence.current += 1
      kbSequence.current += 1
    }
  }, [])

  const isCurrentView = useCallback((expectedVisit: number, expectedId?: number | null) => {
    const view = viewRef.current
    const hashPath = window.location.hash.startsWith('#/')
      ? window.location.hash.slice(1).split('?')[0]
      : window.location.pathname
    return Boolean(mountedRef.current && view && view.visit === expectedVisit &&
      (expectedId === undefined || view.id === expectedId) &&
      view.pathname === hashPath && view.session === getToken())
  }, [])

  return {
    viewRef,
    mountedRef,
    detailSequence,
    detailInFlight,
    hydratedVisit,
    listSequence,
    kbSequence,
    visit,
    isCurrentView,
  }
}
