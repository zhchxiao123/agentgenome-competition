/**
 * 工作台:一屏回答「现在有什么需要我」。
 *
 * 「待我审批」单独一块并带**等待时长**——PRD 08 定了审批超时不自动放行,代价是没人管的
 * 任务会一直停着。这个代价必须在人一进来就看见。
 *
 * ## 待办与审批分开呈现
 *
 * 「我的待办」是**派给人的活**(要交产物),「待我审批」是**裁决**(看整个任务的 diff,
 * 可以否决)。混在一起的话,"轮到你干活"与"轮到你拍板"会长得一样,而它们需要的注意力
 * 完全不同——见 ADR-0009 的代价段。
 *
 * ## 待确认与已升级人工是两件事
 *
 * 两者都是"机器停手等人",但**待确认是健康的、计划内的**:人做的是确认或调整,不是诊断。
 * 把它算进"已升级人工"那个数字里的话,一屏之内最刺眼的那个计数会被一批健康任务顶高,而
 * 真正出事的那个被淹没——这正是那条区分要防的事,只是方向相反。
 */
import { useCallback, useEffect, useState } from "react";
import { ApiError, api, type GenomeTaskSummary, type TaskSummary } from "../api/client";
import { subscribe } from "../api/live";
import { Card, Empty, Note, Tag, stateTone } from "../ui/kit";
import { MyTodos } from "./MyTodos";

const OPEN = ["DEVELOPING", "UNIT_TESTING", "INTEGRATION_TESTING", "MERGING"];

export function Workbench({ onOpen, onGo, actor = "" }: { onOpen: (id: string) => void; onGo: (page: string) => void; actor?: string }) {
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null);
  const [error, setError] = useState("");
  // **token 消耗不能从任务列表里加出来。** 列表只给还需要人过问的任务,已完成的不在里面——
  // 而那才是消耗的大头,于是这个数字永远偏小,还会在任务完成的瞬间不升反降。
  // `/insights/costs` 是按全部任务算的,那才是这个卡片要的口径。
  const [tokens, setTokens] = useState<number | null>(null);
  const [genome, setGenome] = useState<GenomeTaskSummary[]>([]);

  const reload = useCallback(() => {
    Promise.all([api.tasks(), api.genomeTasks()])
      .then(([foundTasks, foundGenome]) => {
        setTasks(foundTasks);
        setGenome(foundGenome.items);
        setError("");
      })
      .catch((e: ApiError) => setError(e.detail));
    // 成本是这一屏的次要数字,拉不到不该把「现在有什么需要我」整屏判死——
    // 单独降级成一个「—」,其余卡片照常。
    api.costs().then((report) => setTokens(report.total_tokens)).catch(() => setTokens(null));
  }, []);

  useEffect(() => {
    reload();
    return subscribe(reload);
  }, [reload]);

  if (error) return <Empty>拉不到任务:{error}</Empty>;
  if (!tasks) return <Empty>加载中…</Empty>;

  const running = tasks.filter((t) => OPEN.includes(t.state));
  const waiting = tasks.filter((t) => t.state === "REVIEWING");
  const escalated = tasks.filter((t) => t.state === "ESCALATED");
  // **待确认不进 escalated。** 它进的是待办,见文件头。
  const awaitingDev = tasks.filter((t) => t.pending_todo?.kind === "split");
  const awaitingGenome = genome.filter((t) => t.state === "AWAITING_CONFIRMATION");
  const awaitingCount = awaitingDev.length + awaitingGenome.length;

  return (
    <>
      <h1>工作台</h1>
      <div className="sub">研发任务与数字员工协同总览</div>

      <div className="grid g5">
        <Card><div className="lab">运行中任务</div><div className="val blue">{running.length}</div></Card>
        <Card>
          <div className="lab">待我审批</div>
          <div className="val amber">{waiting.length}</div>
          <Note tone="warn">审批超时<b>不会</b>自动放行</Note>
        </Card>
        <Card>
          <div className="lab">待我确认</div>
          <div className="val amber">{awaitingCount}</div>
          {/* 动作是"确认",不是"诊断"——文案不同不是措辞偏好,是那条区分在界面上的兑现。 */}
          <Note>机器在等一个回答,任务是健康的</Note>
        </Card>
        <Card>
          <div className="lab">已升级人工</div>
          <div className="val bad">{escalated.length}</div>
          <div className="dt">{escalated[0]?.escalate_reason ?? "—"}</div>
        </Card>
        <Card>
          <div className="lab">token 消耗</div>
          {/* 拉不到就显示「—」。显示 0 的话,"还没花钱"和"这个数字没拿到"分不出来。 */}
          <div className="val purple">{tokens === null ? "—" : tokens.toLocaleString()}</div>
        </Card>
      </div>

      <div className="grid g3" style={{ marginTop: 12 }}>
        <Card title="我的任务" extra={<a onClick={() => onGo("tasks")}>查看全部 ›</a>}>
          {tasks.length === 0 ? (
            <Empty>还没有任务。去「需求管理」提一个。</Empty>
          ) : (
            <table>
              <tbody>
                {tasks.slice(0, 6).map((task) => (
                  <tr key={task.id} onClick={() => onOpen(task.id)}>
                    <td className="mono">{task.id}</td>
                    <td>{task.title}</td>
                    <td><Tag tone={stateTone(task.state)}>{task.state}</Tag></td>
                    <td className="mono">{task.fix_rounds > 0 ? `第 ${task.fix_rounds + 1} 轮` : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card title="待我确认" extra={<a onClick={() => onGo("tasks")}>去看板 ›</a>}>
          {awaitingCount === 0 ? (
            <Empty>没有等我确认的。</Empty>
          ) : (
            <table>
              <tbody>
                {awaitingDev.map((task) => (
                  <tr key={task.id} className="await" onClick={() => onOpen(task.id)}>
                    <td>
                      {task.title}
                      <div className="mono">{task.id}</div>
                    </td>
                    <td style={{ textAlign: "right" }}><Tag tone="pur">待确认拆分</Tag></td>
                  </tr>
                ))}
                {awaitingGenome.map((task) => (
                  <tr key={task.id} className="await" onClick={() => onOpen(task.id)}>
                    <td>
                      {task.title}
                      <div className="mono">{task.id}</div>
                    </td>
                    <td style={{ textAlign: "right" }}>
                      {/* 等太久的要被标出来,否则它会被无声地遗忘。**标记不判死。** */}
                      {task.overdue ? <Tag tone="warn">等太久了</Tag> : <Tag tone="pur">待确认</Tag>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        {actor ? (
          <MyTodos assignee={actor} />
        ) : (
          <Card title="我的待办"><Empty>先在顶栏声明身份，才能查看分派给“我”的 human Job。</Empty></Card>
        )}

        <Card title="待我审批">
          {waiting.length === 0 ? (
            <Empty>没有等着的。</Empty>
          ) : (
            <table>
              <tbody>
                {waiting.map((task) => (
                  <tr key={task.id} onClick={() => onGo("review")}>
                    <td>{task.title}<div className="mono">{task.id}</div></td>
                    {/* 风险是**二值 + 命中规则**,不是 0–100 的分数。 */}
                    <td style={{ textAlign: "right" }}><Tag tone="bad">{task.risk_level ?? "high"}</Tag></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>
    </>
  );
}
