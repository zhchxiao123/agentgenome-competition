import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  api,
  type LogPage,
  type BlockItem,
  type TaskDetail,
  type TodoDetail,
  type TaskTrace as TaskTraceType,
  type TaskTraceStage,
} from "../api/client";
import { subscribe } from "../api/live";
import { priorityLabel } from "../priority";
import { BlockRun, BlockView, groupBlocks } from "../chat/blocks";
import { Empty, Note, Tag, stateTone } from "../ui/kit";
import { CritiqueTimeline } from "./CritiqueTimeline";
import { TaskGraph } from "./TaskGraph";
import { TaskJourney } from "./TaskJourney";
import { SplitVerdict } from "./MyTodos";
import {
  STAGE_LABEL,
  isTerminalDevState,
  devNeedsAttention,
  relativeTime,
  taskDisplayLabel,
  taskRunLabel,
  taskStage,
  taskStateLabel,
  taskStateSummary,
} from "./taskPresentation";

type DetailTab = "overview" | "execution" | "artifacts" | "events";
const RUN_STATUS_POLL_MS = 10_000;
const TRACE_POLL_MS = 5_000;

export function TaskDetailPanel({ id, onClose, onChanged, onOpenTask, onOpenRequirement }: { id: string; onClose: () => void; onChanged: () => void; onOpenTask: (id: string) => void; onOpenRequirement?: (id: string) => void }) {
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [logs, setLogs] = useState<LogPage | null>(null);
  const [tab, setTab] = useState<DetailTab>("overview");
  const [error, setError] = useState("");
  const [logsError, setLogsError] = useState("");
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState("");
  const [actionError, setActionError] = useState("");
  const [resolving, setResolving] = useState(false);
  const [retryOpen, setRetryOpen] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [retryText, setRetryText] = useState("");
  const [currentRequirementText, setCurrentRequirementText] = useState("");
  const [requirementState, setRequirementState] = useState("");
  const [parentRequirementId, setParentRequirementId] = useState("");
  const [runConfirmed, setRunConfirmed] = useState(false);
  const [pendingTodo, setPendingTodo] = useState<TodoDetail | null>(null);
  const [todoError, setTodoError] = useState("");

  const reload = useCallback(() => {
    api.task(id).then((found) => {
      setTask(found);
      if (found.execution_status !== "running") setRunning(false);
      setRunConfirmed(found.execution_status === "running");
      setError("");
      if (found.pending_todo?.kind === "split") {
        api.todo(found.pending_todo.id).then((todo) => {
          setPendingTodo(todo);
          setTodoError("");
        }).catch((reason: ApiError) => {
          setPendingTodo(null);
          setTodoError(reason.detail);
        });
      } else {
        setPendingTodo(null);
        setTodoError("");
      }
      if (found.requirement_id) {
        api.requirement(found.requirement_id).then((requirement) => {
          setRequirementState(requirement.state);
          setCurrentRequirementText(requirement.text);
          setParentRequirementId(requirement.parent_id);
        }).catch(() => {
          setRequirementState("");
          setCurrentRequirementText("");
          setParentRequirementId("");
        });
      } else {
        setRequirementState("");
        setParentRequirementId("");
        setCurrentRequirementText("");
      }
    }).catch((reason: ApiError) => setError(reason.detail));
    api.logs(id).then((found) => {
      setLogs(found);
      setLogsError("");
    }).catch((reason: ApiError) => setLogsError(reason.detail));
  }, [id]);

  useEffect(() => {
    setTask(null);
    setLogs(null);
    setRunError("");
    setTab("overview");
    setRetryOpen(false);
    setRetrying(false);
    setPendingTodo(null);
    setTodoError("");
    reload();
    return subscribe((notice) => {
      reload();
      if (notice.kind === "run_finished") setRunning(false);
    }, id);
  }, [id, reload]);

  useEffect(() => {
    if (task?.execution_status !== "interrupted") return;
    const failure = latestRunFailure(logs);
    if (failure) setRunError(failure);
  }, [logs, task?.execution_status]);

  useEffect(() => {
    if (!running && task?.execution_status !== "running") return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") reload();
    }, RUN_STATUS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [reload, running, task?.execution_status]);

  if (error) return <Empty>拉不到任务详情:{error}<button className="btn" onClick={onClose}>返回任务列表</button></Empty>;
  if (!task) return <Empty>加载中…</Empty>;

  const tabs: ReadonlyArray<[DetailTab, string]> = [
    ["overview", "概览"],
    ["execution", "执行过程"],
    ["artifacts", "产物"],
    ["events", "事件日志"],
  ];
  const isRunning = running || task.execution_status === "running";
  const awaitingSplit = task.pending_todo?.kind === "split";
  const runLabel = !isTerminalDevState(task.state) && task.can_run ? taskRunLabel(task.state, task.plan_retries, task.execution_status) : undefined;

  return (
    <div className="task-detail">
      <header className="task-detail-header">
        <div className="task-detail-heading">
          <div className="task-detail-title-row">
            <h1>{task.title}</h1>
            <Tag tone={stateTone(task.state)}>{awaitingSplit ? "等待拆分确认" : taskDisplayLabel(task.state, isRunning ? "running" : task.execution_status)}</Tag>
            {task.risk_level && <Tag tone={task.risk_level === "high" ? "bad" : "ok"}>{task.risk_level}</Tag>}
          </div>
          <div className="task-detail-sub"><span className="mono">{task.id}</span><span>·</span><span>{isRunning && runConfirmed ? "运行状态刚刚确认" : `${relativeTime(task.updated_at)}更新`}</span></div>
        </div>
        <div className="task-detail-actions">
          {task.state === "ESCALATED" && !task.intervention_resolved_at && task.requirement_id && (
            <button className="btn pri" onClick={() => {
              setRetryText(currentRequirementText || task.requirement);
              setRetryOpen(true);
            }}>
              修改需求并再试一次
            </button>
          )}
          {task.state === "ESCALATED" && !task.intervention_resolved_at && (
            <button
              className="btn"
              disabled={resolving}
              onClick={() => {
                if (!confirm("确认这次人工介入已经处理完？任务会退出“需我处理”，失败状态和记录仍会保留。")) return;
                setResolving(true);
                setActionError("");
                api.resolveTaskIntervention(task.id)
                  .then(() => { onChanged(); onClose(); })
                  .catch((reason: ApiError) => {
                    setResolving(false);
                    setActionError(reason.detail);
                  });
              }}
            >
              {resolving ? "处理中…" : "标记已处理"}
            </button>
          )}
          {runLabel && (
            <button
              className="btn pri"
              disabled={isRunning}
              onClick={() => {
                setRunning(true);
                setRunError("");
                api.run(task.id).catch((reason: ApiError) => {
                  setRunning(false);
                  setRunError(reason.detail);
                });
              }}
            >
              {isRunning ? "执行中…" : runLabel}
            </button>
          )}
          {!isTerminalDevState(task.state) && (
            <details className="task-more">
              <summary className="btn">更多</summary>
              <div className="task-more-menu">
                <button
                  className="task-danger-action"
                  onClick={() => {
                    if (confirm(`取消 ${task.id}?工作区会被清理。`)) {
                      api.cancel(task.id).then(() => { reload(); onChanged(); });
                    }
                  }}
                >取消任务</button>
              </div>
            </details>
          )}
          <button className="task-detail-close" aria-label="关闭任务详情" onClick={onClose}>×</button>
        </div>
      </header>

      {retryOpen && (
        <section className="task-intervention-retry" aria-label="修改需求并创建后继尝试">
          <label htmlFor="intervention-requirement"><b>修改后的需求</b><span className="hint">新任务会读取这份文本；旧任务记录保持不变</span></label>
          <textarea id="intervention-requirement" aria-label="修改后的需求" value={retryText} onChange={(event) => setRetryText(event.target.value)} />
          <div className="task-intervention-retry-actions">
            <button
              className="btn pri"
              disabled={retrying || !retryText.trim()}
              onClick={() => {
                setRetrying(true);
                setActionError("");
                api.retryTaskIntervention(task.id, retryText.trim())
                  .then((successor) => {
                    onChanged();
                    onOpenTask(successor.id);
                  })
                  .catch((reason: ApiError) => {
                    setRetrying(false);
                    setActionError(reason.detail);
                  });
              }}
            >{retrying ? "创建中…" : "创建新尝试"}</button>
            <button className="btn" disabled={retrying} onClick={() => setRetryOpen(false)}>取消</button>
          </div>
        </section>
      )}

      {runError && <Note tone="warn">推进失败:{runError}</Note>}
      {actionError && <Note tone="warn">处理失败:{actionError}</Note>}

      <div className="task-detail-tabs" role="tablist" aria-label="任务详情内容">
        {tabs.map(([value, label]) => (
          <button key={value} role="tab" aria-selected={tab === value} className={tab === value ? "on" : ""} onClick={() => setTab(value)}>{label}</button>
        ))}
      </div>

      <div className="task-detail-content">
        {tab === "overview" && <TaskOverview task={task} running={isRunning} logs={logs} logsError={logsError} pendingTodo={pendingTodo} todoError={todoError} requirementState={requirementState} parentRequirementId={parentRequirementId} onOpenRequirement={onOpenRequirement} onChanged={() => { reload(); onChanged(); }} />}
        {tab === "execution" && <TaskTrace id={id} state={task.state} running={isRunning} />}
        {tab === "artifacts" && <TaskArtifacts id={id} />}
        {tab === "events" && <LifecycleEvents logs={logs} error={logsError} />}
      </div>
    </div>
  );
}

