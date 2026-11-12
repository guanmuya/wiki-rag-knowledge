import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import type { ThemeMode } from './types'

/**
 * 外观（跟随系统 / 浅色 / 深色）。
 *
 * 三件事都在这里：
 *  1. **档位读写**：`localStorage` 里存 `system | light | dark`，与既有偏好模式一致
 *     （见 `pages/models/constants.ts` 的折叠状态）。默认 `system`。
 *  2. **解析成实际主题**：`system` 档不是「启动时读一次」，而是监听
 *     `prefers-color-scheme` 的变化持续重算——用户说的「跟随系统」就是这个意思。
 *  3. **落地**：把结果写到 `<html data-theme>`、`color-scheme`、`<meta name="theme-color">`
 *     和 `<html>` 的内联底色上；同时通过 `wrkDesktop.setThemeMode` 告诉 Electron 主进程，
 *     让窗口底色跟着变（否则深色下启动会先闪一下白底）。
 *
 * 首屏防闪不靠这里：React 挂载前 `public/theme-boot.js` 已经用同一套规则写过一遍
 * （同一个 storage 键、同一组底色常量）。改这两处必须成对改。
 */

/** 与 `public/theme-boot.js` 的 `KEY` 保持一致。 */
export const THEME_STORAGE_KEY = 'wrk_appearance'

export type ResolvedTheme = 'light' | 'dark'

/**
 * 深色文档底色：与 `12-theme.css` 里深色 `--c-bg` 一致，用于首帧防闪
 * （样式表还没到时先把文档底刷成深色）。浅色**不**设内联底色：
 * html 保持透明、由 body 的画布绘制，与改动前完全一致。
 */
export const THEME_DARK_DOC_BACKGROUND = '#0f1116'

/**
 * 窗口/浏览器 chrome 的底色：浅色沿用改动前的 `#f5f7fa`
 * （Electron 窗口底与 `index.html` 的 theme-color 原本就是这个值），深色用深底。
 */
export const THEME_CHROME_BACKGROUND: Record<ResolvedTheme, string> = {
  light: '#f5f7fa',
  dark: '#0f1116',
}

const DARK_QUERY = '(prefers-color-scheme: dark)'

/** 三档的界面文案。`hint` 用于按钮的 title，解释选中后会发生什么。 */
export const THEME_OPTIONS: Array<{ value: ThemeMode; label: string; hint: string }> = [
  { value: 'system', label: '跟随系统', hint: '随系统在浅色与深色之间自动切换' },
  { value: 'light', label: '浅色', hint: '始终使用浅色外观' },
  { value: 'dark', label: '深色', hint: '始终使用深色外观' },
]

export function isThemeMode(value: unknown): value is ThemeMode {
  return value === 'system' || value === 'light' || value === 'dark'
}

/** 读档位；读不到（首次使用、隐私模式、值损坏）一律按「跟随系统」。 */
export function readThemeMode(): ThemeMode {
  try {
    const saved = localStorage.getItem(THEME_STORAGE_KEY)
    return isThemeMode(saved) ? saved : 'system'
  } catch {
    return 'system'
  }
}

export function writeThemeMode(mode: ThemeMode): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, mode)
  } catch {
    /* 隐私模式：本次会话仍然生效，只是下次启动回到跟随系统 */
  }
}

/** 系统当前是否深色。没有 matchMedia（老环境/测试替身）时按浅色。 */
export function systemPrefersDark(): boolean {
  try {
    return window.matchMedia(DARK_QUERY).matches
  } catch {
    return false
  }
}

export function resolveTheme(mode: ThemeMode, systemDark: boolean): ResolvedTheme {
  if (mode === 'system') return systemDark ? 'dark' : 'light'
  return mode
}

/**
 * 把解析结果落到文档上：`data-theme` 供 CSS 选择、`color-scheme` 让原生控件
 * （滚动条、`<select>` 弹层、日期选择器）跟着变、`theme-color` 管窗口/地址栏。
 * 幂等，可以反复调用。
 */
export function applyTheme(resolved: ResolvedTheme): void {
  const root = document.documentElement
  root.dataset.theme = resolved
  root.style.colorScheme = resolved
  // 只有深色需要提前刷文档底色（浅色由 body 的画布画，保持改动前的样子）
  root.style.backgroundColor = resolved === 'dark' ? THEME_DARK_DOC_BACKGROUND : ''
  const meta = document.querySelector('meta[name="theme-color"]')
  if (meta) meta.setAttribute('content', THEME_CHROME_BACKGROUND[resolved])
}

/**
 * 告诉 Electron 主进程当前档位：主进程据此换窗口底色，并把档位落盘，
 * 下次启动在创建窗口之前就能读到（深色下不闪白）。浏览器模式没有这个桥。
 * 失败只影响窗口底色，不弹错、不重试。
 */
function syncDesktopTheme(mode: ThemeMode): void {
  window.wrkDesktop?.setThemeMode(mode).catch(() => {
    /* 老版本主进程没有这个通道：忽略即可，界面自己的主题不受影响 */
  })
}

interface ThemeContextValue {
  mode: ThemeMode
  resolved: ResolvedTheme
  setMode: (mode: ThemeMode) => void
}

const ThemeContext = createContext<ThemeContextValue | null>(null)

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<ThemeMode>(readThemeMode)
  const [systemDark, setSystemDark] = useState<boolean>(systemPrefersDark)

  // 跟随系统档要「实时跟随」：系统在浅色/深色之间切换时立刻重算，
  // 而不是只在启动时读一次 prefers-color-scheme。
  useEffect(() => {
    let query: MediaQueryList | null = null
    try {
      query = window.matchMedia(DARK_QUERY)
    } catch {
      return
    }
    const onChange = (event: MediaQueryListEvent) => setSystemDark(event.matches)
    query.addEventListener('change', onChange)
    // 首帧到监听装好之间系统可能已经变过，这里对齐一次
    setSystemDark(query.matches)
    return () => query.removeEventListener('change', onChange)
  }, [])

  const resolved = resolveTheme(mode, systemDark)

  // 主题落地 + 同步桌面窗口底色。挂载时跑一次，之后每次档位或系统主题变化都跑：
  // 系统在浅色/深色之间切换时，主进程也要跟着换窗口底色。
  useEffect(() => {
    applyTheme(resolved)
    syncDesktopTheme(mode)
  }, [mode, resolved])

  const setMode = useCallback((next: ThemeMode) => {
    setModeState(next)
    writeThemeMode(next)
  }, [])

  const value = useMemo<ThemeContextValue>(() => ({ mode, resolved, setMode }), [mode, resolved, setMode])

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext)
  if (!value) throw new Error('useTheme 必须在 ThemeProvider 内使用')
  return value
}
