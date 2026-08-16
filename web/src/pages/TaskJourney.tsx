/**
 * 任务实际经过的状态,以及每一段**是谁动的手**。
 *
 * ## 为什么不按员工分泳道
 *
 * 「现在处于什么状态」是个只有一个答案的问题,横向条带扫到最后一格就是答案;泳道要在 N 行
 * 里找最后一个活跃格。而且多数任务里状态与员工近似 1:1(开发→开发员工,单测→门禁),泳道会
 * 退化成一条对角线——行高翻 N 倍,信息量不变。「谁干的」这一维在下面的最近活动里本来就有。
 *
 * 真正丢掉的信息不是「谁」,是**一格里折叠了几个执行者**:开发那一格里是员工跑 Job、编排器
 * 判迁移;单测那一格里是门禁跑、编排器判返工。所以这里补的是每格的执行者行,不是换布局。
 *
 * 泳道要等一个状态里**同时**有多个执行者(并行 DAG 子任务、批判环、best-of-n)才值钱,而且
 * 那个视图属于「执行过程」而不是「概览」:概览答"到哪了",执行过程答"里面怎么跑的"。
 *
 * ## 段与事件的对应靠顺序,不靠键匹配
 *
 * 事件是追加写的,所以两条 `transition` 之间发生的事就是前一段状态里发生的事。按
 * `stage + round` 去配的话,`job_finished` 的 payload 里根本没有这两个字段,得从
 * `job_started` 借——而借错一次的表现是某一格的成本挂到了隔壁格上,看不出来。
 */
import type { LogPage, TaskDetail } from "../api/client";
import { Empty, Note } from "../ui/kit";

type JourneyRole = "decision" | "coding" | "testing" | "review" | "delivery" | "system";
type JourneyStatus = "passed" | "rework" | "active" | "waiting" | "stopped";
type ActorKind = "employee" | "gate" | "human" | "orchestrator" | "integration";

/** 一段状态里某个行为主体干了什么、烧了多少。 */
export interface JourneyActor {
  readonly name: string;
  readonly kind: ActorKind;
  /** 这一段里它被派了几次。并行节点与批判环会让同一个员工在一段里跑多次。 */
  readonly runs: number;
  /** 还有没收尾的派发——这一格就是此刻正在跑的那一格。 */
  readonly running: boolean;
  /** 累计 token。拿不到用量时是 0,不显示,**不当成零成本**。 */
  readonly tokens: number;
  readonly durationS: number;
  readonly failed: boolean;
}

export interface JourneyStep {
  readonly id: string;
  readonly state: string;
  readonly label: string;
  readonly attempt: number;
  readonly role: JourneyRole;
  readonly status: JourneyStatus;
  readonly statusLabel: string;
  readonly timestamp: string;
  readonly detail: string;
  readonly actors: ReadonlyArray<JourneyActor>;
}

interface LogEntry {
  readonly ts: string;
  readonly actor: string;
  readonly actorKind: ActorKind;
  readonly kind: string;
  readonly payload: Record<string, unknown>;
}

interface TransitionEvent {
  readonly timestamp: string;
  readonly from: string;
  readonly to: string;
  readonly event: string;
  readonly reason: string;
  readonly actor: string;
  readonly actorKind: ActorKind;
}

const STATE_LABEL: Readonly<Record<string, string>> = {
  CREATED: "需求解析",
  DEVELOPING: "开发",
  UNIT_TESTING: "单元测试",
  INTEGRATION_TESTING: "集成测试",
  READY_TO_COMMIT: "提交前检查",
  REVIEWING: "审查",
  MERGING: "合并",
  COMPLETED: "已完成",
  ESCALATED: "人工接管",
  CANCELLED: "已取消",
};

const STATE_ROLE: Readonly<Record<string, JourneyRole>> = {
  CREATED: "decision",
  DEVELOPING: "coding",
  UNIT_TESTING: "testing",
  INTEGRATION_TESTING: "testing",
  READY_TO_COMMIT: "delivery",
  REVIEWING: "review",
  MERGING: "delivery",
  COMPLETED: "system",
  ESCALATED: "system",
  CANCELLED: "system",
};

const ROLE_LABEL: Readonly<Record<JourneyRole, string>> = {
  decision: "决策",
  coding: "开发",
  testing: "测试",
  review: "审查",
  delivery: "交付",
  system: "系统",
};