function TaskOverview({ task, running, logs, logsError, pendingTodo, todoError, requirementState, parentRequirementId, onOpenRequirement, onChanged }: { task: TaskDetail; running: boolean; logs: LogPage | null; logsError: string; pendingTodo: TodoDetail | null; todoError: string; requirementState: string; parentRequirementId?: string; onOpenRequirement?: (id: string) => void; onChanged: () => void }) {
  const parsed = (logs?.items ?? []).map(parseEvent).filter((event): event is TaskEvent => event !== null).slice(-5).reverse();
  const needsAttention = devNeedsAttention(task.state) && !task.intervention_resolved_at;
  const settled = isTerminalDevState(task.state) && !needsAttention;
  const awaitingSplit = task.pending_todo?.kind === "split";

  return (
    <>
      <TaskJourney logs={logs} error={logsError} task={task} />

      <div className="task-current-card">
        <div className="task-current-main">
          <span className="task-eyebrow">当前状态</span>
          <h2>{awaitingSplit ? "等待拆分确认" : taskDisplayLabel(task.state, running ? "running" : task.execution_status)}</h2>
          <p>{awaitingSplit ? "需求已经分析完成，正在等待你确认、调整或打回拆分提案。" : running ? "自动流程正在持续推进，直到下一个人工闸门或任务结束。" : taskStateSummary(task.state)}</p>
        </div>
        <div className="task-current-metrics">
          <div><span>最近更新</span><b>{relativeTime(task.updated_at)}</b></div>
          <div><span>消耗 Token</span><b>{task.tokens_used.toLocaleString()}</b></div>
          <div><span>{task.state === "CREATED" ? "需求解析重试" : "修复轮次"}</span><b>{task.state === "CREATED" ? task.plan_retries : task.fix_rounds}</b></div>
        </div>
      </div>

      <Note tone={needsAttention || awaitingSplit ? "warn" : undefined}>
        <b>{awaitingSplit ? "需要确认拆分提案" : needsAttention ? "需要人工处理" : "无需人工操作"}</b>
        <span className="task-note-copy">{awaitingSplit ? "确认后系统会创建子需求并继续推进；你也可以先调整方案或带反馈打回。" : needsAttention ? (task.escalate_reason ?? "请检查当前阶段并完成所需决策。") : settled ? "任务已结束，不会再自动推进。" : "系统会在当前步骤结束后自动更新状态。"}</span>
      </Note>

      {todoError && <Note tone="warn">拉不到拆分提案:{todoError}</Note>}
      {awaitingSplit && pendingTodo && <SplitVerdict todo={pendingTodo} onDone={onChanged} />}

      {task.intervention_resolved_at && (
        <Note>
          <b>人工介入已处理</b>
          <span className="task-note-copy">{task.intervention_successor_task_id ? `已创建后继任务 ${task.intervention_successor_task_id}` : "本次异常已人工关闭"}</span>
        </Note>
      )}

      <div className="task-overview-grid">
        <section className="task-panel">
          <h3>最近活动</h3>
          {logsError ? <Note tone="warn">拉不到日志:{logsError}</Note> : parsed.length === 0 ? <Empty>还没有生命周期事件</Empty> : <EventList events={parsed} />}
        </section>
        <section className="task-panel task-facts">
          <h3>任务信息</h3>
          <div><span>分支</span><b className="mono">{task.branch ?? "—"}</b></div>
          <div><span>执行拓扑</span><b>{task.topology || "跟随项目缺省"}</b></div>
          <div><span>集成测试</span><b>{task.needs_itest}</b></div>
          <div><span>优先级</span><b>{priorityLabel(task.priority)}</b></div>
          {task.requirement_id && (
            <div>
              <span>所属需求</span>
              <b><a className="mono" href={`#/requirements/${task.requirement_id}`} onClick={(event) => { event.preventDefault(); if (task.requirement_id) onOpenRequirement?.(task.requirement_id); }}>{task.requirement_id}</a></b>
            </div>
          )}
          {parentRequirementId && (
            <div>
              <span>上级需求</span>
              <b><a className="mono" href={`#/requirements/${parentRequirementId}`} onClick={(event) => { event.preventDefault(); onOpenRequirement?.(parentRequirementId); }}>{parentRequirementId}</a></b>
            </div>
          )}
          {task.state === "ESCALATED" && requirementState === "delivered" && <Note>该需求已由后续尝试交付</Note>}
        </section>
      </div>

      <CritiqueTimeline id={task.id} />
      <TaskGraph id={task.id} />

      <details className="task-requirement">
        <summary>需求原文</summary>
        <div>{task.requirement}</div>
      </details>
    </>
  );
}

