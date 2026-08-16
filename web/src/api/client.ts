/**
 * REST 客户端。
 *
 * 类型来自 `schema.d.ts`,而那份文件由 `npm run gen` 从后端导出的 OpenAPI 生成。
 * **不手写类型**——手写的话后端改一个字段名前端不会报错,只会在运行时拿到 undefined。
 */
import type { components } from "./schema";

export type TaskSummary = components["schemas"]["TaskSummary"];
export type TaskDetail = components["schemas"]["TaskDetail"];
export type EventPage = components["schemas"]["EventPage"];
export type LogPage = components["schemas"]["LogPage"];
export type ArtifactList = components["schemas"]["ArtifactList"];
export type ReportResponse = components["schemas"]["ReportResponse"];
export type TaskTrace = components["schemas"]["TaskTrace"];
export type TaskTraceStage = components["schemas"]["TaskTraceStage"];
export type SubmitRequest = components["schemas"]["SubmitRequest"];
export type ApprovalRequest = components["schemas"]["ApprovalRequest"];
export type LineComment = components["schemas"]["LineComment"];
export type ApprovalPreview = components["schemas"]["ApprovalPreview"];

export type ProjectMapResponse = components["schemas"]["ProjectMapResponse"];
export type ProjectMapVersionList = components["schemas"]["ProjectMapVersionList"];
export type ProjectMapVersionItem = components["schemas"]["ProjectMapVersionItem"];
export type ProjectMapDiffResponse = components["schemas"]["ProjectMapDiffResponse"];
export type LessonList = components["schemas"]["LessonList"];
export type LessonCardResponse = components["schemas"]["LessonCardResponse"];
export type LessonCreateRequest = components["schemas"]["LessonCreateRequest"];
export type RuleSetResponse = components["schemas"]["RuleSetResponse"];
export type RuleProposalRequest = components["schemas"]["RuleProposalRequest"];
export type RuleProposalResponse = components["schemas"]["RuleProposalResponse"];
export type ProcedureStatsList = components["schemas"]["ProcedureStatsList"];
export type GenomeTaskSummary = components["schemas"]["GenomeTaskSummary"];
export type GenomeTaskList = components["schemas"]["GenomeTaskList"];
export type GateDraft = components["schemas"]["GateDraft"];
export type TodoItem = components["schemas"]["TodoItem"];
export type TodoDetail = components["schemas"]["TodoDetail"];
export type TodoList = components["schemas"]["TodoList"];
export type TodoSubmitResponse = components["schemas"]["TodoSubmitResponse"];
export type GateAnswer = components["schemas"]["GateAnswer"];
export type GateResult = components["schemas"]["GateResult"];
export type BoundaryModule = components["schemas"]["BoundaryModule"];
export type GenomeTaskProgress = components["schemas"]["GenomeTaskProgress"];
export type KnowledgeStatus = components["schemas"]["KnowledgeStatus"];
export type WorkspaceList = components["schemas"]["WorkspaceList"];
export type WorkspaceEntry = components["schemas"]["WorkspaceEntry"];
export type WorkspaceCreateRequest = components["schemas"]["WorkspaceCreateRequest"];
export type WorkspaceCreated = components["schemas"]["WorkspaceCreated"];

export type TrendReport = components["schemas"]["TrendReport"];
export type CostReport = components["schemas"]["CostReport"];
export type RosterReport = components["schemas"]["RosterReport"];
export type SettingsView = components["schemas"]["SettingsView"];
export type SettingsChange = components["schemas"]["SettingsChange"];
export type ReadinessView = components["schemas"]["ReadinessView"];
export type RuntimeChoiceView = components["schemas"]["RuntimeChoiceView"];
export type WorkerStatusView = components["schemas"]["WorkerStatusView"];
export type WorkerStatusListView = components["schemas"]["WorkerStatusListView"];
export type WorkerPlanView = components["schemas"]["WorkerPlanView"];
export type WorkerProvisionResult = components["schemas"]["WorkerProvisionResult"];
export type WorkerLifecycleResult = components["schemas"]["WorkerLifecycleResult"];
export type TopologyCatalog = components["schemas"]["TopologyCatalog"];
export type TopologyOption = components["schemas"]["TopologyOption"];
export type AuditEventPage = components["schemas"]["AuditEventPage"];

