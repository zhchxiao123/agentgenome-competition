/**
 * 精化环的迭代时间线:这个任务在送门禁之前被批判了几轮、每轮说了什么、为什么停、花了多少。
 *
 * ## 数据全部来自事件面,前端不重新推导
 *
 * 轮次、结论、停的原因、花费都由后端在跑完那一刻写进拓扑事件里。前端自己数一遍的话,界面上
 * 的轮数与真正跑过的轮数会在某个版本上悄悄分叉,而那时没人能判断哪一份是对的。
 *
 * ## 意见原文按指针取,不塞进事件
 *
 * 事件里只有"第几轮、通过没有、几条意见、产物目录在哪"。意见全文住在产物面,点开才拉——
 * 同一件事只由一个平面记录内容,别的平面只记指针;两个平面各存一份必然发散。
 *
 * ## 没进环的任务这一块整个不出现
 *
 * 不是显示"0 轮"。空环会让"这个任务没走批判"和"走了但没意见"看起来一样,而它们在判断策略
 * 该不该继续开着时是两回事。
 */
import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api/client";
import { Empty, Note, Tag } from "../ui/kit";

/** 停在哪一条判据上。**跑满轮次停和收敛停是两种完全不同的信号。** */
const STOP_LABEL: Record<string, string> = {
  converged: "评审通过",
  "max-rounds": "达轮次上限",
  budget: "环预算用尽",
  "checker-failed": "批判本身失败(本轮未经批判)",
};

/** 为什么进的环。空串表示没进环——那时这个组件根本不渲染。 */
const WHY_LABEL: Record<string, string> = {
  "protected-paths": "计划命中受保护路径",
  modules: "计划命中的模块数达到阈值",
  "changed-files": "上一轮改动的文件数达到阈值",
};

const SEVERITY_ORDER: Record<string, number> = { blocker: 0, major: 1, minor: 2 };

/** 默认展示几条意见。其余折叠——评审的产出是排过序的,界面不该把顺序摊平。 */
const VISIBLE = 3;

export type Finding = {
  file: string;
  line?: number;
  severity: string;
  issue: string;
  suggestion: string;
};

type NodeRow = {
  id: string;
  kind: string;
  ok: boolean;
  approved?: boolean;
  findings?: number;
  slot?: string;
};

export type LoopRun = {
  stage: string;
  template_id: string;
  stopped_because: string;
  rounds: number;
  tokens_used: number | null;
  tokens_available: boolean;
  nodes: NodeRow[];
  why?: string;
};

/** 从事件流里挑出"跑完的环"与"为什么进环"。两条是分开写的事件,这里合成一条。 */
export function loopRuns(
  items: { kind: string; payload?: Record<string, unknown> }[],
): LoopRun[] {
  const topology = items
    .filter((item) => item.kind === "topology")
    .map((item) => item.payload ?? {});
  // 两条拓扑事件靠 `phase` 分开:`chosen` 是"选了哪张图",`ran` 是"这张图跑成了什么"。
  // 按"有没有某个键"去猜的话,后端加第三种事件时这里会静默错。
  const whys = topology
    .filter((payload) => payload.phase === "chosen")
    .map((payload) => String(payload.why ?? ""));
  const runs = topology
    .filter((payload) => payload.phase === "ran")
    .map((payload) => payload as unknown as LoopRun);
  // 选模板与跑完是一一对应的两条事件,按顺序配对——只有进了环的那次才会有 run 事件。
  const hits = whys.filter((why) => why !== "");
  return runs.map((run, index) => ({ ...run, why: hits[index] ?? "" }));
}

export function sortFindings(findings: Finding[]): Finding[] {
  return [...findings].sort(
    (left, right) => (SEVERITY_ORDER[left.severity] ?? 9) - (SEVERITY_ORDER[right.severity] ?? 9),
  );
}

function Findings({ taskId, slot }: { taskId: string; slot: string }) {
  const [findings, setFindings] = useState<Finding[] | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .artifact(taskId, `artifacts/${slot}/result.json`)
      .then((text) => {
        if (!alive) return;
        try {
          setFindings(sortFindings(JSON.parse(text).findings ?? []));
        } catch {
          setFailed(true);
        }
      })
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, [taskId, slot]);

  if (failed) return <Note tone="warn">这一轮的意见原文读不到了(产物可能已被清理)</Note>;
  if (!findings) return <Empty>加载中…</Empty>;
  if (findings.length === 0) return <Empty>这一轮没有提出意见</Empty>;

  const shown = expanded ? findings : findings.slice(0, VISIBLE);
  return (
    <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
      {shown.map((finding, index) => (
        <li key={`${finding.file}-${finding.line ?? index}`} style={{ marginBottom: 4 }}>
          <Tag tone={finding.severity === "blocker" ? "bad" : "mute"}>{finding.severity}</Tag>{" "}
          <span className="mono">
            {finding.file}
            {finding.line ? `:${finding.line}` : ""}
          </span>
          <div>{finding.issue}</div>
          <div className="m">建议:{finding.suggestion}</div>
        </li>
      ))}
      {findings.length > VISIBLE && (
        <button className="btn sm" onClick={() => setExpanded(!expanded)}>
          {expanded ? "收起" : `还有 ${findings.length - VISIBLE} 条`}
        </button>
      )}
    </ul>
  );
}

function checkerLabel(node: NodeRow, round: number): string {
  if (!node.ok) return `第 ${round + 1} 轮 执行失败`;
  return `第 ${round + 1} 轮 ${node.approved ? "通过" : `${node.findings ?? 0} 条意见`}`;
}

export function CritiqueTimeline({ id }: { id: string }) {
  const [runs, setRuns] = useState<LoopRun[] | null>(null);
  const [error, setError] = useState("");

  const reload = useCallback(() => {
    api
      .events(id, 0, 1000)
      .then((page) => {
        setRuns(loopRuns(page.items));
        setError("");
      })
      .catch((e: ApiError) => setError(e.detail));
  }, [id]);

  useEffect(() => reload(), [reload]);

  // 拉不到时降级成一条说明,不是让整个任务详情报错——它是详情页里的次要面板。
  if (error) return <Note tone="warn">拉不到精化环记录:{error}</Note>;
  // **没进过环就整块不出现**,包括还没拉到的时候。
  if (!runs || runs.length === 0) return null;

  return (
    <>
      {runs.map((run, index) => (
        <div key={`${run.stage}-${index}`} style={{ marginBottom: 12 }}>
          <div className="m" style={{ marginBottom: 6 }}>
            <b>{run.rounds} 轮批判</b> · 进环原因:{WHY_LABEL[run.why ?? ""] ?? "策略命中"} ·
            停在:{STOP_LABEL[run.stopped_because] ?? run.stopped_because} · 花费:
            <span className="mono">
              {run.tokens_available ? (run.tokens_used ?? 0).toLocaleString() : "不可得"}
            </span>
          </div>
          {run.nodes
            .filter((node) => node.kind === "checker")
            .map((node, round) => (
              <div key={node.slot ?? round} style={{ marginBottom: 8 }}>
                <Tag tone={!node.ok ? "bad" : node.approved ? "ok" : "warn"}>
                  {checkerLabel(node, round)}
                </Tag>
                {!node.ok && <Note tone="warn">这一轮批判自己失败了,产出未经批判就送了门禁</Note>}
                {node.ok && !node.approved && node.slot && (
                  <Findings taskId={id} slot={node.slot} />
                )}
              </div>
            ))}
        </div>
      ))}
    </>
  );
}