function TaskTrace({ id, state, running }: { id: string; state: string; running: boolean }) {
  const [trace, setTrace] = useState<TaskTraceType | null>(null);
  const [error, setError] = useState("");
  const [rawStage, setRawStage] = useState<TaskTraceStage | null>(null);

  const reload = useCallback(() => {
    api.trace(id).then((found) => {
      setTrace(found);
      setError("");
    }).catch((reason: ApiError) => setError(reason.detail));
  }, [id]);

  useEffect(() => {
    reload();
    const unsubscribe = subscribe(reload, id);
    const timer = running ? window.setInterval(reload, TRACE_POLL_MS) : undefined;
    return () => {
      unsubscribe();
      if (timer !== undefined) window.clearInterval(timer);
    };
  }, [id, reload, running]);

  useEffect(() => setRawStage(null), [id]);

  const closeRaw = useCallback(() => setRawStage(null), []);

  if (error) return <Note tone="warn">拉不到执行轨迹:{error}</Note>;
  if (!trace) return <Empty>加载中…</Empty>;
  if (trace.stages.length === 0) return <Empty>还没有 Job 跑过</Empty>;

  const currentStage = taskStage(state);
  const activeIndex = currentStage ? trace.stages.map((stage) => stage.stage).lastIndexOf(currentStage) : -1;
  const stageEntries = trace.stages.map((stage, index) => ({ stage, index }));
  const orderedEntries = activeIndex >= 0 ? [stageEntries[activeIndex]!, ...stageEntries.filter((entry) => entry.index !== activeIndex)] : stageEntries;

  return (
    <>
      <div className="execution-timeline" aria-live="polite">
        {orderedEntries.map(({ stage, index }) => (
          <ExecutionStage
            key={`${stage.stage}-${stage.number}`}
            stage={stage}
            isCurrent={index === activeIndex}
            sameStageRuns={trace.stages.filter((candidate) => candidate.stage === stage.stage).length}
            attempt={trace.stages.slice(0, index + 1).filter((candidate) => candidate.stage === stage.stage).length}
            onOpenRaw={setRawStage}
          />
        ))}
      </div>
      {rawStage && <RawTraceDrawer stage={rawStage} onClose={closeRaw} />}
    </>
  );
}

