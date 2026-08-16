/**
 * 任务详情里的图:这一步跑的是哪张图、谁依赖谁、每个节点现在什么状态。
 *
 * ## 数据源是事件面,前端不重新推导
 *
 * 图与每个节点的结果都由后端在跑完那一刻写死。前端自己拼一遍的话,界面上画的图与真正跑过的
 * 图会在某个版本上悄悄分叉,而那时没人能判断哪一份是对的。
 *
 * ## 失败与"因此没跑"要分得开
 *
 * 被冻结的节点没有失败现场——把它们画成一样的红色,人会以为有五个地方坏了,而实际上只坏了
 * 一个。这与后端把 `failed` 与 `frozen` 分开记是同一条理由。
 *
 * ## 单节点图不画
 *
 * 画一个只有一个方框的"图"是噪音:它与"这个状态就是一次派发"表达的是同一件事,而后者已经
 * 写在别处了。
 */
import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api/client";
import { Card, Empty, Note, Tag } from "../ui/kit";

type GraphNode = { id: string; kind: string; ok: boolean; passed?: boolean };

export type TaskGraphRun = {
  stage: string;
  template_id: string;
  nodes: GraphNode[];
  failed?: string[];
  frozen?: string[];
  edges: string[][];
};

const STAGE_LABEL: Record<string, string> = {
  plan: "需求解析",
  develop: "开发",
  "unit-gate": "单元测试门禁",
};

/** 从事件流里把"选了哪张图"与"跑成了什么"配成一张可画的图。 */
export function graphRuns(
  items: { kind: string; payload?: Record<string, unknown> }[],
): TaskGraphRun[] {
  const topology = items
    .filter((item) => item.kind === "topology")
    .map((item) => item.payload ?? {});
  const chosen = topology.filter((payload) => payload.phase === "chosen" && payload.template);
  const ran = topology.filter((payload) => payload.phase === "ran");
  return ran
    .map((payload) => {
      // **按阶段配对,不按位置。** 选图这条事件每个阶段都有(包括单节点的),而"跑成了
      // 什么"只有多节点的图才记——按下标配的话,第一张图会配到需求解析那一步的单节点模板,
      // 于是整块图**永远画不出来**,而组件测试里只有一条选图事件,看不出这件事。
      const forStage = chosen.filter((item) => item.stage === payload.stage);
      const template = (forStage[forStage.length - 1]?.template ?? {}) as Record<
        string,
        unknown
      >;
      const ran = (payload.nodes ?? []) as GraphNode[];
      const declared = (template.nodes ?? []) as { id: string; kind?: string }[];
      // **画的是图里的全部节点,不只是跑过的那些。** 被冻结的节点根本没有结论,只按结论
      // 画的话它们会从图上消失——而"哪些因为上游失败而没跑"正是这时候最想知道的事。
      const nodes: GraphNode[] = declared.length
        ? declared.map((item) => {
            const outcome = ran.find((entry) => entry.id === item.id);
            return outcome ?? { id: item.id, kind: item.kind ?? "work", ok: false };
          })
        : ran;
      return {
        stage: String(payload.stage ?? ""),
        template_id: String(payload.template_id ?? ""),
        nodes,
        failed: (payload.failed ?? []) as string[],
        frozen: (payload.frozen ?? []) as string[],
        edges: (template.edges ?? []) as string[][],
      };
    })
    .filter((run) => run.nodes.length > 1);
}

function tone(run: TaskGraphRun, node: GraphNode): "ok" | "bad" | "warn" | "mute" {
  if (run.failed?.includes(node.id)) return "bad";
  // **冻结不是失败**:它没跑,所以既不是坏的也不是好的。
  if (run.frozen?.includes(node.id)) return "warn";
  return node.ok ? "ok" : "mute";
}

export function TaskGraph({ id }: { id: string }) {
  const [runs, setRuns] = useState<TaskGraphRun[] | null>(null);
  const [error, setError] = useState("");

  const reload = useCallback(() => {
    api
      .events(id, 0, 1000)
      .then((page) => {
        setRuns(graphRuns(page.items));
        setError("");
      })
      .catch((e: ApiError) => setError(e.detail));
  }, [id]);

  useEffect(() => reload(), [reload]);

  // 拉不到时降级成一条说明,不是让整个任务详情报错。
  if (error) return <Note tone="warn">拉不到执行图:{error}</Note>;
  if (!runs || runs.length === 0) return null;

  return (
    <Card title="执行图">
      {runs.map((run, index) => (
        <div key={`${run.stage}-${index}`} style={{ marginBottom: 12 }}>
          <div className="m" style={{ marginBottom: 6 }}>
            <b>{STAGE_LABEL[run.stage] ?? run.stage}</b> · {run.template_id} ·{" "}
            {run.nodes.length} 个节点
          </div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {run.nodes.map((node) => (
              <Tag key={node.id} tone={tone(run, node)}>
                {node.id}
                {run.frozen?.includes(node.id) ? "(上游失败,没跑)" : ""}
              </Tag>
            ))}
          </div>
          {run.edges.length === 0 ? (
            <Empty>没有依赖边——这些节点互不相干</Empty>
          ) : (
            <ul className="mono" style={{ margin: "6px 0 0", paddingLeft: 18 }}>
              {run.edges.map((edge) => (
                <li key={edge.join("→")}>
                  {edge[0]} → {edge[1]}
                </li>
              ))}
            </ul>
          )}
        </div>
      ))}
    </Card>
  );
}
