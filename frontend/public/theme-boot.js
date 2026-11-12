/* 首屏防闪：在样式表与 React 之前，先把外观落到 <html> 上。
 *
 * 为什么要一个独立的脚本文件：CSP 是 `script-src 'self'`（见 index.html），
 * 不允许内联脚本；而 ES module（main.tsx）要等样式表之后才执行，
 * 深色用户会先看到一帧白底。这个文件是普通阻塞脚本，解析到就执行。
 *
 * 它只做「读本地档位 → 解析成浅色/深色 → 写 data-theme / color-scheme / 底色」，
 * 规则与 `src/theme.tsx` 完全一致：同一个 storage 键、同一组底色常量。
 * 改其中任何一处都要成对改（深色 `#0f1116` 与 `12-theme.css` 里深色 `--c-bg` 一致）。
 */
(function bootTheme() {
  var KEY = 'wrk_appearance'
  /* 深色文档底色（与 12-theme.css 的深色 --c-bg 一致）与窗口/地址栏底色。
     浅色不设内联底色：html 保持透明，由 body 的画布绘制，与改动前完全一致。 */
  var DARK_DOC_BACKGROUND = '#0f1116'
  var CHROME_BACKGROUND = { light: '#f5f7fa', dark: '#0f1116' }

  var mode = 'system'
  try {
    var saved = localStorage.getItem(KEY)
    if (saved === 'light' || saved === 'dark' || saved === 'system') mode = saved
  } catch (error) {
    /* 隐私模式读不到 localStorage：按跟随系统处理 */
  }

  var theme = mode
  if (theme === 'system') {
    var dark = false
    try {
      dark = window.matchMedia('(prefers-color-scheme: dark)').matches
    } catch (error) {
      /* 没有 matchMedia 就按浅色 */
    }
    theme = dark ? 'dark' : 'light'
  }

  var root = document.documentElement
  root.setAttribute('data-theme', theme)
  root.style.colorScheme = theme
  if (theme === 'dark') root.style.backgroundColor = DARK_DOC_BACKGROUND

  var meta = document.querySelector('meta[name="theme-color"]')
  if (meta) meta.setAttribute('content', CHROME_BACKGROUND[theme])
})()
