import {
  useEffect,
  useRef,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react'
import { errorText } from '../utils'

export function Spinner({ size = 16 }: { size?: number }) {
  return (
    <span
      className="spinner"
      style={{ width: size, height: size }}
      role="img"
      aria-label="加载中"
    />
  )
}

export function LoadingBlock({ text = '加载中…' }: { text?: string }) {
  return (
    <div className="state-block">
      <Spinner size={20} />
      <span>{text}</span>
    </div>
  )
}

export function SkeletonLines({ rows = 3 }: { rows?: number }) {
  return (
    <div className="skeleton-block" aria-label="加载中">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton-line" style={{ width: `${92 - i * 12}%` }} />
      ))}
    </div>
  )
}

export function EmptyState({
  title,
  hint,
  action,
}: {
  title: string
  hint?: string
  action?: ReactNode
}) {
  return (
    <div className="state-block empty">
      <div className="state-icon">◌</div>
      <div className="state-title">{title}</div>
      {hint ? <div className="state-hint">{hint}</div> : null}
      {action ? <div className="state-action">{action}</div> : null}
    </div>
  )
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  return (
    <div className="state-block error">
      <div className="state-icon">!</div>
      <div className="state-title">{errorText(error)}</div>
      {onRetry ? (
        <div className="state-action">
          <button type="button" className="btn btn-ghost btn-sm" onClick={onRetry}>
            重试
          </button>
        </div>
      ) : null}
    </div>
  )
}

export function Badge({
  children,
  tone = 'muted',
}: {
  children: ReactNode
  tone?: 'success' | 'warn' | 'danger' | 'partial' | 'muted' | 'info'
}) {
  return <span className={`badge badge-${tone}`}>{children}</span>
}

export function ProgressBar({ value, tone }: { value: number; tone?: 'danger' | 'success' }) {
  const clamped = Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0))
  return (
    <div className={`progress ${tone ? `progress-${tone}` : ''}`}>
      <div className="progress-fill" style={{ width: `${clamped}%` }} />
      <span className="progress-text">{clamped}%</span>
    </div>
  )
}

export function Modal({
  open,
  title,
  onClose,
  children,
  footer,
  width = 520,
  dismissable = true,
}: {
  open: boolean
  title: string
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
  width?: number
  dismissable?: boolean
}) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && dismissable) onClose()
    }
    window.addEventListener('keydown', onKey)
    ref.current?.focus()
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose, dismissable])

  if (!open) return null
  return (
    <div
      className="modal-mask"
      onMouseDown={(e) => {
        if (dismissable && e.target === e.currentTarget) onClose()
      }}
    >
      <div
        className="modal"
        style={{ width }}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        ref={ref}
      >
        <header className="modal-head">
          <h3>{title}</h3>
          {dismissable ? (
            <button type="button" className="icon-btn" onClick={onClose} aria-label="关闭">
              ×
            </button>
          ) : null}
        </header>
        <div className="modal-body">{children}</div>
        {footer ? <footer className="modal-foot">{footer}</footer> : null}
      </div>
    </div>
  )
}

export function ConfirmDialog({
  open,
  title,
  message,
  confirmText = '确认',
  danger = false,
  busy = false,
  onConfirm,
  onCancel,
}: {
  open: boolean
  title: string
  message: ReactNode
  confirmText?: string
  danger?: boolean
  busy?: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <Modal
      open={open}
      title={title}
      onClose={onCancel}
      width={460}
      footer={
        <>
          <button type="button" className="btn btn-ghost" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button
            type="button"
            className={danger ? 'btn btn-danger' : 'btn btn-primary'}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? <Spinner size={14} /> : null}
            {confirmText}
          </button>
        </>
      }
    >
      <div className="confirm-text">{message}</div>
    </Modal>
  )
}

export function Button({
  variant = 'default',
  size,
  loading,
  children,
  className,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'default' | 'primary' | 'ghost' | 'danger'
  size?: 'sm' | 'md'
  loading?: boolean
}) {
  const cls = ['btn', `btn-${variant}`, size === 'sm' ? 'btn-sm' : '', className ?? '']
    .filter(Boolean)
    .join(' ')
  return (
    <button type="button" className={cls} disabled={rest.disabled || loading} {...rest}>
      {loading ? <Spinner size={14} /> : null}
      {children}
    </button>
  )
}

export function Field({
  label,
  hint,
  error,
  children,
  required,
}: {
  label: string
  hint?: string
  error?: string
  children: ReactNode
  required?: boolean
}) {
  return (
    <label className="field">
      <span className="field-label">
        {label}
        {required ? <span className="req">*</span> : null}
      </span>
      {children}
      {error ? <span className="field-error">{error}</span> : null}
      {!error && hint ? <span className="field-hint">{hint}</span> : null}
    </label>
  )
}

export function TextInput(props: InputHTMLAttributes<HTMLInputElement>) {
  const { className, ...rest } = props
  return <input className={`input ${className ?? ''}`} {...rest} />
}

export function TextArea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  const { className, ...rest } = props
  return <textarea className={`input textarea ${className ?? ''}`} {...rest} />
}

export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  const { className, children, ...rest } = props
  return (
    <select className={`input select ${className ?? ''}`} {...rest}>
      {children}
    </select>
  )
}

export function Tabs<T extends string>({
  value,
  items,
  onChange,
}: {
  value: T
  items: Array<{ key: T; label: string; badge?: ReactNode }>
  onChange: (key: T) => void
}) {
  return (
    <div className="tabs" role="tablist">
      {items.map((it) => (
        <button
          key={it.key}
          type="button"
          role="tab"
          aria-selected={value === it.key}
          className={`tab ${value === it.key ? 'active' : ''}`}
          onClick={() => onChange(it.key)}
        >
          {it.label}
          {it.badge !== undefined ? <span className="tab-badge">{it.badge}</span> : null}
        </button>
      ))}
    </div>
  )
}

export function StatCell({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="stat-cell">
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
    </div>
  )
}
