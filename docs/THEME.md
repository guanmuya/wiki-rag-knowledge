# 深色主题与「跟随系统」外观 · 方向说明

> 对应需求原文：「整体的色调应该还可以有一个跟随系统设定和深色」。
> 本文只回答四件事：三档怎么定义、深色怎么落地、开关为什么放在侧栏底部、浅色凭什么说没被改动。

## 一、三档与默认

| 档位 | 界面文案 | 行为 | 存储 |
| --- | --- | --- | --- |
| `system`（默认） | 跟随系统 | 读 `prefers-color-scheme`，并**持续监听**它的变化：系统在浅色/深色之间切换时界面实时重算，不需要刷新、不需要重启 | localStorage `wrk_appearance` 不写值 |
| `light` | 浅色 | 始终浅色，深色系统下也不变 | `wrk_appearance=light` |
| `dark` | 深色 | 始终深色 | `wrk_appearance=dark` |

默认档刻意不写 `localStorage`：没点过就是没点过，用户第一次进来看不出「被设过什么」。
存储键沿用仓库既有偏好模式（`wrk_token`、`wrk_model_groups_collapsed`），没有新增依赖、没有后端字段。

## 二、做法：只覆盖令牌，不改组件

上一轮「前端整体视觉统一」（[`docs/UI_UNIFICATION.md`](UI_UNIFICATION.md)）已经把颜色收敛到
`frontend/src/styles/01-tokens-base.css` 的 `:root` 一张表里：其余 10 个分片与
`frontend/src/pages/UsagePage.css` 一律引用 `var(--c-*)`，硬编码色值只剩少量 `rgba()` 阴影与半透明白。
**因此深色不是第二套样式，而是同一批变量名换一组取值。**

改动面（`frontend/src/styles.css` 只多一行 `@import`）：

| 文件 | 改动 |
| --- | --- |
| `frontend/src/styles/12-theme.css`（新增，第 12 片） | `color-scheme` 口径 + 深色令牌表 + 定点覆盖 + 三档开关样式 |
| `frontend/src/styles.css` | 追加 `@import './styles/12-theme.css';`（排在最后，深色覆盖天然生效，不必 `!important`） |
| `frontend/src/styles/01-tokens-base.css` | 只加两行注释指向新分片，**令牌取值一个都没改** |
| `frontend/src/styles/03-controls.css` | **未改**：该文件由并行的控件任务（#39）定稿，其中 `.select-wrap::after` 已经用 `background-color: var(--c-text-muted)` 画下拉箭头，深色下换令牌自动跟随，本任务不需要、也不允许再补箭头 |

颜色层次用「亮一点」表达，与浅色下「白 < 淡蓝灰」的方向一致，只是明暗反过来：
画布 `#0f1116` < 卡片 `#171a21` < 次级面 `#1b202a` < 凹槽 `#1f242e`。

正文 / 次要 / 弱化三档文字在卡片底上的对比度：

| 档 | 深色取值 | 深色对比度 | 浅色对照（`#142338` / `#40516a` / `#5b6c82`） |
| --- | --- | --- | --- |
| 正文 | `#e8edf6` | 14.9 : 1 | 15.8 : 1 |
| 次要 | `#b6c0d2` | 9.5 : 1 | 8.1 : 1 |
| 弱化 | `#8b97ab` | 5.9 : 1 | 5.4 : 1 |

同一套节奏，深色下没有一档掉到 4.5:1 以下（弱化色用在字段说明、页脚这类非关键文字上）。

CSS 里刻意**不写** `@media (prefers-color-scheme: dark)` 兜底：`:root` 上的 `data-theme` 一定会被写上去
（首屏脚本 + React 各写一次），多一份媒体查询只会多一份要同步维护的取值表。

## 三、令牌表达不了的十几处，逐个定点覆盖

深色不是把白色换成黑色就完事。下面这些必须单独说明，也是这次改动的全部「例外」：

