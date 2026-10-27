/**
 * 关于页的静态资料：许可证与第三方致谢。
 *
 * 为什么单独放一个文件：这些清单会随依赖变化，混在 JSX 里改起来容易漏。
 * 只列**运行时真正随包分发**的组件；开发期工具（vite / typescript / electron-builder）
 * 不进安装包，不在此列。
 *
 * 许可证口径：本项目自身为私有部署交付，未声明开源许可；
 * 下列第三方组件各自的许可证在安装目录的 LICENSE 文件中随包分发。
 */

export interface ThirdPartyNotice {
  name: string
  license: string
  /** 用途：让用户知道这东西为什么在包里 */
  purpose: string
}

export const THIRD_PARTY_NOTICES: ThirdPartyNotice[] = [
  { name: 'Electron', license: 'MIT', purpose: '桌面外壳（Chromium + Node 运行时）' },
  { name: 'React / React DOM', license: 'MIT', purpose: '前端界面框架' },
  { name: 'React Router', license: 'MIT', purpose: '前端路由（HashRouter）' },
  { name: 'marked', license: 'MIT', purpose: 'Markdown 渲染' },
  { name: 'DOMPurify', license: 'Apache-2.0 或 MPL-2.0', purpose: '渲染内容 XSS 过滤' },
  { name: 'FastAPI', license: 'MIT', purpose: '后端 HTTP 框架' },
  { name: 'Starlette / Uvicorn', license: 'BSD-3-Clause', purpose: 'ASGI 运行时与服务器' },
  { name: 'SQLAlchemy', license: 'MIT', purpose: 'ORM 与 SQLite 访问' },
  { name: 'Pydantic', license: 'MIT', purpose: '请求/响应数据校验' },
  { name: 'pypdf', license: 'BSD-3-Clause', purpose: 'PDF 文本抽取' },
  { name: 'python-docx', license: 'MIT', purpose: 'Word 文档解析' },
  { name: 'python-pptx', license: 'MIT', purpose: 'PowerPoint 解析' },
  { name: 'openpyxl', license: 'MIT', purpose: 'Excel 解析' },
  { name: 'lxml', license: 'BSD-3-Clause', purpose: 'XML/HTML 解析' },
  { name: 'cryptography', license: 'Apache-2.0 或 BSD-3-Clause', purpose: '供应商密钥加密（Fernet）' },
  { name: 'httpx', license: 'BSD-3-Clause', purpose: '模型供应商 API 调用' },
  { name: 'PyInstaller', license: 'GPL-2.0（含打包例外条款）', purpose: '后端打包为单文件 exe' },
  { name: 'NSIS', license: 'zlib/libpng', purpose: 'Windows 安装包' },
]

/**
 * 界面里展示「本项目自身」的说明。
 * 不写具体公司名 —— 私有部署，交付时按需替换。
 */
export const PROJECT_LICENSE = {
  name: '企业知识库 · 文档整理与智能问答系统',
  summary: '私有部署交付，未声明开源许可。第三方组件遵循各自许可证，详见安装目录 LICENSE 文件。',
}
