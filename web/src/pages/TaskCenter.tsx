/**
 * 任务中心:默认用紧凑列表扫全局,需要状态分布时再切看板;选中任务始终在同屏右侧展开。
 *
 * 研发任务与基因组任务保留各自的状态语义。待确认是健康闸门,已升级人工与人发起的失败
 * 才进入异常统计；系统自发的基因组任务失败已经了结,只留记录。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, api, type GenomeTaskSummary, type TaskSummary } from "../api/client";
import { subscribe } from "../api/live";
import { Empty, Note, Tag, stateTone } from "../ui/kit";
import { GenomeTaskDetail } from "./GenomeTaskDetail";
import { TaskDetailPanel } from "./TaskDetailPanel";
import { devAttention, relativeTime, taskDisplayLabel, taskStateLabel, taskStepLabel } from "./taskPresentation";

const DEV_LANES = ["CREATED", "DEVELOPING", "UNIT_TESTING", "INTEGRATION_TESTING", "READY_TO_COMMIT", "REVIEWING", "MERGING", "ESCALATED"] as const;
const DEV_SETTLED_LANES = ["COMPLETED", "CANCELLED"] as const;
const GENOME_LANES = ["SCANNING", "AWAITING_CONFIRMATION", "DEEP_READ", "SUMMARISING", "FAILED"] as const;
const GENOME_SETTLED_LANES = ["SUBMITTED", "CANCELLED"] as const;

const KIND_LABEL: Readonly<Record<string, string>> = {
  init: "初始化",
  reinit: "重建",
  distill: "蒸馏",
  backfill: "补写",
  deepen: "深化",
  mount: "挂载业务仓",
};

type TaskKindFilter = "all" | "dev" | "genome";
type ViewMode = "list" | "board";

export function TaskCenter({ openId, onOpen, onOpenRequirement }: { openId: string | null; onOpen: (id: string | null) => void; onOpenRequirement?: (id: string) => void }) {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [genome, setGenome] = useState<GenomeTaskSummary[]>([]);
  const [kindFilter, setKindFilter] = useState<TaskKindFilter>("all");
  const [view, setView] = useState<ViewMode>("list");
  const [includeSettled, setIncludeSettled] = useState(false);
  const [error, setError] = useState("");
  const [genomeError, setGenomeError] = useState("");

  const reload = useCallback(() => {
    api.tasks(includeSettled ? { settled: "true" } : {}).then((found) => {
      setTasks(found);
      setError("");
    }).catch((reason: ApiError) => setError(reason.detail));
    api.genomeTasks(includeSettled ? { settled: "true" } : {}).then((found) => {
      setGenome(found.items);
      setGenomeError("");
    }).catch((reason: ApiError) => setGenomeError(reason.detail));
  }, [includeSettled]);

  useEffect(() => {
    reload();
    return subscribe(reload);
  }, [reload]);

  const hasRunningTask = tasks.some((task) => task.execution_status === "running");
  useEffect(() => {
    if (!hasRunningTask) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") reload();
    }, 10_000);
    return () => window.clearInterval(timer);
  }, [hasRunningTask, reload]);

  const metrics = useMemo(() => {
    const attention = tasks.filter((task) => !task.intervention_resolved_at && (devAttention(task.state) === "attention" || devAttention(task.state) === "exception")).length
      + genome.filter((task) => task.state === "AWAITING_CONFIRMATION" || (task.state === "FAILED" && task.origin === "human")).length;
    const running = tasks.filter((task) => task.execution_status === "running").length
      + genome.filter((task) => ["SCANNING", "DEEP_READ", "SUMMARISING"].includes(task.state)).length;
    const waiting = tasks.filter((task) => task.execution_status !== "running" && (task.execution_status === "interrupted" || devAttention(task.state) === "waiting" || devAttention(task.state) === "attention")).length
      + genome.filter((task) => task.state === "AWAITING_CONFIRMATION").length;
    const exception = tasks.filter((task) => !task.intervention_resolved_at && devAttention(task.state) === "exception").length
      + genome.filter((task) => task.state === "FAILED" && task.origin === "human").length;
    return { attention, running, waiting, exception };
  }, [genome, tasks]);

  const openGenome = openId?.startsWith("gn-") ? openId : null;
  if (error) return <Empty>拉不到任务列表:{error}</Empty>;

  return (
    <div className="task-center-page">
      <section className="task-overview" aria-label="任务总览">
        <header className="task-page-title">
          <div><h1>任务中心</h1><div className="sub">追踪任务进度,优先处理等待人工介入的事项</div></div>
        </header>

        <div className="task-metrics" aria-label="任务状态统计">
          <Metric label="需我处理" value={metrics.attention} tone="attention" />
          <Metric label="运行中" value={metrics.running} tone="running" />
          <Metric label="等待中" value={metrics.waiting} tone="waiting" />
          <Metric label="异常" value={metrics.exception} tone="exception" />
        </div>

        <div className="task-toolbar">
          <div className="view-switch" role="group" aria-label="任务视图">
            <button className={view === "list" ? "on" : ""} aria-pressed={view === "list"} onClick={() => setView("list")}>列表视图</button>
            <button className={view === "board" ? "on" : ""} aria-pressed={view === "board"} onClick={() => setView("board")}>看板视图</button>
          </div>
          <div className="filters" role="group" aria-label="按任务类型筛选">
            {([ ["all", "全部"], ["dev", "研发任务"], ["genome", "基因组任务"] ] as const).map(([value, label]) => (
              <button key={value} className={`btn sm ${kindFilter === value ? "pri" : ""}`} aria-pressed={kindFilter === value} onClick={() => setKindFilter(value)}>{label}</button>
            ))}
          </div>
          <label className="settled-toggle">
            <input type="checkbox" checked={includeSettled} onChange={(event) => setIncludeSettled(event.target.checked)} />
            显示已完结任务
          </label>
        </div>

        {genomeError && <Note tone="warn">拉不到基因组任务:{genomeError}</Note>}
        <div className="task-overview-scroll">
          {view === "list"
            ? <TaskList tasks={tasks} genome={genome} kindFilter={kindFilter} openId={openId} onOpen={onOpen} />
            : <TaskBoard tasks={tasks} genome={genome} kindFilter={kindFilter} includeSettled={includeSettled} onOpen={onOpen} />}
        </div>
      </section>

      <section className="task-detail-pane" role="region" aria-label="任务详情">
        {!openId ? (
          <div className="task-detail-empty"><div className="task-detail-empty-mark" aria-hidden="true">⌁</div><b>选择一个任务查看详情</b><span>状态、下一步操作和执行结果会显示在这里</span></div>
        ) : openGenome ? (
          <GenomeTaskDetail id={openGenome} onClose={() => onOpen(null)} onChanged={reload} />
        ) : (
          <TaskDetailPanel id={openId} onClose={() => onOpen(null)} onChanged={reload} onOpenTask={onOpen} onOpenRequirement={onOpenRequirement} />
        )}
      </section>
    </div>
  );
}

function Metric({ label, value, tone }: { label: string; value: number; tone: string }) {
  return <div className={`task-metric ${tone}`}><span>{label}</span><b>{value}</b></div>;
}

function TaskList({ tasks, genome, kindFilter, openId, onOpen }: {
  tasks: ReadonlyArray<TaskSummary>;
  genome: ReadonlyArray<GenomeTaskSummary>;
  kindFilter: TaskKindFilter;
  openId: string | null;
  onOpen: (id: string | null) => void;
}) {
  const showDev = kindFilter !== "genome";
  const showGenome = kindFilter !== "dev";
  const empty = (!showDev || tasks.length === 0) && (!showGenome || genome.length === 0);
  if (empty) return <Empty>当前筛选下没有任务</Empty>;

  return (
    <div className="task-list">
      <div className="task-list-head" aria-hidden="true"><span>任务</span><span>当前阶段</span><span>最近活动</span><span>状态</span></div>
      {showDev && tasks.map((task) => (
        <button type="button" className={`task-list-row ${openId === task.id ? "selected" : ""}`} key={task.id} aria-current={openId === task.id ? "true" : undefined} onClick={() => onOpen(task.id)}>
          <span className="task-list-title"><b>{task.title}</b><small className="mono">{task.id}</small></span>
          <span>{taskStepLabel(task.state)}</span>
          <span>{relativeTime(task.updated_at)}</span>
          <Tag tone={stateTone(task.state)}>{taskDisplayLabel(task.state, task.execution_status)}</Tag>
        </button>
      ))}
      {showGenome && genome.map((task) => (
        <div className={`task-list-row genome ${openId === task.id ? "selected" : ""}`} key={task.id}>
          <button type="button" className="task-list-row-main" aria-current={openId === task.id ? "true" : undefined} onClick={() => onOpen(task.id)}>
            <span className="task-list-title"><b>{task.title}</b><small className="mono">{task.id}</small><small>{KIND_LABEL[task.kind] ?? task.kind}</small><small>{task.subject ? `模块 ${task.subject}` : "全量"}</small></span>
            <span>{taskStepLabel(task.state)}</span>
            <span>{relativeTime(task.updated_at)}</span>
            <Tag tone={task.state === "FAILED" && task.origin === "human" ? "bad" : task.state === "AWAITING_CONFIRMATION" ? "pur" : task.state === "SUBMITTED" ? "ok" : task.state === "FAILED" || task.state === "CANCELLED" ? "mute" : "pri"}>{taskStateLabel(task.state)}</Tag>
          </button>
          {task.source_task_id && <button className="task-source-link" onClick={() => onOpen(task.source_task_id ?? null)}>来源任务 {task.source_task_id}</button>}
        </div>
      ))}
    </div>
  );
}

function TaskBoard({ tasks, genome, kindFilter, includeSettled, onOpen }: {
  tasks: ReadonlyArray<TaskSummary>;
  genome: ReadonlyArray<GenomeTaskSummary>;
  kindFilter: TaskKindFilter;
  includeSettled: boolean;
  onOpen: (id: string | null) => void;
}) {
  const devLanes = includeSettled ? [...DEV_LANES, ...DEV_SETTLED_LANES] : DEV_LANES;
  const genomeLanes = includeSettled ? [...GENOME_LANES, ...GENOME_SETTLED_LANES] : GENOME_LANES;
  return (
    <div className="task-board-stack">
      {kindFilter !== "genome" && (
        <div className="lanes">
          {devLanes.map((state) => <Lane key={state} state={state} count={tasks.filter((task) => task.state === state).length}>
            {tasks.filter((task) => task.state === state).map((task) => (
              <button type="button" className="tk" key={task.id} onClick={() => onOpen(task.id)}>
                <span className="r1"><span className="mono">{task.id}</span></span><b>{task.title}</b><span className="m">{task.branch ?? "(还没建分支)"}</span>
                {task.risk_level && <Tag tone={task.risk_level === "high" ? "bad" : "ok"}>{task.risk_level}</Tag>}
              </button>
            ))}
          </Lane>)}
        </div>
      )}
      {kindFilter !== "dev" && (
        <>
          {kindFilter === "all" && <h2 className="sub task-board-heading">基因组任务 —— 系统在改的是认知,不是代码</h2>}
          <div className="lanes">
            {genomeLanes.map((state) => <Lane key={state} state={state} count={genome.filter((task) => task.state === state).length} exceptional={state === "FAILED" && genome.some((task) => task.state === state && task.origin === "human")}>
              {genome.filter((task) => task.state === state).map((task) => (
                <div className={`tk gn ${task.state === "FAILED" && task.origin === "system" ? "settled" : ""}`} key={task.id}>
                  <button type="button" className="tk-main" onClick={() => onOpen(task.id)}>
                    <span className="r1"><span className="mono">{task.id}</span><Tag tone="pur">{KIND_LABEL[task.kind] ?? task.kind}</Tag></span>
                    <b>{task.title}</b><span className="m">{task.subject ? `模块 ${task.subject}` : "全量"}</span>
                    {task.state === "AWAITING_CONFIRMATION" && <Tag tone={task.overdue ? "warn" : "pur"}>{task.overdue ? "等太久了 · 去确认" : "去确认"}</Tag>}
                    {task.state === "FAILED" && task.origin === "human" && <Tag tone="bad">去诊断</Tag>}
                  </button>
                  {task.source_task_id && <button className="task-source-link" onClick={() => onOpen(task.source_task_id ?? null)}>来源任务 {task.source_task_id}</button>}
                </div>
              ))}
            </Lane>)}
          </div>
        </>
      )}
    </div>
  );
}

function Lane({ state, count, children, exceptional = state === "ESCALATED" }: { state: string; count: number; children: React.ReactNode; exceptional?: boolean }) {
  return <div className={`lane ${exceptional ? "term" : ""} ${state === "AWAITING_CONFIRMATION" ? "await" : ""}`}><h4>{state} <b>{count}</b></h4>{count === 0 ? <Empty>空</Empty> : children}</div>;
}
