/**
 * 变更的呈现。
 *
 * 抽出来的理由和 `kit.tsx` 一样:**同一份 diff 在哪儿看都得长得一样**。审批页看到的、
 * 任务详情看到的、对话里员工甩过来的,如果各写一份实现,三个月后它们会在增删配色、
 * 行号对齐、折叠阈值上各自漂移,而用户会以为那是三种不同的东西。
 *
 * ## 它不取数
 *
 * 行从 props 进来。取数留在页面里——组件一旦自己调 api,页面测试就只能连着网络行为一起
 * mock,而 ADR-0001 特意把 mock 边界放在 `api/client` 上就是为了避免这件事。
 */
import type { ReactNode } from "react";

/** 一行。`kind` 缺省表示上下文行(不着色)。 */
export type DiffLine = { text: string; kind?: "add" | "del"; no?: number };

/** 把一段 unified diff 文本切成行。`+++`/`---` 文件头不算增删行。 */
export function parseDiff(text: string): DiffLine[] {
  return text.split("\n").map((line) => {
    if (line.startsWith("+") && !line.startsWith("+++")) return { text: line, kind: "add" as const };
    if (line.startsWith("-") && !line.startsWith("---")) return { text: line, kind: "del" as const };
    return { text: line };
  });
}

export function DiffView({
  file,
  stat,
  badge,
  lines,
  onLineClick,
  renderUnderLine,
}: {
  /** 文件头。不给就不渲染头部。 */
  file?: string;
  /** 文件头右侧的增删统计,如 `+11 / −0`。 */
  stat?: ReactNode;
  /** 文件头里紧跟文件名的徽章,如「命中 migrations」。 */
  badge?: ReactNode;
  lines: DiffLine[];
  /** 给了就让行可点(审批页靠它挂行级批注)。 */
  onLineClick?: (lineNo: number) => void;
  /** 在某一行下面插内容(批注列表、输入框)。 */
  renderUnderLine?: (lineNo: number) => ReactNode;
}) {
  const clickable = Boolean(onLineClick);

  return (
    <div className="diff">
      {file && (
        <div className="fh">
          📄 {file}
          {badge}
          {stat && <span className="st">{stat}</span>}
        </div>
      )}
      {lines.map((line, index) => {
        // 行号优先用行自带的;没有就按顺序数。审批页看的是产物原文,行号必须与文件一致。
        const lineNo = line.no ?? index + 1;
        const under = renderUnderLine?.(lineNo);
        return (
          <div key={lineNo}>
            <div
              className={`l${line.kind ? ` ${line.kind}` : ""}`}
              style={clickable ? { cursor: "pointer", display: "flex", gap: 8 } : undefined}
              onClick={clickable ? () => onLineClick?.(lineNo) : undefined}
            >
              {clickable && (
                <span className="mono" style={{ color: "var(--ink3)", width: 40, flex: "none" }}>
                  {lineNo}
                </span>
              )}
              {/* 空行也要占一行高,否则 diff 的行号会和文件对不上。 */}
              <span>{line.text || " "}</span>
            </div>
            {under}
          </div>
        );
      })}
    </div>
  );
}
