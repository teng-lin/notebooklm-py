function escapeHtml(text: string): string { return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\"/g, "&quot;") }
export function marked(text: string): string {
  let html = escapeHtml(text)
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_m, lang, code) => `<pre><code${lang ? ` class="language-${lang}"` : ""}>${escapeHtml(code)}</code></pre>`)
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>")
  html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
  html = html.replace(/\*(.+?)\*/g, "<em>$1</em>")
  html = html.replace(/~~(.+?)~~/g, "<del>$1</del>")
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
  html = html.replace(/^(\s*)- (.+)$/gm, "<li>$2</li>"); html = html.replace(/(<li>.*<\/li>\n?)+/g, "<ul>$&</ul>")
  html = html.replace(/^(\s*)\d+\. (.+)$/gm, "<li>$2</li>")
  html = html.replace(/^### (.+)$/gm, "<h3>$1</h3>"); html = html.replace(/^## (.+)$/gm, "<h2>$1</h2>"); html = html.replace(/^# (.+)$/gm, "<h1>$1</h1>")
  const lines = html.split("\n"); let result = "", inPara = false
  for (const line of lines) {
    const t = line.trim()
    if (!t) { if (inPara) { result += "</p>"; inPara = false }; continue }
    if (t.startsWith("<h") || t.startsWith("<ul") || t.startsWith("</ul") || t.startsWith("<li") || t.startsWith("<pre") || t.startsWith("</pre")) {
      if (inPara) { result += "</p>"; inPara = false }; result += t + "\n"
    } else { if (!inPara) { result += "<p>"; inPara = true } else result += "<br>"; result += t }
  }
  if (inPara) result += "</p>"
  return result
}
