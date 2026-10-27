import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { useMemo } from 'react'

marked.setOptions({
  gfm: true,
  breaks: true,
})

/** 渲染 Markdown（marked → DOMPurify 清洗）。 */
export function renderMarkdown(source: string): string {
  const raw = marked.parse(source ?? '', { async: false }) as string
  return DOMPurify.sanitize(raw, {
    USE_PROFILES: { html: true },
    ADD_ATTR: ['target', 'rel'],
  })
}

export function Markdown({ content, className }: { content: string; className?: string }) {
  const html = useMemo(() => renderMarkdown(content), [content])
  return (
    <div
      className={className ? `markdown ${className}` : 'markdown'}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}
