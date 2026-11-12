import { THEME_OPTIONS, useTheme } from '../theme'

/**
 * 外观三档开关：跟随系统 / 浅色 / 深色。
 *
 * 位置：侧栏底部（用户信息上方）。理由写在 `docs/THEME.md`：
 *  - 它是「这台机器上我自己怎么看」的设置，和用户信息、数据目录、退出登录同属一类；
 *  - 侧栏在桌面端常驻，入口不需要新增页面，也不去挤顶栏右侧那排身份信息；
 *  - 复用既有的分段控件口径（`.view-switch` 的同款外形），不新造一套控件。
 *
 * 语义：`aria-pressed` 三选一，键盘 Tab 可达、Enter/Space 可切。
 * 标签写全「跟随系统」而不是图标：这个应用的用户不是开发者，图标表达不了这一档的意思。
 */
export default function ThemeSwitch() {
  const { mode, setMode } = useTheme()

  return (
    <div className="theme-switch" data-theme-switch="" role="group" aria-label="外观">
      <span className="theme-switch-label">外观</span>
      <div className="theme-switch-track">
        {THEME_OPTIONS.map((option) => {
          const active = option.value === mode
          return (
            <button
              key={option.value}
              type="button"
              className={`theme-switch-item ${active ? 'active' : ''}`}
              aria-pressed={active}
              title={option.hint}
              onClick={() => setMode(option.value)}
            >
              {option.label}
            </button>
          )
        })}
      </div>
    </div>
  )
}
