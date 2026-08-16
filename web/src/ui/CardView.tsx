/**
 * 一张知识卡片的呈现。
 *
 * ## 这一个是新写的,不是抽出来的
 *
 * `DiffView` 与 `LogViewer` 在页面里本来就有实现,收成一处即可。知识卡片不一样:基因组页
 * 一直用表格展示卡片列表,**从来没有过"单张卡片"这个渲染单位**。而对话里的 `card-ref` 块
 * 需要的正是它——员工引用一张卡片时给的是一张卡,不是一行表格。
 *
 * 所以这里是先建立这个单位,再让基因组页用上它。顺序反过来的话(先在对话里写一个),
 * 三个月后基因组页想改卡片长相就会发现有两份实现。
 *
 * ## 命中数为什么总是露出来
 *
 * `hits` 是这套自进化机制唯一对外可见的"这条经验到底有没有用"(§10.2 自然选择)。把它藏进
 * 详情页的话,卡片该不该淘汰就没有人看得见了。
 */
import type { ReactNode } from "react";
import { Tag } from "./kit";

const CONFIDENCE_TONE = { high: "ok", medium: "warn", low: "bad" } as const;

export function CardView({
  title,
  id,
  summary,
  confidence,
  hits,
  meta,
  actions,
  onOpen,
}: {
  title: string;
  /** 卡片 id,等宽字体显示在标题下。 */
  id?: string;
  summary?: string;
  /** `high` / `medium` / `low`,其余值按中性色显示。 */
  confidence?: string;
  hits?: number;
  /** 附加信息行(更新时间、来源任务等)。 */
  meta?: ReactNode;
  /** 右下角的操作区。 */
  actions?: ReactNode;
  onOpen?: () => void;
}) {
  const tone = CONFIDENCE_TONE[confidence as keyof typeof CONFIDENCE_TONE] ?? "mute";

  return (
    <div
      className="tk"
      style={onOpen ? undefined : { cursor: "default" }}
      onClick={onOpen}
      role={onOpen ? "button" : undefined}
    >
      <b>{title}</b>
      {id && <div className="m mono">{id}</div>}
      {summary && <div className="m">{summary}</div>}
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 6, flexWrap: "wrap" }}>
        {confidence && <Tag tone={tone}>置信度 {confidence}</Tag>}
        {hits !== undefined && <Tag tone="mute">命中 {hits} 次</Tag>}
        {meta}
        {actions && <span style={{ marginLeft: "auto" }}>{actions}</span>}
      </div>
    </div>
  );
}