type ActivityKind = "read" | "search" | "command" | "change" | "error" | "other";
type ActivityItem = { id: number; kind: ActivityKind; label: string };

const ACTIVITY_META: Readonly<Record<ActivityKind, { label: string; short: string }>> = {
  read: { label: "读取文件", short: "读取" },
  search: { label: "搜索代码", short: "搜索" },
  command: { label: "执行命令", short: "命令" },
  change: { label: "修改文件", short: "修改" },
  error: { label: "异常", short: "异常" },
  other: { label: "其他活动", short: "其他" },
};

const METRIC_KINDS: readonly ActivityKind[] = ["read", "search", "command", "change"];
const FILTER_KINDS: readonly ("all" | ActivityKind)[] = ["all", "read", "search", "command", "change", "error", "other"];

function toolName(block: BlockItem): string | null {
  if (block.kind !== "tool-step") return null;
  const name = block.detail?.name;
  return typeof name === "string" && name.trim() ? name.trim() : null;
}

function activityKind(block: BlockItem): ActivityKind | null {
  if (block.kind === "error") return "error";
  if (block.kind === "diff") return "change";
  const name = toolName(block);
  if (!name) return null;
  const value = name.toLowerCase();
  if (/\b(read|read_file|list_directory)\b/.test(value)) return "read";
  if (/\b(grep|glob|search|find|grep_search)\b/.test(value)) return "search";
  if (/\b(write|edit|patch|notebook_edit)\b/.test(value)) return "change";
  if (/\b(bash|shell|command|run_shell_command)\b/.test(value)) return "command";
  return "other";
}

