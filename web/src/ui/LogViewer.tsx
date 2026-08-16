/**
 * 日志与原始文本的呈现。
 *
 * 任务详情的实时日志、基因组页的 diff 原文、对话里展开的工具调用输出——这三处本来各写
 * 一个 `<pre className="log">`,行为一致纯属巧合。收成一处之后,后面要加的虚拟滚动、
 * 折叠、搜索只需要改一个地方。
 *
 * ## 空态给一句话
 *
 * 与 `kit.tsx` 的 `Empty` 同源:白屏没法区分「还没有」与「挂了」。这里默认给
 * 「(还没有日志)」,页面可以覆盖。
 */

export type LogLine = { line: number; text: string };

export function LogViewer({
  lines,
  text,
  empty = "(还没有日志)",
  maxHeight,
}: {
  /** 带行号的日志。与 `text` 二选一。 */
  lines?: LogLine[];
  /** 原始文本(diff、报告)。与 `lines` 二选一。 */
  text?: string;
  empty?: string;
  maxHeight?: number;
}) {
  // 行号与正文之间是两个空格——保持与既有任务日志逐字一致,免得这次抽取顺手改了观感。
  const body = lines ? lines.map((item) => `${item.line}  ${item.text}`).join("\n") : (text ?? "");

  return (
    <pre className="log" style={maxHeight ? { maxHeight } : undefined}>
      {body || empty}
    </pre>
  );
}
