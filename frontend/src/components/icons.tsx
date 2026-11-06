import type { ReactNode } from 'react'

/**
 * 统一线性图标集（unslop 图标 pass）。
 *
 * 为什么集中一处：此前界面里混用了 ⚠ ⏳ ⚡ ◌ ✦ ▸▾ ✓✕ ●◐○ × ☰ ← → 这些字符图标，
 * 字重和基线随字体变化，同一行里粗细不一；导航已经是内联 SVG，其余位置却还是字符，
 * 两套图标放在一起就显得廉价。这里统一成 20×20 viewBox、1.5 描边、16px 的线性图标，
 * 颜色一律跟随 currentColor，由所在文本角色决定。
 */
export type IconProps = {
  size?: number
  className?: string
  /** 给独立使用、没有文字标签的图标补可读名称；纯装饰图标不要传 */
  title?: string
}

function Svg({
  size = 16,
  className,
  title,
  children,
}: IconProps & { children: ReactNode }) {
  return (
    <svg
      viewBox="0 0 20 20"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className ? `icon ${className}` : 'icon'}
      aria-hidden={title ? undefined : true}
      role={title ? 'img' : undefined}
    >
      {title ? <title>{title}</title> : null}
      {children}
    </svg>
  )
}

export function IconAlert(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M10 3.4 18 16.6H2L10 3.4Z" />
      <path d="M10 8.2v4.1M10 14.5v.2" />
    </Svg>
  )
}

export function IconClock(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="10" cy="10" r="7" />
      <path d="M10 6.2V10l2.6 1.7" />
    </Svg>
  )
}

export function IconBolt(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M11.2 2.6 4.8 11h4.1l-1 6.4L15.2 9h-4.1l.1-6.4Z" />
    </Svg>
  )
}

export function IconInbox(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M3 12.4 5.6 4h8.8L17 12.4V16H3v-3.6Z" />
      <path d="M3 12.4h4l1.2 2h3.6l1.2-2h4" />
    </Svg>
  )
}

export function IconMessage(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h10A2.5 2.5 0 0 1 19 5.5v7a2.5 2.5 0 0 1-2.5 2.5H9l-4.2 3.2A.7.7 0 0 1 3.7 17V5.5Z" />
    </Svg>
  )
}

export function IconChevronRight(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="m7.6 4.8 5.2 5.2-5.2 5.2" />
    </Svg>
  )
}

export function IconChevronDown(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="m4.8 7.6 5.2 5.2 5.2-5.2" />
    </Svg>
  )
}

export function IconCheck(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="m4 10.5 4 4 8-9" />
    </Svg>
  )
}

export function IconClose(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="m5 5 10 10M15 5 5 15" />
    </Svg>
  )
}

export function IconMenu(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M3.5 6h13M3.5 10h13M3.5 14h13" />
    </Svg>
  )
}

export function IconArrowLeft(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M16 10H4m5-5-5 5 5 5" />
    </Svg>
  )
}

export function IconArrowRight(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M4 10h12m-5-5 5 5-5 5" />
    </Svg>
  )
}

export function IconPlus(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M10 4v12M4 10h12" />
    </Svg>
  )
}

export function IconInfo(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="10" cy="10" r="7" />
      <path d="M10 9v4.2M10 6.6v.2" />
    </Svg>
  )
}

export function IconLock(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M5.5 9V7.4a4.5 4.5 0 0 1 9 0V9" />
      <path d="M4 9h12v8H4V9Z" />
    </Svg>
  )
}

export function IconAgent(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="4" y="6" width="12" height="9" rx="2.5" />
      <path d="M10 3v3M7.2 10v1.2M12.8 10v1.2M7.5 15v2M12.5 15v2" />
    </Svg>
  )
}

export function IconPlug(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M7 3v4M13 3v4" />
      <path d="M5 7h10v2.5A5 5 0 0 1 10 14.5 5 5 0 0 1 5 9.5V7Z" />
      <path d="M10 14.5V17" />
    </Svg>
  )
}

export function IconStop(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="5.5" y="5.5" width="9" height="9" rx="1.5" />
    </Svg>
  )
}

export function IconRefresh(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M16.2 8.4A6.4 6.4 0 1 0 16 12" />
      <path d="M16.4 4.4v4h-4" />
    </Svg>
  )
}

export function IconTerminal(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="2.8" y="4" width="14.4" height="12" rx="2" />
      <path d="m6 8 2.4 2L6 12M10.4 12h3.6" />
    </Svg>
  )
}
