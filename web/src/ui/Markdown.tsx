/**
 * 聊天气泡里的 Markdown 渲染。
 *
 * ## 不拼 HTML 字符串
 *
 * 气泡内容来自员工的模型输出,不是完全可信的文本。`remark-gfm` 只把它解析成语法树,
 * `react-markdown` 再把语法树映射成 React 元素——不是 `dangerouslySetInnerHTML`。markdown
 * 里出现的裸 HTML(`<script>`、`<img onerror>`)默认原样当文本显示,不会被当成标签解析执行。
 *
 * ## 代码块复用 LogViewer
 *
 * 围栏代码块与"查证过程"里展开的工具输出该是同一种视觉语言——等宽字体、浅灰底、圆角框。
 * 两处分别实现的话,以后调一次代码块的观感要改两个地方。
 *
 * ## 块 / 行内代码靠 `pre` 是不是父节点判断,不猜内容
 *
 * react-markdown 已经不传 `inline` 属性给 `code` 渲染器了——没写语言的单行围栏代码块和
 * 行内代码 span 因此长得一样,靠正则猜内容(有没有换行、有没有 `language-` 前缀)会把
 * `` ```\nls -la\n``` `` 这种块误判成行内。真正可靠的信号是 CommonMark 本身的结构:
 * 块级代码永远是 `pre > code`,行内 span 永远不是——所以在 `pre` 这一层直接读 hast 节点
 * 里的原文,不经过 `code` 渲染器,`code` 只处理行内那一种情形。
 */
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { LogViewer } from "./LogViewer";

function textOf(node: { value?: string; children?: { value?: string; children?: unknown[] }[] }): string {
  if (typeof node.value === "string") return node.value;
  return (node.children ?? []).map((child) => textOf(child as typeof node)).join("");
}

export function Markdown({ text }: { text: string }) {
  if (!text) return null;
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noreferrer noopener">
              {children}
            </a>
          ),
          pre: ({ node }) => {
            const code = node?.children.find((child) => "tagName" in child && child.tagName === "code");
            const value = code ? textOf(code as never).replace(/\n$/, "") : "";
            return <LogViewer text={value} />;
          },
          code: ({ children }) => <code className="mono ic">{children}</code>,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