| 位置 | 浅色 | 深色 | 为什么 |
| --- | --- | --- | --- |
| `.topbar` | `rgba(255,255,255,.82)` | `rgba(15,17,22,.86)` | 顶栏是半透明毛玻璃，令牌管不到；深色下必须换成深底半透明，否则一片白雾 |
| 分段控件选中块 | 比凹槽「更白」 | `--c-surface-raised`（比凹槽更亮） | 深色下「更白」等于「更暗」，看起来像没选中，方向要反过来 |
| 主按钮 / 危险按钮 / 品牌块 | 实色底 + 白字 | 另给 `--c-primary-solid #3f6fd8`、`--c-danger-solid #c33f3f` | 主色在深色下当**文字**要够亮（`#6ea3ff`），当**实色底**就不够深了：白字压上去只有 2.4:1。分开成「文字用亮档、实色底用 solid 档」，两边都过 4.5:1（实测 4.7:1 / 5.1:1） |
| 输入框 / 下拉 / 多行框 | 与卡片同色（都白） | 抬高一档 `#1b202a` | 深色下同色会糊成一块，「这里能输入」看不出来 |
| 遮罩、`::selection`、悬停白、证据引用、`kb-pick .lv`、spinner 环、品牌投影、Toast 描边、单价面板 `brightness()` | 各种 `rgba(255,255,255,…)` 与 `brightness(.97)` | 换成低透明白 / 更黑的投影 / `brightness(1.18)` | 都是「在白底上再提亮」的写法，深色下要么刺眼要么完全没反应 |

**下拉框箭头不在上表里**，这是刻意留下的：控件任务（#39）把箭头从「写死颜色的 data-URI」改成了
`<span class="select-wrap">` + `.select-wrap::after`，形状用 `mask`、颜色用
`background-color: var(--c-text-muted)`。也就是说箭头颜色**本来就是令牌驱动**的，深色下自动变成
`#8b97ab`，主题分片里不需要任何覆盖。旧实现在 `12-theme.css` 里为 `.select` 补过一条
`background-image`，在 #39 之后会与 `.select-wrap::after` 叠成**两个箭头**（而且因为
`background-repeat: no-repeat` 已随 #39 删除，那条 `background-image` 会平铺成一整排）——
这一条已经删掉，并在 `tests/theme.test.mjs` 里用两条断言钉住：`<select>` 自身
`background-image` 必须是 `none`、`.select-wrap::after` 的颜色必须等于当前主题的 `--c-text-muted`。

另外 `color-scheme` 单独给了一档：`:root` 显式 `light`，深色下 `dark`。
它管的是**原生控件**——滚动条、`<select>` 弹层、复选框、日期选择器，这些由浏览器按 `color-scheme` 绘制，
只给 `body` 上色是调不出来的。浅色下也显式写 `light`：否则深色系统上会出现「白页面配深色下拉弹层」。

## 四、开关放在侧栏底部（需求留给我们定的那一处）

需求把入口位置留给我们：「侧栏底部（`sidebar-foot`）还是顶栏右侧（`topbar-right`），不单独开页面」。
**选侧栏底部，放在用户信息上方。**

1. 它是「这台机器上我自己怎么看」的设置，和用户信息、数据目录、退出登录同属一类：
   都是本地、只影响自己、不需要权限的东西。顶栏右侧那排是身份与角色信息，语义不同。
2. 侧栏在桌面端常驻，入口在每一个业务页都在，不需要新增页面，也不占正文的宽度。
3. 侧栏宽度够放三个中文词（跟随系统 / 浅色 / 深色），顶栏右侧要塞进去只能缩成图标或
   下拉菜单——图标表达不了「跟随系统」这一档，下拉菜单又多一次点击才能看到当前档位。

形态是文字三档的分段控件（与 `.view-switch` 同一套视觉语言：凹槽 + 圆角小块 + 选中块浮起），
`aria-pressed` 标注当前档位，Tab 可达、Enter/Space 可切。

登录页 / 初始化页**没有**这个开关：那两页还没有「用户」这个概念，但它们照样跟随已保存的档位
（深色用户打开登录页不会突然白一下）。

## 五、首屏不闪白：一个外部脚本 + 窗口底色

深色最容易被忽略的一处是**第一帧**，三处都要管住：

1. **文档第一帧**：`frontend/public/theme-boot.js`，由 `index.html` 在样式表与入口脚本**之前**同步加载。
   CSP 是 `script-src 'self'`（不允许内联脚本），所以只能是一个外部小文件；它只做
   「读本地档位 → 解析成浅色/深色 → 写 `data-theme` / `color-scheme` / 深色文档底色」，
   规则与 `src/theme.tsx` 完全一致（同一个 storage 键、同一组底色常量，测试会比对这两处）。
   构建产物里这行被 vite 按 `base: './'` 改写成 `<script src="./theme-boot.js">`，
   打包版用 `file://` 打开也能命中。
2. **窗口第一帧**：`frontend/electron/main.cjs` 在 `new BrowserWindow` 之前，
   用 `electron/theme-prefs.cjs` 读上次落盘的档位、按 `nativeTheme.shouldUseDarkColors` 算窗口底色，
   浅色仍是改动前的 `#f5f7fa`，深色是 `#0f1116`。窗口底色晚一步就是深色下先闪一帧白。