export type RequirementSummary = components["schemas"]["RequirementSummary"];
export type RequirementDetail = components["schemas"]["RequirementDetail"];
export type RequirementPatch = components["schemas"]["RequirementPatch"];
export type AttemptView = components["schemas"]["AttemptView"];

export type ImportRequest = components["schemas"]["ImportRequest"];
export type ImportResult = components["schemas"]["ImportResult"];
export type NotificationPreference = components["schemas"]["NotificationPreference"];
export type NotificationPreferenceList = components["schemas"]["NotificationPreferenceList"];

export type SessionSummary = components["schemas"]["SessionSummary"];
export type SessionList = components["schemas"]["SessionList"];
export type SessionCreateRequest = components["schemas"]["SessionCreateRequest"];
export type EmployeeSummary = components["schemas"]["EmployeeSummary"];
export type EmployeeList = components["schemas"]["EmployeeList"];
export type BlockItem = components["schemas"]["BlockItem"];
export type BlockPage = components["schemas"]["BlockPage"];
export type TaskDraft = components["schemas"]["TaskDraft"];
export type InjectRequest = components["schemas"]["InjectRequest"];
export type FeedbackResponse = components["schemas"]["FeedbackResponse"];

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(detail);
  }
}

const base = import.meta.env.VITE_API_BASE ?? "";

// --- 当前项目 ----------------------------------------------------------------
//
// **参数注入只发生在这一层**(PRD 44 的验收线):页面代码不感知当前项目,正如 mock 只
// 属于这个边界(ADR-0001)。多项目实例上服务端对"没说要哪个项目"默认拒绝,这里漏注入
// 的表现是当场 4xx,不是悄悄读到别的项目——那道闸在服务端,这里只是把名字带上。

const WORKSPACE_KEY = "agentgenome.workspace";

let currentWorkspace = ((): string => {
  try {
    return localStorage.getItem(WORKSPACE_KEY) ?? "";
  } catch {
    return "";
  }
})();

export function setWorkspace(name: string): void {
  currentWorkspace = name;
  try {
    if (name) localStorage.setItem(WORKSPACE_KEY, name);
    else localStorage.removeItem(WORKSPACE_KEY);
  } catch {
    // localStorage 不可用(隐私模式)时只活在内存里——刷新丢选择,不丢功能。
  }
}

export function getWorkspace(): string {
  return currentWorkspace;
}

function workspaceHeaders(): Record<string, string> {
  return currentWorkspace ? { "x-workspace": currentWorkspace } : {};
}

/** 一个 `data: {...}` SSE 事件解析成一块。不是 `data:` 行(比如心跳/空行)就返回 null。 */
function parseSseBlock(chunk: string): BlockItem | null {
  const line = chunk.split("\n").find((l) => l.startsWith("data: "));
  return line ? (JSON.parse(line.slice("data: ".length)) as BlockItem) : null;
}

/**
 * 边收边解析一个块流的 SSE 响应体,每解出一块就回调一次。
 *
 * **`sendSessionMessage` 与 `attachSessionStream` 共用它。** 两条路在后端都是接上
 * `SessionService.attach`,协议完全一样——发消息只是"顺便起了一轮"的那次接。
 */
async function readBlockStream(response: Response, onBlock: (block: BlockItem) => void): Promise<void> {
  const reader = response.body?.getReader();
  if (!reader) {
    // 拿不到可读流(比如测试环境的 fetch polyfill)时退化成读完一次性解析——正确性
    // 不受影响,只是没有"边到边"的体验。
    for (const chunk of (await response.text()).split("\n\n")) {
      const block = parseSseBlock(chunk);
      if (block) onBlock(block);
    }
    return;
  }
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";
    for (const chunk of chunks) {
      const block = parseSseBlock(chunk);
      if (block) onBlock(block);
    }
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...workspaceHeaders(),
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    // 后端的 detail 原样带上来。改写成"操作失败"会把唯一能指导用户的那句话丢掉——
    // 比如"驳回必须附意见"或者"你不在审批人名单里"。
    const body = (await response.json().catch(() => ({}))) as { detail?: string };
    // 唯一的例外:多项目实例上**这边没选项目**导致的默认拒绝。后端那句说的是
    // "请求必须说明要哪一个",而用户能做的动作是"去顶栏选"——补上去处,原话跟在后面。
    if (
      !currentWorkspace &&
      (response.status === 400 || response.status === 404) &&
      body.detail?.includes("工作区")
    ) {
      throw new ApiError(response.status, `未选择项目 —— 在顶栏选一个项目。(${body.detail})`);
    }
    throw new ApiError(response.status, body.detail ?? `HTTP ${response.status}`);
  }
  return (await parse<T>(response, path));
}

