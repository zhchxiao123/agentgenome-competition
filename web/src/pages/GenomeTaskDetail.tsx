import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  api,
  type GenomeTaskProgress,
  type GenomeTaskSummary,
  type LogPage,
  type TaskTrace,
} from "../api/client";
import { subscribe } from "../api/live";
import { BlockRun, BlockView, groupBlocks } from "../chat/blocks";
import { Empty, Note, Tag } from "../ui/kit";
import { BoundaryGate } from "./BoundaryGate";
import { LifecycleEvents } from "./TaskDetailPanel";
import { relativeTime, taskStateLabel, taskStateSummary, taskStepLabel } from "./taskPresentation";

const KIND_LABEL: Record<string, string> = {
  init: "初始化",
  reinit: "重建",
  distill: "蒸馏",
  backfill: "补写",
  deepen: "深化",
};

const STATUS_LABEL: Record<string, string> = {
  done: "已建",
  failed: "失败",
  pending: "排队中",
};

type DetailTab = "overview" | "execution" | "artifacts" | "events";

export function GenomeTaskDetail({
  id,
  onClose,
  onChanged,
}: {
  id: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [task, setTask] = useState<GenomeTaskSummary | null>(null);
  const [progress, setProgress] = useState<GenomeTaskProgress | null>(null);
  const [logs, setLogs] = useState<LogPage | null>(null);
  const [tab, setTab] = useState<DetailTab>("overview");
  const [moreOpen, setMoreOpen] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [error, setError] = useState("");
  const [panelError, setPanelError] = useState("");
  const [logsError, setLogsError] = useState("");
  const [actionError, setActionError] = useState("");
  const [rebuilding, setRebuilding] = useState(false);
  const [resolving, setResolving] = useState(false);

  const reload = useCallback(() => {
    api
      .genomeTask(id)
      .then((found) => {
        setTask(found);
        setError("");
      })
      .catch((e: ApiError) => setError(e.detail));
    api
      .genomeProgress(id)
      .then((found) => {
        setProgress(found);
        setPanelError("");
      })
      .catch((e: ApiError) => setPanelError(e.detail));
    api
      .logs(id)
      .then((found) => {
        setLogs(found);
        setLogsError("");
      })
      .catch((e: ApiError) => setLogsError(e.detail));
  }, [id]);

  useEffect(() => {
    setTask(null);
    setProgress(null);
    setLogs(null);
    setTab("overview");
    setMoreOpen(false);
    reload();
    return subscribe(reload, id);
  }, [reload, id]);

  if (error) return <Empty>拉不到基因组任务:{error}</Empty>;
  if (!task) return <Empty>加载中…</Empty>;

  const failed = (progress?.modules ?? []).filter((row) => row.status === "failed");
  const needsHuman = task.state === "AWAITING_CONFIRMATION" || (task.state === "FAILED" && task.origin === "human");
  const isConfirmation = task.state === "AWAITING_CONFIRMATION";
  const canCancel = task.state !== "FAILED";
  // 闸门答复后服务端会自动接上驱动;这个按钮是驱动断了(进程重启)时的接回入口。
  // 待确认与终态都不该出现它:前者要人答闸门,后者没有下一步。
  const canResume = ["SCANNING", "DEEP_READ", "SUMMARISING"].includes(task.state);

  return (
    <section className="task-detail" aria-label="任务详情">
      <header className="task-detail-header">
        <div className="task-detail-heading">
          <div className="task-detail-eyebrow">
            <span className="mono">{task.id}</span>
            <Tag tone="pur">{KIND_LABEL[task.kind] ?? task.kind}</Tag>
            <Tag tone={isConfirmation ? "pur" : needsHuman ? "bad" : "mute"}>{taskStateLabel(task.state)}</Tag>
          </div>
          <h1>{task.title}</h1>
          <p>{task.subject ? `模块 ${task.subject}` : "全量知识范围"}</p>
        </div>
        <div className="task-detail-actions">
          {task.state === "FAILED" && task.origin === "human" && (
            <button
              className="btn pri"
              disabled={resolving}
              onClick={() => {
                if (!confirm("确认这次失败已经处理完？任务会退出“需我处理”，失败状态和记录仍会保留。")) return;
                setResolving(true);
                setActionError("");
                api.resolveGenomeIntervention(task.id)
                  .then(() => { onChanged(); onClose(); })
                  .catch((e: ApiError) => {
                    setResolving(false);
                    setActionError(e.detail);
                  });
              }}
            >
              {resolving ? "处理中…" : "标记已处理"}
            </button>
          )}
          {canResume && (
            <button
              className="btn pri"
              disabled={resuming}
              onClick={() => {
                setResuming(true);
                api
                  .runGenomeTask(task.id)
                  .then(() => {
                    reload();
                    onChanged();
                  })
                  .catch((e: ApiError) => {
                    setResuming(false);
                    setPanelError(e.detail);
                  });
              }}
            >
              {resuming ? "推进中…" : "▶ 继续推进"}
            </button>
          )}
          <button className="btn" onClick={onClose}>收起</button>
          {canCancel && (
            <div className="task-more">
              <button className="btn" aria-expanded={moreOpen} onClick={() => setMoreOpen((open) => !open)}>
                更多
              </button>
              {moreOpen && (
                <div className="task-more-menu">
                  <button
                    className="btn bad"
                    onClick={() => {
                      if (confirm(`取消 ${task.id}?`)) {
                        api
                          .cancelGenomeTask(task.id)
                          .then(() => {
                            setMoreOpen(false);
                            reload();
                            onChanged();
                          })
                          .catch((e: ApiError) => setError(e.detail));
                      }
                    }}
                  >
                    取消任务
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      </header>

      {actionError && <Note tone="warn">操作失败:{actionError}</Note>}

      <nav className="task-detail-tabs" role="tablist" aria-label="详情分区">
        {([
          ["overview", "概览"],
          ["execution", "执行过程"],
          ["artifacts", "产物"],
          ["events", "事件日志"],
        ] as const).map(([value, label]) => (
          <button key={value} role="tab" aria-selected={tab === value} className={tab === value ? "on" : ""} onClick={() => setTab(value)}>
            {label}
          </button>
        ))}
      </nav>

      <div className="task-detail-content">
        {tab === "overview" && (
          <div className="genome-task-overview">
            <section className="task-current-card">
              <div className="task-current-main">
                <span className="task-eyebrow">当前状态</span>
                <h3>{taskStepLabel(task.state)}</h3>
                <p>{taskStateSummary(task.state)}</p>
              </div>
              <Tag tone={isConfirmation ? "pur" : needsHuman ? "bad" : "ok"}>{isConfirmation ? "等待确认" : needsHuman ? "需要诊断" : "无需人工操作"}</Tag>
            </section>

            {task.state === "FAILED" && task.origin === "system" && (
              <Note>系统任务失败已收口并记录事件，不进入人工待办。</Note>
            )}
            {task.failure_reason && <Note tone="warn">失败原因：{task.failure_reason}</Note>}
            {task.state === "AWAITING_CONFIRMATION" && (
              <BoundaryGate taskId={task.id} onAnswered={() => { reload(); onChanged(); }} />
            )}

            <section className="task-panel">
              <h3>任务信息</h3>
              <div className="task-facts">
                <div><span>任务类型</span><b>{KIND_LABEL[task.kind] ?? task.kind}</b></div>
                <div><span>知识范围</span><b>{task.subject ?? "全量"}</b></div>
                <div><span>已消耗</span><b>{task.tokens_used.toLocaleString()} tokens</b></div>
                <div><span>预算</span><b>{task.budget_tokens?.toLocaleString() ?? "使用根配置"}</b></div>
                <div><span>发起来源</span><b>{task.origin === "human" ? "人工" : "系统"}</b></div>
                <div><span>最近更新</span><b>{relativeTime(task.updated_at)}</b></div>
              </div>
            </section>
          </div>
        )}

        {tab === "execution" && (
          <section className="task-panel genome-progress-panel">
            <div className="task-panel-heading">
              <div><span className="task-eyebrow">执行过程</span><h3>逐模块进度</h3></div>
              {progress?.started && <span className="m">{progress.modules?.length ?? 0} 个模块</span>}
            </div>
            {panelError ? (
              <Note tone="warn">拉不到进度:{panelError}</Note>
            ) : !progress?.started ? (
              <Empty>深读还没开始。</Empty>
            ) : (
              <table>
                <tbody>
                  {(progress.modules ?? []).map((row) => (
                    <tr key={row.module_id}>
                      <td className="mono">{row.module_id}</td>
                      <td><Tag tone={row.status === "failed" ? "bad" : row.status === "done" ? "ok" : "mute"}>{STATUS_LABEL[row.status] ?? row.status}</Tag></td>
                      <td className="m">{row.detail}</td>
                      <td className="m mono">{row.duration_s ? `${row.duration_s}s` : ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {failed.length > 0 && (
              <button
                className="btn"
                disabled={rebuilding}
                onClick={() => {
                  setRebuilding(true);
                  api
                    .reinitModules(failed.map((row) => row.module_id))
                    .then(() => { setActionError(""); onChanged(); })
                    .catch((e: ApiError) => setActionError(e.detail))
                    .finally(() => setRebuilding(false));
                }}
              >
                重建这 {failed.length} 个失败的模块
              </button>
            )}
          </section>
        )}
        {/* 员工真正干了什么(工具调用、模型输出)。没跑过作业就整段不出现——
            空态由上面的「逐模块进度」承担,两段各自空着会互相抢着解释同一件事。 */}
        {tab === "execution" && <GenomeTrace id={id} />}

        {tab === "artifacts" && (
          <section className="task-panel">
            <div className="task-panel-heading"><div><span className="task-eyebrow">任务输出</span><h3>知识产物</h3></div></div>
            {(progress?.pull_requests ?? []).length > 0 ? (
              <div className="task-artifact-list">
                {(progress?.pull_requests ?? []).map((number) => (
                  <a className="task-artifact" href={`#/pulls/${number}`} key={number}>
                    <span><strong>知识 PR #{number}</strong><small>查看本次知识更新</small></span><span>查看 →</span>
                  </a>
                ))}
              </div>
            ) : <Empty>这个任务还没有产出知识 PR。</Empty>}
            {task.source_task_id && <Note>由研发任务 <span className="mono">{task.source_task_id}</span> 触发。</Note>}
          </section>
        )}

        {tab === "events" && <LifecycleEvents logs={logs} error={logsError} />}
      </div>
    </section>
  );
}

function GenomeTrace({ id }: { id: string }) {
  /**
   * 基因组作业的执行轨迹。数据形状与研发任务的执行轨迹相同(同一套归一化事件流),
   * 但走 `/genome/tasks/{id}/trace`——基因组作业的产物目录按模块铺,不走槽位编址。
   */
  const [trace, setTrace] = useState<TaskTrace | null>(null);
  const [error, setError] = useState("");

  const reload = useCallback(() => {
    api
      .genomeTrace(id)
      .then((found) => {
        setTrace(found);
        setError("");
      })
      .catch((e: ApiError) => setError(e.detail));
  }, [id]);

  useEffect(() => {
    reload();
    return subscribe(reload, id);
  }, [id, reload]);

  if (error) return <Note tone="warn">拉不到执行轨迹:{error}</Note>;
  if (!trace || trace.stages.length === 0) return null;

  return (
    <section className="task-panel">
      <div className="task-panel-heading">
        <div><span className="task-eyebrow">执行轨迹</span><h3>员工干了什么</h3></div>
      </div>
      {trace.stages.map((stage) => {
        const tools = stage.blocks.filter((block) => block.kind === "tool-step").length;
        return (
          <details key={`${stage.stage}-${stage.number}`} open={trace.stages.length === 1}>
            <summary>
              <span className="mono">{stage.stage}</span>
              <span className="m"> · {stage.blocks.length} 条执行信息 · {tools} 次工具调用</span>
            </summary>
            {stage.blocks.length === 0 ? (
              <Empty>没有对话轨迹</Empty>
            ) : (
              <div className="blocks">
                {groupBlocks(stage.blocks).map((item, index) =>
                  Array.isArray(item) ? (
                    <BlockRun key={`g-${index}`} blocks={item} />
                  ) : (
                    <BlockView key={item.seq} block={item} />
                  ),
                )}
              </div>
            )}
          </details>
        );
      })}
    </section>
  );
}
