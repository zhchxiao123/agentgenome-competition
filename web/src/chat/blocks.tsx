/**
 * 块渲染器注册表。
 *
 * ## 加一种块 = 注册一个渲染器
 *
 * 消息组件只做查表与分发,不认识任何具体块类型。写成一串 `if (kind === ...)` 的话,
 * 每加一种块都要改消息组件,而那个文件会慢慢变成所有块类型的大杂烩。
 *
 * ## 未知块类型降级为纯文本
 *
 * **不抛错、不白屏。** 后端先上线一种新块是正常的演进节奏,前端应当容忍——这与
 * `live.ts` 里"解析不了的推送直接丢"是同一个判断:局部降级好过整体崩掉。反过来的话,
 * 前端会变成后端演进的闸门。
 *
 * ## 复用共享组件
 *
 * diff / 日志 / 卡片都走 `ui/` 里那三个组件,与任务详情页同一份实现。对话里的 diff 和
 * 审批页的 diff 长得一样、行为一样,不是巧合而是同一段代码。
 */
import { useState } from "react";
import type { ReactNode } from "react";
import type { BlockItem } from "../api/client";
import { CardView } from "../ui/CardView";
import { DiffView, parseDiff } from "../ui/DiffView";
import { LogViewer } from "../ui/LogViewer";
import { Markdown } from "../ui/Markdown";
import { Tag } from "../ui/kit";

export type BlockHandlers = {
  onOpenFile?: (path: string, line?: number) => void;
  onOpenTask?: (taskId: string) => void;
  onOpenCard?: (cardId: string) => void;
  onAction?: (actionId: string) => void;
};

type Detail = Record<string, unknown>;

const text = (value: unknown): string => (typeof value === "string" ? value : "");
const num = (value: unknown): number | undefined =>
  typeof value === "number" ? value : undefined;

/**
 * 这一块属于哪条"泳道"。**决定它和前后的块能不能收进同一个容器。**
 *
 * 三条泳道对应三件性质不同的东西:员工在盘算什么(thinking)、它去查了什么(tool-step)、
 * 以及**这些是不是它派出去的子员工干的**(`parent_tool_use_id`)。正文不进任何泳道——
 * 它是结论,永远独占一行。
 *
 * **"谁产出的"优先于"这是哪一类"**:子员工自己也会思考、也会调工具、也会说话,把它的
 * 思考并进主线的思考里,用户就再也分不清哪一段推理是谁的了。
 */
function laneOf(block: BlockItem, agentAware: boolean): string | null {
  const parent = text((block.detail ?? {})["parent_tool_use_id"]);
  if (agentAware && parent) return `agent:${parent}`;
  if (block.kind === "tool-step") return "tool";
  if (block.kind === "thinking") return "thinking";
  return null;
}

/**
 * 一串连续的思考,收成**一个默认折叠**的容器。
 *
 * ## 为什么不直接当正文渲染
 *
 * 用户要读的是结论。把一整段内心独白("Let me first check what the project looks like.
 * I'll use…")当正文铺开,结论会被挤到屏幕外——而那段独白**看起来和答复一模一样**,
 * 用户没有任何线索判断自己读的是盘算还是答案。
 *
 * ## 为什么也不直接丢掉
 *
 * 与工具调用同一条理由:过程可见是信任的来源。想知道"它凭什么这么说"的时候,展开就能看。
 * 默认折叠只是把"过程"放回它该在的位置,不是把它藏起来。
 */