function activitiesFrom(blocks: BlockItem[]): ActivityItem[] {
  return blocks.flatMap((block) => {
    const kind = activityKind(block);
    return kind ? [{ id: block.seq, kind, label: blockSummary(block.kind, block.text) }] : [];
  });
}

function countActivity(activities: ActivityItem[], kind: ActivityKind): number {
  return activities.filter((activity) => activity.kind === kind).length;
}

function stageProgress(isCurrent: boolean, activities: ActivityItem[]): string {
  if (!isCurrent) {
    const errors = countActivity(activities, "error");
    return errors > 0 ? `阶段已结束，记录了 ${errors} 个异常` : "阶段执行记录已保存";
  }
  const latest = activities.at(-1);
  if (!latest) return "正在分析任务上下文";
  if (latest.kind === "error") return latest.label;
  return `正在${ACTIVITY_META[latest.kind].short}：${latest.label}`;
}

function ExecutionStage({ stage, isCurrent, sameStageRuns, attempt, onOpenRaw }: {
  stage: TaskTraceStage;
  isCurrent: boolean;
  sameStageRuns: number;
  attempt: number;
  onOpenRaw: (stage: TaskTraceStage) => void;
}) {
  const activities = useMemo(() => activitiesFrom(stage.blocks), [stage.blocks]);
  const openRaw = useCallback(() => onOpenRaw(stage), [onOpenRaw, stage]);
  const toolCount = stage.blocks.filter((block) => toolName(block) !== null).length;

  return (
    <section className={`execution-stage ${isCurrent ? "current" : "recorded"}`}>
      <div className="execution-stage-rail"><span>{isCurrent ? stage.number : "✓"}</span></div>
      <div className="execution-stage-body">
        <header>
          <div>
            <h3><span>{STAGE_LABEL[stage.stage] ?? stage.stage}</span>{sameStageRuns > 1 && <small>第 {attempt} 次</small>}</h3>
            <p>{toolCount > 0 ? `${toolCount} 次工具调用` : "确定性步骤"}</p>
          </div>
          <Tag tone={isCurrent ? "pri" : "mute"}>{isCurrent ? "执行中" : "已记录"}</Tag>
        </header>
        {stage.blocks.length === 0 ? <Empty>确定性执行,没有对话轨迹</Empty> : (
          <>
            <div className="execution-progress">
              <span>{isCurrent ? "当前进展" : "阶段结果"}</span>
              <b>{stageProgress(isCurrent, activities)}</b>
            </div>
            <div className="execution-metrics" aria-label="阶段活动统计">
              {METRIC_KINDS.map((kind) => (
                <div key={kind}>
                  <ActivityIcon kind={kind} />
                  <span>{ACTIVITY_META[kind].label}</span>
                  <b>{countActivity(activities, kind)}</b>
                </div>
              ))}
            </div>
            <div className="execution-stage-actions">
              <ActivityDetails activities={activities} />
              <button className="execution-link" type="button" onClick={openRaw}>查看原始日志</button>
            </div>
          </>
        )}
      </div>
    </section>
  );
}

