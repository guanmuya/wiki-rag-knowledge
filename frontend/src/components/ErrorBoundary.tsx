import { Component, type ErrorInfo, type ReactNode } from 'react'

/**
 * 渲染期异常兜底（体检清单 P1-10）。
 *
 * 原始问题：`App.tsx` / `main.tsx` 都没有 ErrorBoundary，任意页面在渲染时抛错
 * （例如接口返回了结构不对的数据）会让 React 卸载整棵树，用户看到的是纯白窗口，
 * 既没有提示也没有自救入口。
 *
 * 现在的行为：捕获子树里的渲染期异常，就地显示一张可读的兜底页，提供
 * 「重试」「回到首页」「重新加载应用」三个出口，并把技术细节折叠起来备查。
 *
 * 注意 React 的边界：它只接渲染期异常，不接事件处理器、异步回调、SSE 里的错误——
 * 那些仍由各页面的 `ErrorState` / `ApiError` 处理，两者互补，不是替代关系。
 */

interface Props {
  children: ReactNode
  /** 兜底页标题，默认「界面出了点问题」 */
  title?: string
  /** 兜底页说明，默认给出下一步可做什么 */
  description?: string
  /**
   * 整窗兜底（挂在应用最外层时用）：容器占满视口高度并居中。
   * 页面级兜底不要传：那种情况侧边栏还在，占满视口会多出一条无谓的滚动条。
   */
  fullscreen?: boolean
}

interface State {
  error: Error | null
  detailOpen: boolean
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, detailOpen: false }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 控制台是排障第一现场：把组件栈一并留下，日志里就能看出是哪个页面炸的。
    console.error('[ErrorBoundary] 页面渲染异常：', error, info.componentStack)
  }

  private reset = (): void => {
    this.setState({ error: null, detailOpen: false })
  }

  /**
   * 回到首页 = 改 hash 触发路由变化 + 清掉错误状态。
   * 为什么两个都要：已经在首页时改 hash 不会触发导航，只能靠清状态重渲染。
   */
  private goHome = (): void => {
    window.location.hash = '#/kbs'
    this.reset()
  }

  private reload = (): void => {
    window.location.reload()
  }

  render(): ReactNode {
    const { error } = this.state
    if (!error) return this.props.children

    const title = this.props.title ?? '界面出了点问题'
    const description =
      this.props.description ??
      '这个页面在渲染时遇到了意外错误，没有影响已保存的数据。可以先点「重试」；不行就回到首页换个页面，或在桌面版重载应用。'

    return (
      <div className={this.props.fullscreen ? 'boot-screen' : 'error-boundary'}>
        <div className="backend-down" role="alert">
          <div className="backend-down-icon">!</div>
          <h1>{title}</h1>
          <p className="backend-down-msg">{description}</p>

          <div className="backend-down-actions">
            <button type="button" className="btn btn-primary" onClick={this.reset}>
              重试
            </button>
            <button type="button" className="btn" onClick={this.goHome}>
              回到首页
            </button>
            <button type="button" className="btn" onClick={this.reload}>
              重新加载应用
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => this.setState((prev) => ({ detailOpen: !prev.detailOpen }))}
            >
              {this.state.detailOpen ? '收起详情' : '查看详情'}
            </button>
          </div>

          {this.state.detailOpen ? (
            <div className="backend-down-logs">
              <dl className="backend-down-meta">
                <dt>错误信息</dt>
                <dd className="mono">{error.message || String(error)}</dd>
              </dl>
              <pre className="context-preview" style={{ maxHeight: 240 }}>
                {error.stack || '（没有堆栈信息）'}
              </pre>
              <div className="tiny muted">
                把这段内容连同桌面版日志一起发给技术支持，可以定位到具体页面。
              </div>
            </div>
          ) : null}
        </div>
      </div>
    )
  }
}