function ThinkingGroup({ blocks }: { blocks: BlockItem[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="blk">
      <button
        className="bh"
        style={{ width: "100%", border: 0, cursor: "pointer", textAlign: "left" }}
        onClick={() => setOpen((value) => !value)}
      >
        <span>💭 思考过程{blocks.length > 1 ? ` · ${blocks.length} 段` : ""}</span>
        <span className="r">{open ? "收起 ▴" : "展开 ▾"}</span>
      </button>
      {open && (
        <div className="bc">
          {blocks.map((block) => (
            // 弱化呈现:它是过程,不该和结论抢注意力。仍然走 Markdown——推理里一样有列表
            // 和代码,当裸字符吐出来只会更难读。
            <div key={block.seq} className="m">
              <Markdown text={block.text} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * 子员工干的一段活,整段收进**一个**容器。
 *
 * 它的思考、工具调用、结论都在里面,按各自的形态渲染(递归走同一套分组,只是这一层不再
 * 按"谁产出的"分——里面本来就都是同一个子员工的)。
 *
 * **不摊平到主线里。** 摊平的话,一次派出去的子任务会在主对话流里插进十几行它自己的
 * 盘算与查证,而用户问的是主线那个问题——他需要知道"派了个子员工去查",不需要那个子员工
 * 的每一步都和主线的步骤混在一起排队。
 */
function SubAgentGroup({ blocks }: { blocks: BlockItem[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="blk">
      <button
        className="bh"
        style={{ width: "100%", border: 0, cursor: "pointer", textAlign: "left" }}
        onClick={() => setOpen((value) => !value)}
      >
        <span>🤖 子员工 · {blocks.length} 步</span>
        <span className="r">{open ? "收起 ▴" : "展开 ▾"}</span>
      </button>
      {open && (
        <div className="bc" style={{ borderLeft: "2px solid var(--line, #e5e7eb)", paddingLeft: 10 }}>
          {/* 递归分组时关掉"按谁产出的分"——里面已经都是同一个子员工了,再分一次会把整段
              又包进一个同样的容器,套娃到栈溢出。 */}
          {groupBlocks(blocks, false).map((entry) =>
            Array.isArray(entry) ? (
              <BlockRun key={entry[0]!.seq} blocks={entry} agentAware={false} />
            ) : (
              <BlockView key={entry.seq} block={entry} />
            ),
          )}
        </div>
      )}
    </div>
  );
}

/**
 * 一串同泳道的块该怎么渲染。**查表分发**,调用方不必知道有几条泳道。
 *
 * `agentAware` 必须与产出这一组的 `groupBlocks` 保持一致。不一致的后果是**套娃**:
 * 分组时说"这一层不按谁产出的分",渲染时却又按它分,于是子员工容器里的每一组都会被
 * 再包一层同样的子员工容器,一直包到栈溢出。
 */
export function BlockRun({
  blocks,
  agentAware = true,
}: {
  blocks: BlockItem[];
  agentAware?: boolean;
}) {
  const lane = laneOf(blocks[0]!, agentAware);
  if (lane?.startsWith("agent:")) return <SubAgentGroup blocks={blocks} />;
  if (lane === "thinking") return <ThinkingGroup blocks={blocks} />;
  return <ToolGroup blocks={blocks} />;
}

/**
 * 一串连续的工具调用,收成**一个**可折叠容器。
 *
 * **默认折叠**:过程可见但不淹没结论。折叠态给步数与总耗时——不展开也知道它查了多少、
 * 花了多久;展开后逐条显示读了什么、搜了什么。
 *
 * 每次调用各占一个块的话,一次三步的查证会在对话流里占三行灰条,把结论挤到屏幕外——而
 * 「工具调用过程可视化是信任的来源」要的是"看得见它在查",不是"被过程淹没"。
 */
export function ToolGroup({ blocks }: { blocks: BlockItem[] }) {
  const [open, setOpen] = useState(false);
  // 耗时取这一组里最后一步的值:它是"这一轮开始到这步结束"的秒数,也就是整组的用时。
  const elapsed = blocks
    .map((block) => num((block.detail ?? {})["elapsed_s"]))
    .filter((value): value is number => value !== undefined)
    .pop();

  return (
    <div className="blk">
      <button
        className="bh"
        style={{ width: "100%", border: 0, cursor: "pointer", textAlign: "left" }}
        onClick={() => setOpen((value) => !value)}
      >
        {/* **一步的时候直接把它做了什么写在头上。** 收成「查证过程 · 1 步」等于把唯一
            那条信息藏起来,而这个块的全部意义就是"看得见它具体读了什么"。 */}
        <span className={blocks.length === 1 ? "mono" : undefined}>
          {blocks.length === 1 ? `🔍 ${blocks[0]!.text}` : `🔍 查证过程 · ${blocks.length} 步`}
        </span>
        <span className="r">
          {elapsed !== undefined ? `用时 ${elapsed}s · ` : ""}
          {open ? "收起 ▴" : "展开 ▾"}
        </span>
      </button>
      {open && (
        <div className="bc" style={{ padding: 0 }}>
          {blocks.map((block) => (
            <ToolLine key={block.seq} block={block} />
          ))}
        </div>
      )}
    </div>
  );
}

/** 一步。**必须显示具体读了什么、跑了什么**——写成一句笼统的"正在思考"这个块就白做了。 */
function ToolLine({ block }: { block: BlockItem }) {
  const output = text((block.detail ?? {})["output"]);
  return (
    <div className="tool">
      <span className="ok">✓</span>
      <span className="mono">{block.text}</span>
      {output && <LogViewer text={output} maxHeight={140} />}
    </div>
  );
}

function FileRef({ block, onOpenFile }: { block: BlockItem } & BlockHandlers) {
  const line = num((block.detail ?? {})["line"]);
  return (
    <span className="fref" onClick={() => onOpenFile?.(block.text, line)}>
      📁 {block.text}
      {line !== undefined ? `:${line}` : ""}
    </span>
  );
}

function CardRef({ block, onOpenCard }: { block: BlockItem } & BlockHandlers) {
  const detail = (block.detail ?? {}) as Detail;
  const id = text(detail.card_id) || text(detail.title);
  return (
    <CardView
      title={text(detail.title) || id}
      summary={text(detail.summary)}
      confidence={text(detail.confidence) || undefined}
      hits={num(detail.hits)}
      onOpen={onOpenCard ? () => onOpenCard(id) : undefined}
    />
  );
}

function Diff({ block }: { block: BlockItem }) {
  const detail = (block.detail ?? {}) as Detail;
  return <DiffView file={text(detail.file) || undefined} lines={parseDiff(block.text)} />;
}

/**
 * 任务卡:标题 + 状态 + 风险 + 修复轮次 + 打开。
 *
 * **少了风险与轮次就没法判断它的处境**——一个第 3/3 轮还在 REVIEWING 的高风险任务,
 * 和一个第 1/3 轮的低风险任务,下一步动作完全不同,而只给 id 和状态时它们长得一样。
 */
function TaskCard({ block, onOpenTask }: { block: BlockItem } & BlockHandlers) {
  const detail = (block.detail ?? {}) as Detail;
  const taskId = text(detail.task_id);
  const rounds = num(detail.fix_rounds);
  const maxRounds = num(detail.max_fix_rounds);
  return (
    <div className="blk">
      <div className="bh">📋 任务卡</div>
      <div className="bc" style={{ display: "flex", gap: 8, alignItems: "center" }}>
        {/* 标题本身就可点——引用要是可达的,而"只有右边那个小箭头能点"是个需要发现的秘密。 */}
        <a style={{ fontWeight: 600 }} onClick={() => onOpenTask?.(taskId)}>
          {text(detail.title) || taskId}
        </a>
        {detail.state ? <Tag tone="warn">{text(detail.state)}</Tag> : null}
        {detail.risk ? <Tag tone="bad">{text(detail.risk)}</Tag> : null}
        {rounds !== undefined && (
          <span className="m">
            修复 {rounds}
            {maxRounds !== undefined ? `/${maxRounds}` : ""} 轮
          </span>
        )}
        <a style={{ marginLeft: "auto" }} onClick={() => onOpenTask?.(taskId)}>
          打开 ›
        </a>
      </div>
    </div>
  );
}

type GateRow = { id?: string; passed?: boolean; detail?: string };

function GateReport({ block }: { block: BlockItem }) {
  const rows = ((block.detail ?? {})["gates"] ?? []) as GateRow[];
  return (
    <div className="blk">
      <div className="bh">🛡 门禁结果</div>
      <div className="bc" style={{ padding: 0 }}>
        <table>
          <tbody>
            {rows.map((row, index) => (
              <tr key={row.id ?? index}>
                <td>{row.id}</td>
                <td>
                  <Tag tone={row.passed ? "ok" : "bad"}>{row.passed ? "通过" : "失败"}</Tag>
                </td>
                <td className="mono">{row.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** 纯文本。**未知块类型也走这里**——降级而不是白屏。走 Markdown 渲染,员工回复里的
 * 加粗、列表、围栏代码块才不会被当成裸字符原样吐出来。 */
function PlainText({ block }: { block: BlockItem }) {
  return <Markdown text={block.text} />;
}

function CodeBlock({ block }: { block: BlockItem }) {
  return <LogViewer text={block.text} />;
}

function ErrorBlock({ block }: { block: BlockItem }) {
  // 内联呈现,不弹全局 toast 打断心流。
  return <div className="note warn">⚠ {block.text}</div>;
}

type Action = { id?: string; label?: string };

/** 动作块。**贴着它所属的那条消息**——拆到右栏只会制造「两个地方都要看」的负担。 */
function ActionRow({ block, onAction }: { block: BlockItem } & BlockHandlers) {
  const actions = ((block.detail ?? {})["actions"] ?? []) as Action[];
  return (
    <div style={{ display: "flex", gap: 6, flexWrap: "wrap", margin: "0 0 9px" }}>
      {actions.map((action, index) => (
        <button
          key={action.id ?? index}
          className="btn sm"
          onClick={() => onAction?.(action.id ?? "")}
        >
          {action.label ?? action.id}
        </button>
      ))}
    </div>
  );
}

type Renderer = (props: { block: BlockItem } & BlockHandlers) => ReactNode;

/** 注册表。加一种块类型 = 在这里加一行。 */
const REGISTRY: Record<string, Renderer> = {
  text: PlainText,
  // 单独一段思考也走分组容器,形态才一致。连续的多段由 `groupBlocks` 收成一组。
  thinking: ({ block }) => <ThinkingGroup blocks={[block]} />,
  code: CodeBlock,
  // 单独一个工具块也走分组容器,形态才一致。连续的多个由 `MessageList` 收成一组。
  "tool-step": ({ block }) => <ToolGroup blocks={[block]} />,
  "file-ref": FileRef,
  "card-ref": CardRef,
  diff: ({ block }) => <Diff block={block} />,
  "task-card": TaskCard,
  "gate-report": ({ block }) => <GateReport block={block} />,
  action: ActionRow,
  error: ({ block }) => <ErrorBlock block={block} />,
};

export function BlockView({ block, ...handlers }: { block: BlockItem } & BlockHandlers) {
  const Render = REGISTRY[block.kind] ?? PlainText;
  return <Render block={block} {...handlers} />;
}

export const KNOWN_BLOCK_KINDS = Object.keys(REGISTRY);

/**
 * 把连续的**同泳道**块收成一组:思考归思考、查证归查证、子员工的活整段归一处。
 *
 * 每次调用各占一个块的话,一次三步的查证会在流里占三行,把结论挤到屏幕外——而
 * 「工具调用过程可视化是信任的来源」要的是"看得见它在查",不是"被过程淹没"。思考同理,
 * 而且更严重:它和正文长得一模一样,不收起来的话用户根本分不出自己在读盘算还是答案。
 *
 * 正文不进任何泳道,永远独占一项——它是结论。
 *
 * 对话工作台的消息流、任务详情页的执行轨迹都要这一步——两处的块来源不同(会话运行时
 * 的实时流 / 任务 Job 落盘的文件),但分组规则是同一条。
 *
 * `agentAware=false` 只给 `SubAgentGroup` 内部递归用,见那里的注释。
 */
export function groupBlocks(
  blocks: BlockItem[],
  agentAware = true,
): (BlockItem | BlockItem[])[] {
  const out: (BlockItem | BlockItem[])[] = [];
  let lane: string | null = null;
  for (const block of blocks) {
    const current = laneOf(block, agentAware);
    const last = out[out.length - 1];
    // **泳道相同才续上一组。** 只看 `Array.isArray(last)` 的话,一串思考后面紧跟一串
    // 工具调用会被并进同一个容器,而它们是两件事。
    if (current !== null && current === lane && Array.isArray(last)) last.push(block);
    else if (current !== null) out.push([block]);
    else out.push(block);
    lane = current;
  }
  return out;
}