function ActivityDetails({ activities }: { activities: ActivityItem[] }) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState<"all" | ActivityKind>("all");
  const toggle = useCallback(() => setOpen((value) => !value), []);
  const visibleKinds = FILTER_KINDS.filter((kind): kind is ActivityKind => kind !== "all" && countActivity(activities, kind) > 0 && (filter === "all" || filter === kind));

  return (
    <div className="execution-activity">
      <button className="execution-link" type="button" aria-expanded={open} onClick={toggle}>查看详细活动</button>
      {open && (
        <div className="execution-activity-panel">
          <div className="execution-activity-filters" role="group" aria-label="筛选详细活动">
            {FILTER_KINDS.filter((kind) => kind === "all" || countActivity(activities, kind) > 0).map((kind) => (
              <ActivityFilter key={kind} kind={kind} selected={filter === kind} onSelect={setFilter} />
            ))}
          </div>
          {visibleKinds.map((kind) => (
            <section className="execution-activity-group" key={kind}>
              <header><ActivityIcon kind={kind} /><b>{ACTIVITY_META[kind].label}</b><span>{countActivity(activities, kind)}</span></header>
              <ul>
                {activities.filter((activity) => activity.kind === kind).map((activity) => <li className="mono" key={activity.id}>{activity.label}</li>)}
              </ul>
            </section>
          ))}
          {activities.length === 0 && <Empty>这一阶段没有可归类的工具活动</Empty>}
        </div>
      )}
    </div>
  );
}

function ActivityFilter({ kind, selected, onSelect }: { kind: "all" | ActivityKind; selected: boolean; onSelect: (kind: "all" | ActivityKind) => void }) {
  const select = useCallback(() => onSelect(kind), [kind, onSelect]);
  return <button type="button" aria-pressed={selected} className={selected ? "on" : ""} onClick={select}>{kind === "all" ? "全部" : ACTIVITY_META[kind].label}</button>;
}

function ActivityIcon({ kind }: { kind: ActivityKind }) {
  const paths: Readonly<Record<ActivityKind, string>> = {
    read: "M4 5.5A2.5 2.5 0 0 1 6.5 3H18v15H6.5A2.5 2.5 0 0 0 4 20.5z M4 5.5v15 M8 7h6",
    search: "M11 18a7 7 0 1 1 0-14 7 7 0 0 1 0 14z M16 16l5 5",
    command: "M5 7l4 5-4 5 M12 17h7",
    change: "M4 20l4.5-1 10-10-3.5-3.5-10 10z M13.5 7l3.5 3.5",
    error: "M12 9v4 M12 17h.01 M10 3.5 2.8 17h18.4L14 3.5a2.2 2.2 0 0 0-4 0z",
    other: "M4 7h16 M4 12h16 M4 17h10",
  };
  return <svg aria-hidden="true" viewBox="0 0 24 24"><path d={paths[kind]} /></svg>;
}

function RawTraceDrawer({ stage, onClose }: { stage: TaskTraceStage; onClose: () => void }) {
  const closeButton = useRef<HTMLButtonElement>(null);
  const title = `${STAGE_LABEL[stage.stage] ?? stage.stage}原始日志`;
  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeButton.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previous;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [onClose]);

  return (
    <div className="execution-drawer-backdrop">
      <aside className="execution-drawer" role="dialog" aria-modal="true" aria-label={title}>
        <header>
          <div><span>诊断视图</span><h2>{title}</h2><p>{stage.blocks.length} 条原始记录</p></div>
          <button ref={closeButton} type="button" aria-label="关闭原始日志" onClick={onClose}>×</button>
        </header>
        <div className="execution-drawer-content">
          {groupBlocks(stage.blocks).map((entry) => Array.isArray(entry)
            ? <BlockRun key={entry[0]?.seq ?? 0} blocks={entry} />
            : <BlockView key={entry.seq} block={entry} />)}
        </div>
      </aside>
    </div>
  );
}

function blockSummary(kind: string, text: string): string {
  if (kind === "tool-step") {
    const firstLine = text.trim().split("\n").find((line) => line.trim())?.trim() || "工具调用";
    if (firstLine.includes("<tool_use_error>")) return "工具调用未执行（展开原始对话查看原因）";
    return firstLine.length > 80 ? `${firstLine.slice(0, 80)}…` : firstLine;
  }
  if (kind === "diff") return "生成代码变更";
  if (kind === "code") return "生成代码或命令";
  if (kind === "error") return "记录执行错误";
  return "生成阶段输出";
}