const ACTOR_KIND_LABEL: Readonly<Record<ActorKind, string>> = {
  employee: "数字员工",
  gate: "门禁",
  human: "人工",
  orchestrator: "编排器",
  integration: "集成入口",
};

const REWORK_EVENTS = new Set([
  "plan_failed",
  "gate_fail",
  "itest_fail",
  "precheck_fail",
  "reject",
  "merge_conflict",
  "scope_widened",
]);

//: 与后端 `core/events.py` 里的保留名同源。名字对不上的后果是员工被算成人,而那正是
//: 「自动化率」这个指标唯一的分母。
const ORCHESTRATOR = "orchestrator";
const GATE_ACTOR = "gate-runner";
const INTEGRATION_ACTORS = new Set(["alert", "im"]);
const EMPLOYEE_KINDS = new Set(["job_started", "job_finished"]);
const ACTOR_KINDS = new Set<string>([
  "employee",
  "gate",
  "human",
  "orchestrator",
  "integration",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringField(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  return typeof value === "string" ? value : "";
}

function numberField(record: Record<string, unknown>, key: string): number {
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

/**
 * 没有显式声明时,这个 actor 算哪一类。**与后端 `infer_actor_kind` 是同一条规则**——
 * 认不出来的一律算人:把人记成机器,追责时那条线索直接断掉;把机器记成人只是多看一眼。
 */
function inferActorKind(actor: string, kind: string): ActorKind {
  if (actor === ORCHESTRATOR) return "orchestrator";
  if (actor === GATE_ACTOR) return "gate";
  if (INTEGRATION_ACTORS.has(actor)) return "integration";
  if (EMPLOYEE_KINDS.has(kind)) return "employee";
  return "human";
}

function entryFrom(line: { readonly text: string }): LogEntry | null {
  try {
    const parsed: unknown = JSON.parse(line.text);
    if (!isRecord(parsed)) return null;
    const kind = stringField(parsed, "kind");
    if (!kind) return null;
    const actor = stringField(parsed, "actor");
    // `actor_kind` 是后加的一列,老事件没有——那时按名字与事件类型反推,推断规则与后端同源。
    const declared = stringField(parsed, "actor_kind");
    return {
      ts: stringField(parsed, "ts"),
      actor,
      actorKind: ACTOR_KINDS.has(declared)
        ? (declared as ActorKind)
        : inferActorKind(actor, kind),
      kind,
      payload: isRecord(parsed.payload) ? parsed.payload : {},
    };
  } catch {
    return null;
  }
}

function transitionOf(entry: LogEntry): TransitionEvent | null {
  if (entry.kind !== "transition") return null;
  const from = stringField(entry.payload, "from");
  const to = stringField(entry.payload, "to");
  if (!from || !to) return null;
  return {
    timestamp: entry.ts,
    from,
    to,
    event: stringField(entry.payload, "event"),
    reason: stringField(entry.payload, "reason"),
    actor: entry.actor,
    actorKind: entry.actorKind,
  };
}

interface ActorTally {
  name: string;
  kind: ActorKind;
  runs: number;
  open: number;
  tokens: number;
  durationS: number;
  failed: boolean;
}

/** 一段状态里实际动过手的是谁,按第一次出现的顺序排。 */
function actorsOf(entries: ReadonlyArray<LogEntry>): JourneyActor[] {
  const tally = new Map<string, ActorTally>();
  const touch = (entry: LogEntry): ActorTally => {
    const found = tally.get(entry.actor);
    if (found) return found;
    const fresh: ActorTally = {
      name: entry.actor,
      kind: entry.actorKind,
      runs: 0,
      open: 0,
      tokens: 0,
      durationS: 0,
      failed: false,
    };
    tally.set(entry.actor, fresh);
    return fresh;
  };

  for (const entry of entries) {
    if (entry.kind === "job_started") {
      const actor = touch(entry);
      actor.runs += 1;
      actor.open += 1;
    } else if (entry.kind === "job_finished") {
      const actor = touch(entry);
      // 配不到开头的收尾(翻页把前面截掉了)照样算一次派发——不算的话这一格会显示成没人干过,
      // 而"没人干过"与"开头没拉到"是两件完全不同的事。
      if (actor.open > 0) actor.open -= 1;
      else actor.runs += 1;
      actor.tokens += numberField(entry.payload, "tokens_used");
      actor.durationS += numberField(entry.payload, "duration_s");
      if (entry.payload.ok === false) actor.failed = true;
    } else if (entry.kind === "gate_result") {
      const actor = touch(entry);
      actor.runs += 1;
      if (entry.payload.passed === false) actor.failed = true;
    } else if (entry.kind === "approval") {
      const actor = touch(entry);
      actor.runs += 1;
      if (entry.payload.approved === false) actor.failed = true;
    }
  }

  return [...tally.values()].map((actor) => ({
    name: actor.name,
    kind: actor.kind,
    runs: actor.runs,
    running: actor.open > 0,
    tokens: actor.tokens,
    durationS: actor.durationS,
    failed: actor.failed,
  }));
}

/**
 * 判这一步的是谁。缺省是编排器,不单列——它推了绝大多数迁移,每格都标一遍等于没标。
 * **人介入的那几次必须显形**:取消一个跑飞的任务、驳回一次评审,那是全部的人工干预。
 */
function withDecider(
  actors: JourneyActor[],
  event: TransitionEvent | null,
): JourneyActor[] {
  if (!event || event.actorKind !== "human" || !event.actor) return actors;
  if (actors.some((actor) => actor.name === event.actor)) return actors;
  return [
    ...actors,
    { name: event.actor, kind: "human", runs: 0, running: false, tokens: 0, durationS: 0, failed: false },
  ];
}

function completedStatus(event: TransitionEvent): Pick<JourneyStep, "status" | "statusLabel"> {
  if (event.to === "ESCALATED") return { status: "stopped", statusLabel: "转人工" };
  if (event.to === "CANCELLED") return { status: "stopped", statusLabel: "已取消" };
  if (REWORK_EVENTS.has(event.event)) return { status: "rework", statusLabel: "返工" };
  const labels: Readonly<Record<string, string>> = {
    plan_done: "已解析",
    dev_done: "已产出",
    gate_pass: "通过",
    itest_pass: "通过",
    risk_low: "检查完成",
    risk_high: "待审查",
    approve: "已批准",
    merged: "已合并",
  };
  return { status: "passed", statusLabel: labels[event.event] ?? "已流转" };
}

function currentStatus(
  state: string,
  executionStatus: string,
): Pick<JourneyStep, "status" | "statusLabel"> {
  if (state === "COMPLETED") return { status: "passed", statusLabel: "已完成" };
  if (state === "ESCALATED") return { status: "stopped", statusLabel: "等人工" };
  if (state === "CANCELLED") return { status: "stopped", statusLabel: "已取消" };
  if (executionStatus === "running") return { status: "active", statusLabel: "执行中" };
  return { status: "waiting", statusLabel: "等待继续" };
}

function nextAttempt(counts: Map<string, number>, state: string): number {
  const attempt = (counts.get(state) ?? 0) + 1;
  counts.set(state, attempt);
  return attempt;
}

export function buildTaskJourney(
  items: LogPage["items"],
  currentState: string,
  executionStatus: string,
): ReadonlyArray<JourneyStep> {
  const entries = items.map(entryFrom).filter((entry): entry is LogEntry => entry !== null);
  const counts = new Map<string, number>();
  const steps: JourneyStep[] = [];
  let bucket: LogEntry[] = [];
  let index = 0;

  for (const entry of entries) {
    const event = transitionOf(entry);
    if (!event) {
      bucket.push(entry);
      continue;
    }
    const attempt = nextAttempt(counts, event.from);
    const outcome = completedStatus(event);
    steps.push({
      id: `${event.timestamp || index}-${event.event}-${attempt}`,
      state: event.from,
      label: STATE_LABEL[event.from] ?? event.from,
      attempt,
      role: STATE_ROLE[event.from] ?? "system",
      ...outcome,
      timestamp: event.timestamp,
      detail:
        event.reason ||
        `${STATE_LABEL[event.from] ?? event.from} → ${STATE_LABEL[event.to] ?? event.to}`,
      actors: withDecider(actorsOf(bucket), event),
    });
    bucket = [];
    index += 1;
  }

  const attempt = nextAttempt(counts, currentState);
  const outcome = currentStatus(currentState, executionStatus);
  steps.push({
    id: `current-${currentState}-${attempt}`,
    state: currentState,
    label: STATE_LABEL[currentState] ?? currentState,
    attempt,
    role: STATE_ROLE[currentState] ?? "system",
    ...outcome,
    timestamp: "",
    detail: "当前治理状态",
    // 最后一段用的是收尾之后剩下的事件——**没配到 `job_finished` 的那次派发就在这里**,
    // 它就是此刻正在跑的那一个。「现在到底谁在干活」只有这一格答得出来。
    actors: actorsOf(bucket),
  });
  return steps;
}

function formatTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${Math.round(value / 1_000)}k`;
  return String(value);
}

function formatDuration(seconds: number): string {
  const total = Math.round(seconds);
  if (total < 60) return `${total} 秒`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes} 分`;
  return `${Math.floor(minutes / 60)} 时 ${minutes % 60} 分`;
}

/** 执行者名字右边那串成本。**拿不到的项直接不写**——写 0 会被读成"没花钱"。 */
export function actorCost(actor: JourneyActor): string {
  const bits: string[] = [];
  if (actor.running) bits.push("执行中");
  if (actor.runs > 1) bits.push(`${actor.runs} 次`);
  if (actor.tokens > 0) bits.push(`${formatTokens(actor.tokens)} token`);
  if (actor.durationS > 0) bits.push(formatDuration(actor.durationS));
  return bits.join(" · ");
}

function actorSummary(actor: JourneyActor): string {
  const cost = actorCost(actor);
  return `${ACTOR_KIND_LABEL[actor.kind]} ${actor.name}${cost ? ` · ${cost}` : ""}`;
}

function ariaLabel(step: JourneyStep): string {
  const attempt = step.attempt > 1 ? `，第 ${step.attempt} 次` : "";
  const actors = step.actors.length
    ? `，执行者 ${step.actors.map(actorSummary).join("、")}`
    : "";
  return `${step.label}${attempt}，${ROLE_LABEL[step.role]}，${step.statusLabel}${actors}，${step.detail}`;
}

export function TaskJourney({
  logs,
  error,
  task,
}: {
  readonly logs: LogPage | null;
  readonly error: string;
  readonly task: Pick<TaskDetail, "state" | "execution_status">;
}) {
  if (error) return <Note tone="warn">拉不到实际轨迹:{error}</Note>;
  if (!logs) return <Empty>正在还原实际轨迹…</Empty>;

  const steps = buildTaskJourney(logs.items, task.state, task.execution_status);
  const roles = Array.from(new Set(steps.map((step) => step.role)));
  // 日志接口从头翻页,截断掉的是**后面**那些事件。说成"最近一段"会让人以为开头缺了,
  // 于是照着一条实际上不完整的中段去数返工次数。
  const clipped = logs.total > logs.items.length;

  return (
    <section className="task-journey" aria-labelledby="task-journey-title">
      <header className="task-journey-head">
        <div>
          <span className="task-eyebrow">事件还原 · 仅展示实际发生</span>
          <h3 id="task-journey-title">实际轨迹</h3>
        </div>
        <span className="task-journey-count">{steps.length} 个状态片段{clipped ? " · 开头一段" : ""}</span>
      </header>

      <ol className="task-journey-track" aria-label="任务实际经过的状态">
        {steps.map((step, index) => (
          <li className={`task-journey-step ${step.role} ${step.status}`} key={step.id}>
            <div className="task-journey-segment" tabIndex={0} aria-label={ariaLabel(step)} title={step.detail}>
              <div className="task-journey-line">
                <span className="task-journey-order" aria-hidden="true">{index + 1}</span>
                <span className="task-journey-copy">
                  <b>{step.label}</b>
                  <small>{step.attempt > 1 ? `第 ${step.attempt} 次` : ROLE_LABEL[step.role]}</small>
                </span>
                <span className="task-journey-outcome">{step.status === "rework" ? "↺ " : ""}{step.statusLabel}</span>
              </div>
              {step.actors.length > 0 && (
                <div className="task-journey-actors">
                  {step.actors.map((actor) => {
                    const cost = actorCost(actor);
                    const tone = `${actor.kind}${actor.running ? " running" : ""}${actor.failed ? " failed" : ""}`;
                    return (
                      <span className={`task-journey-actor ${tone}`} key={actor.name}>
                        <b>{actor.name}</b>
                        {cost && <em>{cost}</em>}
                      </span>
                    );
                  })}
                </div>
              )}
            </div>
          </li>
        ))}
      </ol>

      <div className="task-journey-legend" aria-label="轨迹角色图例">
        {roles.map((role) => <span className={role} key={role}><i aria-hidden="true" />{ROLE_LABEL[role]}</span>)}
      </div>
      {clipped && <p className="task-journey-note">事件较多,当前轨迹只还原了最早的 {logs.items.length} 条事件,后面的片段没有计入。</p>}
    </section>
  );
}