3. **原生控件**：`nativeTheme.themeSource = 档位`，好处是「浅色档」在深色系统下也不会让原生菜单/弹窗变深。

渲染进程切换档位时调新增的 `wrkDesktop.setThemeMode(mode)`（preload → `wrk:theme-set`），
主进程换窗口底色并把档位落盘到 `userData/appearance.json`；「跟随系统」档下系统自己换了深浅，
主进程另有 `nativeTheme.on('updated')` 兜一手窗口底色。

数据契约同步更新：`frontend/src/types.ts` 的 `DesktopBridge.setThemeMode`，
`pnpm check:contract` 依旧比对 `preload.cjs` 的暴露面（通过）。

## 六、边界：没做什么

- **没改浅色**。浅色令牌取值一个都没动；浅色下唯一可见的变化就是侧栏底部多了这个开关
  （需求要求的入口，无法避免），其余靠下面的样式快照比对证明。
- **没改业务逻辑**：未动 `frontend/src` 的接口调用、路由、状态机，未动 `backend/**`。
- **没动 `03-controls.css`**：该文件由并行的控件任务（#39）定稿，下拉箭头已由
  `.select-wrap::after` 用令牌上色，本分片不碰、也不补任何箭头覆盖（见 §三 末段）。
- **没出安装包、没打 tag、没发版**：本分支只到「代码 + 文档 + 测试」。

## 七、验证结论（本机实跑，均在合入 #39 之后的 `master` 上复跑）

| 检查 | 结果 |
| --- | --- |
| `python -m pytest backend/tests -q` | 784 passed, 1 skipped（与改动前基线一致，未动后端） |
| `pnpm check:contract` | 通过（`setThemeMode` 已进 `DesktopBridge` 与 `preload.cjs` 两侧） |
| `pnpm typecheck` | 通过 |
| `pnpm build:web` | 通过；`dist/theme-boot.js` 存在，`dist/index.html` 里是 `<script src="./theme-boot.js">` 且在入口脚本之前 |
| `pnpm test` | 通过，**38 / 38**（六个测试文件；`tests/theme.test.mjs` 12 条、`tests/ui-controls.test.mjs` 4 条都在这个脚本里） |
| 浅色「没被改动」 | 合并前后各拍 15 个快照（登录页 + 12 条路由 + 弹窗 + 样式探针），按「元素类 × 样式取值」分组比对 `color / backgroundColor / borderColor / boxShadow / fill / stroke / backgroundImage`：**1215 组里只有 6 处不同，且全部来自 #39 的控件改造**——5 处 `.select` 不再自带 data-URI 箭头（箭头改由 `.select-wrap::after` 画）、模型弹窗里两个复选框从裸 `input` 变成 `input.checkbox`。主题分片自身引入的浅色差异为 0 |
| 深色下拉箭头（本轮返工点） | 深色下逐个数出来：`.select` 自身 `background-image` 全是 `none`（不会再叠第二个箭头/平铺），箭头只有 `.select-wrap::after` 那一个，颜色 `rgb(139, 151, 171)` = 深色 `--c-text-muted`，mask 12×12。可用态（任务中心两个筛选）与禁用态（问答页模型下拉，离线时 disabled）各留一张放大图：`docs/screenshots/theme/16-select-enabled.png`、`17-select-disabled.png` |
| 深色人工核对 | 15 个快照 + 组件样式探针，0 个 console / page 错误；覆盖 工作台 / 知识库 / 知识库详情（文档·Wiki·设置）/ 问答 / 个人 Agent / 检索调试 / 任务中心 / 用户管理 / 模型设置 / 用量概览 / 关于 / 登录 / 弹窗 / Toast / 表格 / 状态徽标 / 骨架屏 / Markdown / 代码块。合并后重拍一遍，除下拉箭头本身外无变化 |
| 打包版首帧 | 把 `dist/` 用 `file://` 直接打开：`theme-boot.js` 正常加载、`data-theme` 与 `color-scheme` 按系统偏好落地、样式表加载成功（除 `/api/*` 无后端，预期内） |
| 跟随系统实时性 | 浏览器把系统偏好从浅切深、再切回浅，界面不刷新即重算（`theme.test.mjs` 第 9 条） |

深色界面图见 [`docs/screenshots/theme/`](screenshots/theme/)：登录页、知识库列表、智能问答（含证据）、
用量概览各一张；要整套 15 张，按 [`docs/screenshots/README.md`](screenshots/README.md) §1 的命令加
`$env:UI_THEME='dark'` 重拍即可（输出自动落到 `docs/screenshots/theme/`）。