/**
 * 200 不等于拿到了 JSON。
 *
 * **开发代理漏配一条路由时,请求会落到 dev server 上,而它对任何未知路径都回 200 +
 * `index.html`。** 于是 `response.ok` 为真、`response.json()` 抛一个 `SyntaxError`,而全站
 * 那些 `.catch((e: ApiError) => setError(e.detail))` 拿到的 `detail` 是 `undefined`——
 * 错误框判空不显示,页面就只是**安静地空着**。
 *
 * 真实发生过:`/employees` 没进代理白名单,新建会话的员工下拉是空的,没有任何报错,
 * 看起来像"一个员工都没有"。**拿到 HTML 却当成数据,必须是一句响亮的话。**
 */
async function parse<T>(response: Response, path: string): Promise<T> {
  const text = await response.text();
  try {
    return JSON.parse(text) as T;
  } catch {
    const kind = response.headers.get("content-type") ?? "(未知类型)";
    throw new ApiError(
      response.status,
      `${path} 返回的不是 JSON(${kind})。八成是请求没到后端——` +
        `开发模式下检查 vite 代理有没有覆盖这条路径,生产下检查反向代理。`,
    );
  }
}

export const api = {
  tasks: (params: { settled?: string } = {}) =>
    call<TaskSummary[]>(`/tasks?${new URLSearchParams(params as Record<string, string>)}`),
  task: (id: string) => call<TaskDetail>(`/tasks/${id}`),
  submit: (body: SubmitRequest) =>
    call<TaskDetail>("/tasks", { method: "POST", body: JSON.stringify(body) }),
  requirements: () => call<RequirementSummary[]>("/requirements"),
  requirement: (id: string) => call<RequirementDetail>(`/requirements/${id}`),
  patchRequirement: (id: string, body: RequirementPatch) =>
    call<RequirementDetail>(`/requirements/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  resplitRequirement: (id: string) =>
    call<TaskDetail>(`/requirements/${id}/resplit`, { method: "POST" }),
  cancel: (id: string) => call<TaskSummary>(`/tasks/${id}/cancel`, { method: "POST" }),
  resolveTaskIntervention: (id: string, note = "") =>
    call<TaskSummary>(`/tasks/${id}/intervention/resolve`, {
      method: "POST",
      body: JSON.stringify({ note }),
    }),
  retryTaskIntervention: (id: string, requirement: string) =>
    call<TaskDetail>(`/tasks/${id}/intervention/retry`, {
      method: "POST",
      body: JSON.stringify({ requirement }),
    }),
  // 202:后台在推,不等它跑完。前端靠 `subscribe` 的推送知道这一步什么时候落地,
  // 不靠这次调用的返回值。
  run: (id: string) => call<TaskSummary>(`/tasks/${id}/run`, { method: "POST" }),
  decide: (id: string, body: ApprovalRequest) =>
    call<TaskSummary>(`/tasks/${id}/approval`, { method: "POST", body: JSON.stringify(body) }),
  events: (id: string, offset = 0, limit = 100) =>
    call<EventPage>(`/tasks/${id}/events?offset=${offset}&limit=${limit}`),
  /** 游标是**行号**而不是字节偏移:日志在追加,偏移量会随内容变化错位。 */
  logs: (id: string, cursor = 0, limit = 200) =>
    call<LogPage>(`/tasks/${id}/logs?cursor=${cursor}&limit=${limit}`),
  artifacts: (id: string) => call<ArtifactList>(`/tasks/${id}/artifacts`),
  /** 每个 stage 里 Job 真正的对话过程——与"实时日志"(生命周期事件)不是同一份数据。 */
  trace: (id: string) => call<TaskTrace>(`/tasks/${id}/trace`),
  genomeTrace: (id: string) => call<TaskTrace>(`/genome/tasks/${id}/trace`),

  // --- 会话 -------------------------------------------------------------------
  //
  // 没有一个改 mode 的方法,而且不该有:只读与可写是两套工具集,提供这条路径等于
  // 提供一次不经任何闸门的提权。前端也因此把模式渲染成标签而不是可点的 Tab。
  /**
   * 有哪些员工可选,以及哪些开得了会话。
   *
   * **能力由后端算,前端不按运行时名去猜。** 猜的话接第三个运行时时会漏判,而漏判的
   * 表现是选择器里一个开不了会话的员工看起来可选。
   */
  employees: () => call<EmployeeList>("/employees"),
  sessions: (query: Record<string, string> = {}) =>
    call<SessionList>(`/sessions?${new URLSearchParams(query)}`),
  session: (id: string) => call<SessionSummary>(`/sessions/${id}`),
  createSession: (body: SessionCreateRequest) =>
    call<SessionSummary>("/sessions", { method: "POST", body: JSON.stringify(body) }),
  /**
   * 发一条消息,边到边把块喂给 `onBlock`。
   *
   * **走这里而不是在页面里手搓 `fetch`。** ADR-0001 把 mock 边界定在这个模块上,
   * 页面里另开一条网络路径的话,那条路径既不被页面测试覆盖,也不受生成类型的保护。
   *
   * 这一轮在后端是后台任务,**不绑定这次请求**——这次调用中途被打断(页面关掉、组件
   * 卸载)不会打断生成,已经在跑的那一轮会继续把块落盘,重新连上时用
   * `attachSessionStream` 接。
   */
  sendSessionMessage: async (
    id: string,
    message: string,
    onBlock: (block: BlockItem) => void,
  ): Promise<void> => {
    const response = await fetch(`${base}/sessions/${id}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...workspaceHeaders() },
      body: JSON.stringify({ message }),
    });
    if (!response.ok) {
      const body = (await response.json().catch(() => ({}))) as { detail?: string };
      throw new ApiError(response.status, body.detail ?? `HTTP ${response.status}`);
    }
    await readBlockStream(response, onBlock);
  },
  /** 历史块。`after` 之后的那些——断线补齐与回放共用它。 */
  sessionMessages: (id: string, after = 0) =>
    call<BlockPage>(`/sessions/${id}/messages?after=${after}`),
  /**
   * 接上一个 session 的块流:补齐 `after` 之后的历史,如果这会儿有一轮在跑就接着直播。
   *
   * **专给"重新打开页面/切回来发现 `SessionSummary.generating` 是 true"用**——发消息
   * 本身走 `sendSessionMessage`,那条路自己已经在看自己起的这一轮,不需要再接一次。
   *
   * 传 `signal` 以便组件卸载/切走时中止这次连接——后端那侧不受影响,纯粹是我们自己
   * 摘掉这个订阅位。
   */
  attachSessionStream: async (
    id: string,
    after: number,
    onBlock: (block: BlockItem) => void,
    signal?: AbortSignal,
  ): Promise<void> => {
    const response = await fetch(`${base}/sessions/${id}/messages/stream?after=${after}`, {
      signal,
      headers: workspaceHeaders(),
    });
    if (!response.ok) {
      const body = (await response.json().catch(() => ({}))) as { detail?: string };
      throw new ApiError(response.status, body.detail ?? `HTTP ${response.status}`);
    }
    await readBlockStream(response, onBlock);
  },
  /**
   * 打断正在跑的那一轮。没有一轮在跑也不算错——用户点两下很正常,第二下它已经如愿了。
   */
  stopSession: (id: string) => call<SessionSummary>(`/sessions/${id}/stop`, { method: "POST" }),
  endSession: (id: string) => call<SessionSummary>(`/sessions/${id}/end`, { method: "POST" }),
  resumeSession: (id: string) => call<SessionSummary>(`/sessions/${id}/resume`, { method: "POST" }),
  /** 转任务:**返回草稿,不建任务**。确认后走 `submit`。 */
  escalateSession: (id: string) => call<TaskDraft>(`/sessions/${id}/escalate`, { method: "POST" }),
  /** 钉住/取消钉住一条上下文。钉住的不参与截断。 */
  pinContext: (id: string, item: string, pinned = true) =>
    call<SessionSummary>(`/sessions/${id}/pin`, {
      method: "POST",
      body: JSON.stringify({ item, pinned }),
    }),
  dropContext: (id: string, item: string) =>
    call<SessionSummary>(`/sessions/${id}/context/${item}`, { method: "DELETE" }),
  /**
   * 有用/没用。
   *
   * **记账在后端做。** 前端不自行改任何 hits 显示值——那是基因组的写操作,必须走后端
   * 既有路径(§10.2)。这里只把结果显示出来。
   */
  sessionFeedback: (id: string, useful: boolean) =>
    call<FeedbackResponse>(`/sessions/${id}/feedback`, {
      method: "POST",
      body: JSON.stringify({ useful }),
    }),
  injectSession: (id: string, body: InjectRequest) =>
    call<SessionSummary>(`/sessions/${id}/inject`, { method: "POST", body: JSON.stringify(body) }),
  // --- 待办:派给人的那些 Job ---------------------------------------------------
  //
  // **它与「待我审批」不是一回事**:审批看的是整个任务的 diff 并且可以否决,待办看的是
  // 一份活并且要交产物。混在一个列表里的话,"轮到你干活"与"轮到你拍板"会长得一样,
  // 而它们需要的注意力完全不同。
  todos: (assignee = "") =>
    call<TodoList>(`/todos${assignee ? `?assignee=${encodeURIComponent(assignee)}` : ""}`),
  todo: (id: string) => call<TodoDetail>(`/todos/${id}`),
  submitTodo: (id: string, result: Record<string, unknown> | null) =>
    call<TodoSubmitResponse>(`/todos/${id}/submit`, {
      method: "POST",
      body: JSON.stringify({ result }),
    }),
  artifact: (id: string, path: string) =>
    fetch(`${base}/tasks/${id}/artifacts/${path}`, { headers: workspaceHeaders() }).then((r) =>
      r.ok ? r.text() : "",
    ),
  report: (id: string) => call<ReportResponse>(`/tasks/${id}/report`),
  previewRejection: (id: string, comment: string, lineComments: LineComment[]) =>
    call<ApprovalPreview>(`/tasks/${id}/approval/preview`, {
      method: "POST",
      body: JSON.stringify({ comment, line_comments: lineComments }),
    }),
  metrics: async () => (await fetch(`${base}/metrics`)).text(),
  workspaces: () => call<WorkspaceList>("/workspaces"),
  createWorkspace: (body: WorkspaceCreateRequest) =>
    call<WorkspaceCreated>("/workspaces", { method: "POST", body: JSON.stringify(body) }),
  remountWorkspace: (name: string) =>
    call<GenomeTaskSummary>(`/workspaces/${encodeURIComponent(name)}/mount`, { method: "POST" }),
  knowledgeInit: () =>
    call<GenomeTaskSummary>("/genome/tasks/init", { method: "POST" }),

  // --- 基因组管理(PRD 12) ---------------------------------------------------
  projectMap: () => call<ProjectMapResponse>("/genome/project-map"),
  /** `asset` 选基因组的哪一层：知识 / 规则 / 工序。三层都要能回溯。 */
  projectMapVersions: (asset = "knowledge") =>
    call<ProjectMapVersionList>(`/genome/project-map/versions?asset=${asset}`),
  projectMapDiff: (from: string, to: string, asset = "knowledge") =>
    call<ProjectMapDiffResponse>(
      `/genome/project-map/diff?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}&asset=${asset}`,
    ),
  lessons: (
    params: { q?: string; module?: string; min_hits?: string; status?: string; sort?: string } = {},
  ) =>
    call<LessonList>(`/genome/lessons?${new URLSearchParams(params as Record<string, string>)}`),
  addLesson: (body: LessonCreateRequest) =>
    call<LessonCardResponse>("/genome/lessons", { method: "POST", body: JSON.stringify(body) }),
  deprecateLesson: (id: string) =>
    call<LessonCardResponse>(`/genome/lessons/${id}/deprecate`, { method: "POST" }),
  restoreLesson: (id: string) =>
    call<LessonCardResponse>(`/genome/lessons/${id}/restore`, { method: "POST" }),
  rules: () => call<RuleSetResponse>("/genome/rules"),
  proposeRuleChange: (body: RuleProposalRequest) =>
    call<RuleProposalResponse>("/genome/rules/proposal", { method: "POST", body: JSON.stringify(body) }),
  procedureStats: () => call<ProcedureStatsList>("/genome/procedures/stats"),
  knowledgeStatus: (q = "") =>
    call<KnowledgeStatus>(`/genome/knowledge?q=${encodeURIComponent(q)}`),
  /** 缺省只列人还需要看见的：系统自发的蒸馏失败算已了结，全量列会把在跑的那几个淹没。 */
  genomeTasks: (params: { kind?: string; state?: string; settled?: string } = {}) =>
    call<GenomeTaskList>(`/genome/tasks?${new URLSearchParams(params as Record<string, string>)}`),
  genomeTask: (id: string) => call<GenomeTaskSummary>(`/genome/tasks/${id}`),
  genomeProgress: (id: string) => call<GenomeTaskProgress>(`/genome/tasks/${id}/progress`),
  cancelGenomeTask: (id: string) =>
    call<GenomeTaskSummary>(`/genome/tasks/${id}/cancel`, { method: "POST" }),
  resolveGenomeIntervention: (id: string, note = "") =>
    call<GenomeTaskSummary>(`/genome/tasks/${id}/intervention/resolve`, {
      method: "POST",
      body: JSON.stringify({ note }),
    }),
  runGenomeTask: (id: string) =>
    call<GenomeTaskSummary>(`/genome/tasks/${id}/run`, { method: "POST" }),
  reinitModules: (modules: string[]) =>
    call<GenomeTaskList>("/genome/tasks/reinit", {
      method: "POST",
      body: JSON.stringify({ modules }),
    }),
  gateDraft: (id: string) => call<GateDraft>(`/genome/tasks/${id}/gate`),
  answerGate: (id: string, body: GateAnswer) =>
    call<GateResult>(`/genome/tasks/${id}/gate`, { method: "POST", body: JSON.stringify(body) }),

  // --- 观测中心(PRD 12) -----------------------------------------------------
  trends: (windowDays = 7) => call<TrendReport>(`/insights/trends?window_days=${windowDays}`),
  costs: () => call<CostReport>("/insights/costs"),
  roster: () => call<RosterReport>("/insights/roster"),
  /**
   * 能选的执行拓扑。**名单与文案都由后端给**——前端硬编码一份的话,加第六个模板时
   * 下拉里会少一个,而少的那个在命令行上明明可选。
   */
  topologies: () => call<TopologyCatalog>("/topologies"),
  /**
   * 现在生效的配置里能改的那几段,外加"你改不改得动"。
   *
   * **表单要听它,不听 `roster()` 的 dial**:dial 是给人看的档位摘要,阈值与确认名单
   * 在里面根本不存在——拿它当数据源等于从一个形状读、往另一个形状写。
   */
  settings: () => call<SettingsView>("/settings"),
  /**
   * 把一个员工挪到信任爬坡的某一档。
   *
   * **一个调用管三档**,尽管它们落在两个存储上(员工定义的运行时 / 根配置的确认名单):
   * 分派由服务端做——前端拼一遍的话,拼错的那一次会造出"human 却又在确认名单里"这种
   * 谁都解释不了的状态。
   */
  setExecution: (employee: string, execution: string, assignee = "") =>
    call<SettingsChange>(`/employees/${employee}/execution`, {
      method: "PUT",
      body: JSON.stringify({ execution, assignee }),
    }),
  /**
   * 改一段配置。**按段提交**:一次写入覆盖多段会把别人这期间改的另一段一起盖回旧值,
   * 而设置层那把互斥锁挡不住这个(它挡的是并发写,不是"你手里那份读得太早")。
   */
  updateSettings: (section: string, value: unknown) =>
    call<SettingsChange>("/settings", {
      method: "PUT",
      body: JSON.stringify({ section, value }),
    }),
  /**
   * 探一遍容器运行时那条链路。**只读**——它的全部意义是"在改任何东西之前就知道通不通"。
   *
   * 结果是**分项**的:平台、存储、Matrix、服务端凭证指向四个不同的运维动作,前端不要把它们折叠成
   * 一个红绿灯——那样"哪一项挂了"就答不出来了。
   */
  containerRuntimeReadiness: () =>
    call<ReadinessView>("/settings/container-runtime/readiness", { method: "POST" }),
  /**
   * 这个员工跑在哪儿、能跑在哪儿,以及换到 `candidate` 还差哪些兼容声明。
   *
   * **选项由服务端给**——前端枚举的话,配置里没配的运行时会出现在下拉框里,而选中
   * 之后的失败要等到下一次派发才出现。
   */
  employeeRuntime: (employee: string, candidate = "") =>
    call<RuntimeChoiceView>(
      `/employees/${employee}/runtime${candidate ? `?candidate=${encodeURIComponent(candidate)}` : ""}`,
    ),
  setEmployeeRuntime: (employee: string, runtime: string) =>
    call<SettingsChange>(`/employees/${employee}/runtime`, {
      method: "PUT",
      body: JSON.stringify({ runtime }),
    }),
  /**
   * 给这个员工的工序补兼容声明。**显式动作**:换运行时时不会自动发生——自动补等于
   * 把兼容闸变成摆设。
   */
  declareCompat: (employee: string, runtime: string, procedures: string[] = []) =>
    call<SettingsChange>(`/employees/${employee}/runtime/compat`, {
      method: "POST",
      body: JSON.stringify({ runtime, procedures }),
    }),
  /**
   * 每个容器员工此刻在平台上的状态。**每次都去问平台,前端也别缓存**——真机实测
   * Worker 重建会换房间 id,而缓存下来的那个仍是一个格式正确、但没人在听的 id。
   */
  workerStatuses: () => call<WorkerStatusListView>("/employees/workers"),
  /** 对齐一次**会**做什么。只读:算这份计划不会在平台上写任何东西。 */
  workerPlan: () => call<WorkerPlanView>("/employees/workers/plan"),
  /**
   * 把这个员工对齐成平台上一个就绪的 Worker。**幂等**,可反复调。
   *
   * **一次一个。** 整份花名册由调用方逐个走完——供应是长动作(每建一个都会真的拉起
   * 容器并做一次模型探活),逐个走进度才是真的,而不是一个转到底的圈。
   */
  provisionWorker: (employee: string) =>
    call<WorkerProvisionResult>(`/employees/${employee}/worker`, { method: "POST" }),
  /** 让这个员工的容器休眠。**可逆**——下一次派发会自动唤醒,所以不必二次确认。 */
  sleepWorker: (employee: string) =>
    call<WorkerLifecycleResult>(`/employees/${employee}/worker/sleep`, { method: "POST" }),
  /**
   * 删掉这个员工的容器。**不可逆**:重建会换房间 id。
   *
   * `confirm=true` 由服务端要求,不是界面上的仪式——弹窗不是边界,一条手滑的 curl
   * 一样能删。
   */
  deleteWorker: (employee: string) =>
    call<WorkerLifecycleResult>(`/employees/${employee}/worker?confirm=true`, {
      method: "DELETE",
    }),
  auditEvents: (
    params: {
      task_id?: string;
      actor?: string;
      actor_kind?: string;
      kind?: string;
      kinds?: string;
      since?: string;
      until?: string;
      workspace?: string;
    } = {},
  ) =>
    call<AuditEventPage>(`/audit/events?${new URLSearchParams(params as Record<string, string>)}`),
  auditExportUrl: (taskId: string) => `${base}/audit/export/${taskId}`,

  // --- 体验补齐(PRD 12) -----------------------------------------------------
  importTicket: (url: string) =>
    call<ImportResult>("/requirements/import", { method: "POST", body: JSON.stringify({ url } satisfies ImportRequest) }),
  notificationPreferences: () => call<NotificationPreferenceList>("/notifications/preferences"),
  saveNotificationPreference: (body: NotificationPreference) =>
    call<NotificationPreference>("/notifications/preferences", { method: "PUT", body: JSON.stringify(body) }),
};