function TaskArtifacts({ id }: { id: string }) {
  const [items, setItems] = useState<ReadonlyArray<{ path: string; size: number }> | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api.artifacts(id).then((found) => {
      setItems(found.items);
      setError("");
    }).catch((reason: ApiError) => setError(reason.detail));
  }, [id]);

  if (error) return <Note tone="warn">拉不到产物列表:{error}</Note>;
  if (!items) return <Empty>加载中…</Empty>;
  if (items.length === 0) return <Empty>这个任务还没有产物</Empty>;

  return <div className="artifact-list">{items.map((item) => <div className="artifact-row" key={item.path}><span className="artifact-icon" aria-hidden="true">◇</span><div><b>{item.path}</b><span>{formatBytes(item.size)}</span></div></div>)}</div>;
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

type TaskEvent = { readonly timestamp: string; readonly actor: string; readonly summary: string; readonly raw: string };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringField(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  return typeof value === "string" ? value : "";
}

function latestRunFailure(logs: LogPage | null): string {
  const lines = logs?.items ?? [];
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    try {
      const line = lines[index];
      if (!line) continue;
      const parsed: unknown = JSON.parse(line.text);
      if (!isRecord(parsed) || stringField(parsed, "kind") !== "note") continue;
      const payload = isRecord(parsed.payload) ? parsed.payload : {};
      const note = stringField(payload, "note");
      if (note.startsWith("推进失败:")) return note.slice("推进失败:".length).trim();
    } catch {
      // 事件流允许旧版本留下非 JSON 行；它们不是结构化的后台失败事实。
    }
  }
  return "";
}

function parseEvent(line: { text: string }): TaskEvent | null {
  try {
    const parsed: unknown = JSON.parse(line.text);
    if (!isRecord(parsed)) return null;
    const payload = isRecord(parsed.payload) ? parsed.payload : {};
    const kind = stringField(parsed, "kind");
    const stage = stringField(payload, "stage");
    const from = stringField(payload, "from");
    const to = stringField(payload, "to");
    const summaries: Readonly<Record<string, string>> = {
      task_created: "创建任务",
      topology: "选择执行拓扑",
      job_started: `开始执行${STAGE_LABEL[stage] ?? (stage || "当前")}阶段`,
      job_finished: payload.ok === false
        ? `${STAGE_LABEL[stage] ?? (stage || "当前阶段")}失败：${stringField(payload, "failure_detail") || "未产出有效结果"}`
        : `完成${STAGE_LABEL[stage] ?? (stage || "当前")}阶段执行`,
      transition: from && to ? `状态从 ${taskStateLabel(from)} 变为 ${taskStateLabel(to)}` : "任务状态已更新",
    };
    return { timestamp: stringField(parsed, "ts"), actor: stringField(parsed, "actor") || "system", summary: summaries[kind] ?? (kind || "任务事件"), raw: line.text };
  } catch {
    return { timestamp: "", actor: "system", summary: "未结构化日志", raw: line.text };
  }
}

function EventList({ events }: { events: ReadonlyArray<TaskEvent> }) {
  return <div className="event-list">{events.map((event, index) => <div className="event-row" key={`${event.timestamp}-${index}`}><span className="event-dot" aria-hidden="true" /><time>{event.timestamp ? relativeTime(event.timestamp) : "时间未知"}</time><b>{event.summary}</b><span>{event.actor}</span></div>)}</div>;
}

export function LifecycleEvents({ logs, error }: { logs: LogPage | null; error: string }) {
  const [raw, setRaw] = useState(false);
  if (error) return <Note tone="warn">拉不到日志:{error}</Note>;
  if (!logs) return <Empty>加载中…</Empty>;
  const events = logs.items.map(parseEvent).filter((event): event is TaskEvent => event !== null);
  return (
    <div className="task-events">
      <div className="task-events-head"><div><h2>生命周期事件</h2><p>{logs.total} 条 · 默认展示人可读摘要</p></div><button className="btn sm" onClick={() => setRaw((value) => !value)}>{raw ? "隐藏原始 JSON" : "查看原始 JSON"}</button></div>
      {events.length === 0 ? <Empty>还没有生命周期事件</Empty> : raw ? <pre className="log">{events.map((event) => event.raw).join("\n")}</pre> : <EventList events={events} />}
    </div>
  );
}
