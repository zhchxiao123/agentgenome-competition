import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskCenter } from "./TaskCenter";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      tasks: vi.fn(),
      genomeTasks: vi.fn(),
      task: vi.fn(),
      logs: vi.fn(),
      run: vi.fn(),
      trace: vi.fn(),
      artifacts: vi.fn(),
      requirement: vi.fn(),
      todo: vi.fn(),
      submitTodo: vi.fn(),
      resolveTaskIntervention: vi.fn(),
      retryTaskIntervention: vi.fn(),
    },
  };
});
vi.mock("../api/live", () => ({ subscribe: vi.fn(() => () => undefined) }));
vi.mock("./GenomeTaskDetail", () => ({
  GenomeTaskDetail: ({ id }: { id: string }) => <div>基因组详情 {id}</div>,
}));

const { api, ApiError } = await import("../api/client");
const { subscribe } = await import("../api/live");
const tasksMock = vi.mocked(api.tasks);
const genomeMock = vi.mocked(api.genomeTasks);
const taskMock = vi.mocked(api.task);
const logsMock = vi.mocked(api.logs);
const runMock = vi.mocked(api.run);
const traceMock = vi.mocked(api.trace);
const artifactsMock = vi.mocked(api.artifacts);
const requirementMock = vi.mocked(api.requirement);
const todoMock = vi.mocked(api.todo);
const submitTodoMock = vi.mocked(api.submitTodo);
const resolveInterventionMock = vi.mocked(api.resolveTaskIntervention);
const retryInterventionMock = vi.mocked(api.retryTaskIntervention);
const subscribeMock = vi.mocked(subscribe);

const devTask = {
  id: "ag-0001",
  title: "修一个下单的 bug",
  state: "DEVELOPING",
  priority: 5,
  fix_rounds: 0,
  plan_retries: 0,
  needs_itest: "UNDECIDED",
  itest_override: "auto",
  mode: "autonomous",
  topology: "",
  tokens_used: 0,
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
  can_run: true,
  execution_status: "idle",
} as const;

const genomeTask = {
  id: "gn-0001",
  title: "重建 order 的认知",
  kind: "reinit",
  origin: "human",
  state: "DEEP_READ",
  subject: "order",
  source_task_id: "ag-0001",
  tokens_used: 0,
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
  overdue: false,
};

beforeEach(() => {
  tasksMock.mockReset();
  genomeMock.mockReset();
  tasksMock.mockResolvedValue([]);
  genomeMock.mockResolvedValue({ items: [] });
  taskMock.mockReset();
  logsMock.mockReset();
  logsMock.mockResolvedValue({ items: [], next_cursor: null, total: 0 });
  runMock.mockReset();
  traceMock.mockReset();
  traceMock.mockResolvedValue({ task_id: "ag-0001", stages: [] });
  artifactsMock.mockReset();
  artifactsMock.mockResolvedValue({ items: [] });
  requirementMock.mockReset();
  todoMock.mockReset();
  submitTodoMock.mockReset();
  resolveInterventionMock.mockReset();
  retryInterventionMock.mockReset();
  subscribeMock.mockReset();
  subscribeMock.mockReturnValue(() => undefined);
});

afterEach(() => vi.useRealTimers());

describe("TaskCenter", () => {
  async function openBoard() {
    await userEvent.click(await screen.findByRole("button", { name: "看板视图" }));
  }

  it("defaults to a compact task list and opens the selected task in the adjacent detail panel", async () => {
    const onOpen = vi.fn();
    tasksMock.mockResolvedValue([devTask]);
    taskMock.mockResolvedValue({ ...devTask, requirement: "下单要预占库存" });

    render(<TaskCenter openId="ag-0001" onOpen={onOpen} />);

    expect(await screen.findByRole("button", { name: /修一个下单的 bug/ })).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("region", { name: "任务详情" })).toHaveTextContent("修一个下单的 bug");
    expect(screen.getByRole("tab", { name: "概览" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("region", { name: "任务详情" })).toHaveTextContent("P2");
  });

  it("shows an error instead of a silently-empty board when the task list fails to load", async () => {
    // 回归测试:这里曾经是 `.catch(() => undefined)`,失败时泳道照样渲染成"全部是空的",
    // 跟"这个 Workspace 真的还没有任务"在界面上完全没法分辨。
    tasksMock.mockRejectedValue(new ApiError(500, "backend unreachable"));

    render(<TaskCenter openId={null} onOpen={vi.fn()} />);

    expect(await screen.findByText(/拉不到任务列表/)).toHaveTextContent("backend unreachable");
  });

  it("degrades a genome list failure without hiding the escalated lane", async () => {
    // 同一条,对新的那一半:一个只有研发任务的看板与"基因组任务拉不到"长得一模一样。
    // 但**不能把整块看板换成一行字**——那会连 ESCALATED 那一列一起藏起来,而那正是
    // "系统在最需要注意力的时候把自己藏起来",只是从可用性那一侧进来的。
    tasksMock.mockResolvedValue([{ ...devTask, state: "ESCALATED", title: "炸了的任务" }]);
    genomeMock.mockRejectedValue(new ApiError(500, "genome list down"));

    render(<TaskCenter openId={null} onOpen={vi.fn()} />);

    expect(await screen.findByText(/拉不到基因组任务/)).toHaveTextContent("genome list down");
    expect(screen.getByText("炸了的任务")).toBeInTheDocument();
    await openBoard();
    expect(screen.getByText("ESCALATED")).toBeInTheDocument();
  });

  it("renders the lanes when the task list loads successfully", async () => {
    render(<TaskCenter openId={null} onOpen={vi.fn()} />);

    await openBoard();
    expect(await screen.findByText("CREATED")).toBeInTheDocument();
  });

  it("renders both kinds of task, each in its own set of lanes", async () => {
    // 基因组任务的状态集与研发任务的不重合。把「深读中」映射进「开发中」会产生一批
    // 语义错误的归类——而错误的归类比没有归类更难发现:界面上它看起来完全正常。
    tasksMock.mockResolvedValue([devTask]);
    genomeMock.mockResolvedValue({ items: [genomeTask] });

    render(<TaskCenter openId={null} onOpen={vi.fn()} />);

    expect(await screen.findByText("修一个下单的 bug")).toBeInTheDocument();
    await openBoard();
    expect(screen.getByText("重建 order 的认知")).toBeInTheDocument();
    expect(screen.getByText("DEEP_READ")).toBeInTheDocument();
  });

  it("shows the genome task's kind, module and source task on the card", async () => {
    genomeMock.mockResolvedValue({ items: [genomeTask] });

    render(<TaskCenter openId={null} onOpen={vi.fn()} />);

    expect(await screen.findByText("重建")).toBeInTheDocument();
    expect(screen.getByText("模块 order")).toBeInTheDocument();
    expect(screen.getByText(/来源任务 ag-0001/)).toBeInTheDocument();
  });

  it("filters the board down to one kind of task", async () => {
    tasksMock.mockResolvedValue([devTask]);
    genomeMock.mockResolvedValue({ items: [genomeTask] });
    render(<TaskCenter openId={null} onOpen={vi.fn()} />);
    await screen.findByText("修一个下单的 bug");

    await userEvent.click(screen.getByRole("button", { name: "基因组任务" }));

    expect(screen.getByText("重建 order 的认知")).toBeInTheDocument();
    // 筛掉的那一类要真的不在,不是变灰。
    expect(screen.queryByText("修一个下单的 bug")).not.toBeInTheDocument();
  });

  it("only asks for settled tasks once the toggle is switched on", async () => {
    // 默认接口把已完成/已取消的任务过滤掉——这是任务中心"完成的任务都不见了"的根因。
    // 开关打开后要带上 `settled: "true"` 重新拉一次,而不是在前端假装过滤客户端已有的数据。
    tasksMock.mockResolvedValue([devTask]);
    genomeMock.mockResolvedValue({ items: [genomeTask] });
    render(<TaskCenter openId={null} onOpen={vi.fn()} />);
    await screen.findByText("修一个下单的 bug");
    expect(tasksMock).toHaveBeenLastCalledWith({});
    expect(genomeMock).toHaveBeenLastCalledWith({});

    await userEvent.click(screen.getByRole("checkbox", { name: "显示已完结任务" }));

    expect(tasksMock).toHaveBeenLastCalledWith({ settled: "true" });
    expect(genomeMock).toHaveBeenLastCalledWith({ settled: "true" });
  });

  it("adds completed and cancelled lanes to the board only once settled tasks are shown", async () => {
    tasksMock.mockResolvedValue([{ ...devTask, id: "ag-done", title: "已经上线的任务", state: "COMPLETED" }]);
    render(<TaskCenter openId={null} onOpen={vi.fn()} />);
    await openBoard();
    expect(screen.queryByText("COMPLETED")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("checkbox", { name: "显示已完结任务" }));

    expect(await screen.findByText("COMPLETED")).toBeInTheDocument();
    // 研发与基因组两套泳道都新增了各自的"已取消",两个都该出现。
    expect(screen.getAllByText("CANCELLED")).toHaveLength(2);
  });

  it("tells the two kinds apart visually", async () => {
    // 一眼分清系统在改代码还是在改认知——不用读文字。
    tasksMock.mockResolvedValue([devTask]);
    genomeMock.mockResolvedValue({ items: [genomeTask] });

    render(<TaskCenter openId={null} onOpen={vi.fn()} />);

    await openBoard();
    const genomeCard = (await screen.findByText("重建 order 的认知")).closest(".tk");
    const devCard = screen.getByText("修一个下单的 bug").closest(".tk");
    expect(genomeCard).toHaveClass("gn");
    expect(devCard).not.toHaveClass("gn");
  });

  it("opens the source task when the link on a genome card is clicked", async () => {
    const onOpen = vi.fn();
    genomeMock.mockResolvedValue({ items: [genomeTask] });
    render(<TaskCenter openId={null} onOpen={onOpen} />);

    await userEvent.click(await screen.findByText(/来源任务 ag-0001/));

    expect(onOpen).toHaveBeenCalledWith("ag-0001");
    // 卡片本身也是可点的;不拦住冒泡的话这里会被调用两次、打开的是基因组任务。
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("keeps awaiting-confirmation out of the exception lanes", async () => {
    // **本页最重要的一条。** 上一次的教训是"升级人工的任务从看板上消失";加进待确认之后,
    // 同样的错误会以镜像的形式重来——不是消失,而是异常队列里混进一堆健康任务,真正出事的
    // 那个被淹没。
    tasksMock.mockResolvedValue([
      { ...devTask, id: "ag-bad", title: "炸了的任务", state: "ESCALATED" },
    ]);
    genomeMock.mockResolvedValue({
      items: [{ ...genomeTask, title: "等我确认的", state: "AWAITING_CONFIRMATION" }],
    });

    render(<TaskCenter openId={null} onOpen={vi.fn()} />);

    await openBoard();
    const exception = (await screen.findByText("ESCALATED")).closest(".lane");
    const gate = screen.getByText("AWAITING_CONFIRMATION").closest(".lane");
    expect(exception).toHaveTextContent("炸了的任务");
    expect(exception).not.toHaveTextContent("等我确认的");
    expect(gate).toHaveTextContent("等我确认的");
    expect(gate).not.toHaveTextContent("炸了的任务");
  });

  it("marks the two kinds of waiting differently", async () => {
    // 视觉标记不同、动作文案不同:一个是"确认",一个是"诊断"。这不是措辞偏好——
    // 它是那条区分在界面上的兑现方式。
    genomeMock.mockResolvedValue({
      items: [
        { ...genomeTask, id: "gn-a", title: "等确认", state: "AWAITING_CONFIRMATION" },
        { ...genomeTask, id: "gn-b", title: "挂了的", state: "FAILED" },
      ],
    });

    render(<TaskCenter openId={null} onOpen={vi.fn()} />);

    await openBoard();
    expect(await screen.findByText("去确认")).toBeInTheDocument();
    expect(screen.getByText("去诊断")).toBeInTheDocument();
    // 待确认的泳道不套异常那一类样式。
    expect(screen.getByText("AWAITING_CONFIRMATION").closest(".lane")).not.toHaveClass("term");
    expect(screen.getByText("FAILED").closest(".lane")).toHaveClass("term");
  });

  it("keeps system-origin genome failures out of the human exception count", async () => {
    genomeMock.mockResolvedValue({
      items: [
        { ...genomeTask, id: "gn-human", title: "人工任务失败", state: "FAILED", origin: "human" },
        { ...genomeTask, id: "gn-system", title: "系统任务失败", state: "FAILED", origin: "system" },
      ],
    });

    render(<TaskCenter openId={null} onOpen={vi.fn()} />);

    const metrics = await screen.findByLabelText("任务状态统计");
    const exceptionMetric = within(metrics).getByText("异常").closest(".task-metric");
    expect(within(exceptionMetric as HTMLElement).getByText("1")).toBeInTheDocument();

    await openBoard();
    expect(screen.getAllByText("去诊断")).toHaveLength(1);
  });

  it("flags a gate that has been waiting too long", async () => {
    genomeMock.mockResolvedValue({
      items: [{ ...genomeTask, state: "AWAITING_CONFIRMATION", overdue: true }],
    });

    render(<TaskCenter openId={null} onOpen={vi.fn()} />);

    await openBoard();
    expect(await screen.findByText(/等太久了/)).toBeInTheDocument();
  });

  it("opens the genome detail for a genome task, not the dev one", async () => {
    genomeMock.mockResolvedValue({ items: [genomeTask] });

    render(<TaskCenter openId="gn-0001" onOpen={vi.fn()} />);

    expect(await screen.findByText("基因组详情 gn-0001")).toBeInTheDocument();
  });

  it("still renders the development-task blocks that genome tasks drop", async () => {
    // 防回归:按类型裁剪只对基因组任务生效。研发任务的风险评级与需求原文要照旧——
    // 一个"顺手统一一下详情页"的改动会把它们一起删掉,而那正是这条守着的。
    taskMock.mockResolvedValue({
      ...devTask,
      requirement: "下单要预占库存",
      risk_level: "high",
      branch: "task/ag-0001",
      escalate_reason: null,
      budget_tokens: null,
    });

    render(<TaskCenter openId="ag-0001" onOpen={vi.fn()} />);

    expect(await screen.findByText("需求原文")).toBeInTheDocument();
    expect(screen.getAllByText("high").length).toBeGreaterThan(0);
  });

  it("links a child-requirement task to its parent requirement", async () => {
    // 子需求的任务要能一步跳到上级需求:审的是一个子需求,但"这属于哪件大事"要一眼可见。
    taskMock.mockResolvedValue({
      ...devTask,
      requirement: "实现库存预占接口",
      requirement_id: "req-child-1",
      can_run: false,
    });
    requirementMock.mockResolvedValue({
      id: "req-child-1", title: "预占接口", text: "实现库存预占接口", priority: 5,
      state: "in_progress", parked: "", attempts: 1,
      parent_id: "req-parent-9", blocked_by: [], children_total: 0, children_delivered: 0,
      children: [], tree_tokens: 0,
      chain: [], total_tokens: 0,
      created_at: "2026-01-01T00:00:00+00:00", updated_at: "2026-01-01T00:00:00+00:00",
    });

    render(<TaskCenter openId="ag-0001" onOpen={vi.fn()} />);

    expect(await screen.findByText("上级需求")).toBeInTheDocument();
    expect(screen.getByText("req-parent-9")).toBeInTheDocument();
  });

  it("shows the split proposal and its confirmation action on the waiting task", async () => {
    const pending = {
      id: "todo-split",
      kind: "split",
      assignee: "decision-employee",
    };
    taskMock.mockResolvedValue({
      ...devTask,
      state: "CREATED",
      requirement: "构建兼容 SQLite 的 SQL 引擎",
      can_run: false,
      pending_todo: pending,
    } as never);
    todoMock.mockResolvedValue({
      id: "todo-split",
      task_id: "ag-0001",
      stage: "plan",
      node: "split",
      assignee: "decision-employee",
      employee_id: "decision-employee",
      procedure_id: "requirement-analysis",
      kind: "split",
      state: "pending",
      reminded: false,
      reassignments: 0,
      created_at: "2026-01-01T00:00:00+00:00",
      updated_at: "2026-01-01T00:00:00+00:00",
      due_at: "2026-01-04T00:00:00+00:00",
      proposal: {
        children: [
          { title: "核心引擎", text: "实现解析、存储与执行。验收:基础查询通过。" },
          { title: "性能收口", text: "建立对比基准。验收:性能不回退。", blocked_by: [0] },
        ],
        rationale: "单任务无法评审。",
      },
      context_file: "",
      output_dir: "tasks/ag-0001/artifacts/01-plan",
      workdir: "",
      schema: { required: ["approved"] },
    });

    render(<TaskCenter openId="ag-0001" onOpen={vi.fn()} />);

    expect(await screen.findByRole("heading", { name: "等待拆分确认" })).toBeInTheDocument();
    expect(screen.getByText("核心引擎")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认拆分" })).toBeInTheDocument();
    expect(screen.queryByText("无需人工操作")).not.toBeInTheDocument();
  });

  it("links the task to its requirement and tells when a later attempt delivered it", async () => {
    taskMock.mockResolvedValue({
      ...devTask,
      state: "ESCALATED",
      requirement: "下单要预占库存",
      requirement_id: "req-20260813-001",
      can_run: false,
    });
    requirementMock.mockResolvedValue({
      id: "req-20260813-001",
      title: "预占库存",
      text: "下单要预占库存",
      priority: 5,
      state: "delivered",
      parked: "",
      attempts: 2,
      created_at: "2026-01-01T00:00:00+00:00",
      updated_at: "2026-01-01T00:00:00+00:00",
      parent_id: "", blocked_by: [], children_total: 0, children_delivered: 0,
      children: [], tree_tokens: 0,
      chain: [],
      total_tokens: 0,
    });
    const onOpenRequirement = vi.fn();
    render(<TaskCenter openId="ag-0001" onOpen={vi.fn()} onOpenRequirement={onOpenRequirement} />);

    await userEvent.click(await screen.findByText("req-20260813-001"));
    expect(onOpenRequirement).toHaveBeenCalledWith("req-20260813-001");
    expect(await screen.findByText("该需求已由后续尝试交付")).toBeInTheDocument();
  });

  it("edits the requirement, creates a successor and opens it as one intervention action", async () => {
    const onOpen = vi.fn();
    taskMock.mockResolvedValue({
      ...devTask,
      state: "ESCALATED",
      requirement: "下单要预占库存",
      requirement_id: "req-20260813-001",
      can_run: false,
    });
    requirementMock.mockResolvedValue({
      id: "req-20260813-001", title: "预占库存", text: "下单要预占库存", priority: 5,
      state: "needs_attention", parked: "", attempts: 1,
      created_at: "2026-01-01T00:00:00+00:00", updated_at: "2026-01-01T00:00:00+00:00",
      parent_id: "", blocked_by: [], children_total: 0, children_delivered: 0,
      children: [], tree_tokens: 0,
      chain: [], total_tokens: 0,
    });
    retryInterventionMock.mockResolvedValue({
      ...devTask,
      id: "ag-0002",
      requirement: "支持按 SKU 预占库存",
      requirement_id: "req-20260813-001",
    });
    render(<TaskCenter openId="ag-0001" onOpen={onOpen} onOpenRequirement={vi.fn()} />);

    await userEvent.click(await screen.findByRole("button", { name: "修改需求并再试一次" }));
    const editor = screen.getByRole("textbox", { name: "修改后的需求" });
    await userEvent.clear(editor);
    await userEvent.type(editor, "支持按 SKU 预占库存");
    await userEvent.click(screen.getByRole("button", { name: "创建新尝试" }));

    expect(retryInterventionMock).toHaveBeenCalledWith("ag-0001", "支持按 SKU 预占库存");
    expect(onOpen).toHaveBeenCalledWith("ag-0002");
  });

  it("can close an escalation without retrying it", async () => {
    const onOpen = vi.fn();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    taskMock.mockResolvedValue({
      ...devTask,
      state: "ESCALATED",
      requirement: "下单要预占库存",
      requirement_id: "req-20260813-001",
      can_run: false,
    });
    requirementMock.mockResolvedValue({
      id: "req-20260813-001", title: "预占库存", text: "下单要预占库存", priority: 5,
      state: "queued", parked: "", attempts: 1,
      created_at: "2026-01-01T00:00:00+00:00", updated_at: "2026-01-01T00:00:00+00:00",
      parent_id: "", blocked_by: [], children_total: 0, children_delivered: 0,
      children: [], tree_tokens: 0,
      chain: [], total_tokens: 0,
    });
    resolveInterventionMock.mockResolvedValue({} as never);
    render(<TaskCenter openId="ag-0001" onOpen={onOpen} onOpenRequirement={vi.fn()} />);

    await userEvent.click(await screen.findByRole("button", { name: "标记已处理" }));
    expect(resolveInterventionMock).toHaveBeenCalledWith("ag-0001");
    expect(onOpen).toHaveBeenCalledWith(null);
  });

  it("does not count or offer actions for a resolved escalation in history", async () => {
    const resolved = {
      ...devTask,
      state: "ESCALATED",
      requirement: "下单要预占库存",
      requirement_id: "req-20260813-001",
      intervention_resolved_at: "2026-01-02T00:00:00+00:00",
      intervention_successor_task_id: "ag-0002",
      can_run: false,
    } as const;
    tasksMock.mockResolvedValue([resolved]);
    taskMock.mockResolvedValue(resolved);
    requirementMock.mockResolvedValue({
      id: "req-20260813-001", title: "预占库存", text: "下单要预占库存", priority: 5,
      state: "in_progress", parked: "", attempts: 2,
      created_at: "2026-01-01T00:00:00+00:00", updated_at: "2026-01-02T00:00:00+00:00",
      parent_id: "", blocked_by: [], children_total: 0, children_delivered: 0,
      children: [], tree_tokens: 0,
      chain: [], total_tokens: 0,
    });
    render(<TaskCenter openId="ag-0001" onOpen={vi.fn()} onOpenRequirement={vi.fn()} />);

    const metrics = await screen.findByLabelText("任务状态统计");
    expect(within(within(metrics).getByText("异常").closest(".task-metric") as HTMLElement).getByText("0")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "修改需求并再试一次" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "标记已处理" })).not.toBeInTheDocument();
    expect(screen.getByText(/已创建后继任务 ag-0002/)).toBeInTheDocument();
  });

  it("renders no requirement row for a legacy task without one", async () => {
    taskMock.mockResolvedValue({ ...devTask, requirement: "下单要预占库存", can_run: false });

    render(<TaskCenter openId="ag-0001" onOpen={vi.fn()} />);

    await screen.findByText("需求原文");
    expect(screen.queryByText("所属需求")).not.toBeInTheDocument();
    expect(requirementMock).not.toHaveBeenCalled();
  });

  it("shows a start button only when the server says the task can run", async () => {
    // `can_run` 是服务端按状态机算好的——不猜哪些状态该有按钮,直接读这个字段。
    taskMock.mockResolvedValue({ ...devTask, requirement: "下单要预占库存", can_run: false });

    render(<TaskCenter openId="ag-0001" onOpen={vi.fn()} />);

    await screen.findByText("需求原文");
    expect(screen.queryByText("继续推进")).not.toBeInTheDocument();
  });

  it("does not offer another run after a task has escalated", async () => {
    taskMock.mockResolvedValue({
      ...devTask,
      state: "ESCALATED",
      can_run: true,
      requirement: "下单要预占库存",
      escalate_reason: "需要人工判断迁移策略",
    });

    render(<TaskCenter openId="ag-0001" onOpen={vi.fn()} />);

    expect(await screen.findByText("自动流程已停手,等待人工接管。")).toBeInTheDocument();
    expect(screen.queryByText(/开始执行|继续推进|重试推进/)).not.toBeInTheDocument();
    expect(screen.queryByText("执行中")).not.toBeInTheDocument();
  });

  it("presents the real ready-to-commit state and keeps it actionable", async () => {
    tasksMock.mockResolvedValue([{ ...devTask, state: "READY_TO_COMMIT" }]);
    taskMock.mockResolvedValue({ ...devTask, state: "READY_TO_COMMIT", requirement: "下单要预占库存" });

    render(<TaskCenter openId="ag-0001" onOpen={vi.fn()} />);

    const readyRow = await screen.findByRole("button", { name: /修一个下单的 bug/ });
    expect(within(readyRow).getAllByText("提交前检查")).toHaveLength(2);
    expect(screen.getByText("继续检查")).toBeInTheDocument();
    await openBoard();
    expect(screen.getByText("READY_TO_COMMIT")).toBeInTheDocument();
  });

  it("starting a task calls /run and disables the button until run_finished arrives", async () => {
    taskMock.mockResolvedValue({ ...devTask, requirement: "下单要预占库存" });
    runMock.mockResolvedValue({ ...devTask });
    // TaskCenter 自己的看板也订阅了一路(不带 `taskId`,用来刷新泳道计数)——
    // 只捕获详情面板那一路的回调,不然会被后挂上的那一路覆盖掉。
    const notified: Array<(notice: { task_id: string; kind: string }) => void> = [];
    subscribeMock.mockImplementation((onChange, taskId) => {
      if (taskId === "ag-0001") notified.push(onChange);
      return () => undefined;
    });

    render(<TaskCenter openId="ag-0001" onOpen={vi.fn()} />);
    await userEvent.click(await screen.findByText("继续推进"));

    expect(runMock).toHaveBeenCalledWith("ag-0001");
    expect(await screen.findByText("执行中…")).toBeInTheDocument();

    // 后台那一步跑完了,推送一条 `run_finished`——按钮应该松开,不用等用户手动刷新。
    notified[0]?.({ task_id: "ag-0001", kind: "run_finished" });
    expect(await screen.findByText("继续推进")).toBeInTheDocument();
  });

  it("shows a background run failure after run_finished instead of failing silently", async () => {
    const interrupted = {
      ...devTask,
      state: "MERGING",
      execution_status: "interrupted",
      requirement: "下单要预占库存",
    } as const;
    taskMock
      .mockResolvedValueOnce({ ...devTask, state: "MERGING", requirement: "下单要预占库存" })
      .mockResolvedValue(interrupted);
    logsMock
      .mockResolvedValueOnce({ items: [], next_cursor: null, total: 0 })
      .mockResolvedValue({
        items: [{
          line: 1,
          text: JSON.stringify({
            kind: "note",
            payload: { note: "推进失败: GitHub 仓库定位失败" },
          }),
        }],
        next_cursor: null,
        total: 1,
      });
    runMock.mockResolvedValue(interrupted);
    const notified: Array<(notice: { task_id: string; kind: string }) => void> = [];
    subscribeMock.mockImplementation((onChange, taskId) => {
      if (taskId === "ag-0001") notified.push(onChange);
      return () => undefined;
    });

    render(<TaskCenter openId="ag-0001" onOpen={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "继续合并" }));
    await act(async () => notified[0]?.({ task_id: "ag-0001", kind: "run_finished" }));

    expect(await screen.findByText(/推进失败:.*GitHub 仓库定位失败/)).toBeInTheDocument();
  });

  it("distinguishes a plan retry from a task that has never started", async () => {
    taskMock.mockResolvedValue({
      ...devTask,
      state: "CREATED",
      plan_retries: 1,
      execution_status: "retry_pending",
      requirement: "下单要预占库存",
    });

    render(<TaskCenter openId="ag-0001" onOpen={vi.fn()} />);

    expect(await screen.findByRole("button", { name: "重试需求解析" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "开始执行" })).not.toBeInTheDocument();
  });

  it("uses the server execution projection after a page refresh", async () => {
    taskMock.mockResolvedValue({
      ...devTask,
      state: "CREATED",
      execution_status: "running",
      requirement: "下单要预占库存",
    });

    render(<TaskCenter openId="ag-0001" onOpen={vi.fn()} />);

    expect(await screen.findByRole("button", { name: "执行中…" })).toBeDisabled();
    expect(screen.getAllByText("需求解析中").length).toBeGreaterThan(0);
    expect(screen.getByText("运行状态刚刚确认")).toBeInTheDocument();
  });

  it("polls after a local start even when the run-started SSE notice is lost", async () => {
    const waiting = {
      ...devTask,
      state: "CREATED",
      execution_status: "idle",
      requirement: "下单要预占库存",
    } as const;
    taskMock
      .mockResolvedValueOnce(waiting)
      .mockResolvedValue({ ...waiting, state: "COMPLETED", execution_status: "finished", can_run: false });
    runMock.mockResolvedValue({ ...devTask, state: "CREATED", execution_status: "running" });

    render(<TaskCenter openId="ag-0001" onOpen={vi.fn()} />);
    const start = await screen.findByRole("button", { name: "开始执行" });
    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(start);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(runMock).toHaveBeenCalledTimes(1);
    expect(taskMock).toHaveBeenCalledTimes(1);

    await act(async () => vi.advanceTimersByTimeAsync(10_000));

    expect(taskMock).toHaveBeenCalledTimes(2);
    await act(async () => vi.advanceTimersByTimeAsync(20_000));
    expect(taskMock).toHaveBeenCalledTimes(2);
  });

  it("polls running task facts as an SSE fallback and stops after terminal state", async () => {
    vi.useFakeTimers();
    tasksMock
      .mockResolvedValueOnce([{ ...devTask, execution_status: "running" }])
      .mockResolvedValueOnce([{ ...devTask, state: "COMPLETED", execution_status: "finished" }]);

    render(<TaskCenter openId={null} onOpen={vi.fn()} />);
    await vi.waitFor(() => expect(tasksMock).toHaveBeenCalledTimes(1));
    await act(async () => undefined);

    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(tasksMock).toHaveBeenCalledTimes(2);

    await act(async () => vi.advanceTimersByTimeAsync(20_000));
    expect(tasksMock).toHaveBeenCalledTimes(2);
  });

  it("offers continuation after the server reports an interrupted drive", async () => {
    tasksMock.mockResolvedValue([{ ...devTask, execution_status: "interrupted" }]);
    taskMock.mockResolvedValue({
      ...devTask,
      state: "CREATED",
      execution_status: "interrupted",
      requirement: "下单要预占库存",
    });

    render(<TaskCenter openId="ag-0001" onOpen={vi.fn()} />);

    expect((await screen.findAllByText("执行已中断")).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "继续执行" })).toBeInTheDocument();
    const waitingMetric = within(screen.getByLabelText("任务状态统计"))
      .getByText("等待中").closest(".task-metric");
    expect(within(waitingMetric as HTMLElement).getByText("1")).toBeInTheDocument();
  });

  it("renders a failed job as a failure instead of a completed stage", async () => {
    taskMock.mockResolvedValue({ ...devTask, requirement: "下单要预占库存" });
    logsMock.mockResolvedValue({
      items: [{
        line: 1,
        text: JSON.stringify({
          ts: "2026-01-01T00:00:00+00:00",
          actor: "decision-employee",
          kind: "job_finished",
          payload: { stage: "plan", ok: false, failure_detail: "输出不合契约" },
        }),
      }],
      next_cursor: null,
      total: 1,
    });

    render(<TaskCenter openId="ag-0001" onOpen={vi.fn()} />);

    expect(await screen.findByText("需求解析失败：输出不合契约")).toBeInTheDocument();
  });

  it("surfaces a run error and re-enables the button", async () => {
    taskMock.mockResolvedValue({ ...devTask, requirement: "下单要预占库存" });
    runMock.mockRejectedValue(new ApiError(409, "ag-0001 已经在推进中"));

    render(<TaskCenter openId="ag-0001" onOpen={vi.fn()} />);
    await userEvent.click(await screen.findByText("继续推进"));

    expect(await screen.findByText(/ag-0001 已经在推进中/)).toBeInTheDocument();
    expect(await screen.findByText("继续推进")).toBeInTheDocument();
  });

  it("shows a compact execution summary and moves raw blocks into a drawer", async () => {
    taskMock.mockResolvedValue({ ...devTask, requirement: "下单要预占库存" });
    traceMock.mockResolvedValue({
      task_id: "ag-0001",
      stages: [
        {
          stage: "plan",
          number: 1,
          blocks: [
            { seq: 1, kind: "thinking", text: "我来先看看当前项目的结构。", detail: {} },
            { seq: 2, kind: "tool-step", text: "Read pyproject.toml", detail: { name: "Read" } },
            { seq: 3, kind: "tool-step", text: "Grep reservation", detail: { name: "Grep" } },
            { seq: 4, kind: "tool-step", text: "匹配到 reservation.py", detail: { tool_use_id: "call-grep" } },
          ],
        },
        { stage: "unit-gate", number: 2, blocks: [] },
      ],
    });

    render(<TaskCenter openId="ag-0001" onOpen={vi.fn()} />);

    await userEvent.click(await screen.findByRole("tab", { name: "执行过程" }));
    expect(await screen.findByText("需求解析")).toBeInTheDocument();
    expect(screen.getByText("阶段结果")).toBeInTheDocument();
    expect(screen.getByText("读取文件").parentElement).toHaveTextContent("1");
    expect(screen.getByText("搜索代码").parentElement).toHaveTextContent("1");
    expect(screen.getByText("2 次工具调用")).toBeInTheDocument();
    expect(screen.queryByText(/条执行信息/)).not.toBeInTheDocument();
    expect(screen.queryByText("我来先看看当前项目的结构。")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "查看详细活动" }));
    expect(screen.getByText("Read pyproject.toml")).toBeVisible();
    expect(screen.getByText("Grep reservation")).toBeVisible();
    expect(screen.queryByText("匹配到 reservation.py")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "查看原始日志" }));
    expect(screen.getByRole("dialog", { name: "需求解析原始日志" })).toBeInTheDocument();
    expect(screen.queryByText("我来先看看当前项目的结构。")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /思考过程/ }));
    expect(screen.getByText("我来先看看当前项目的结构。")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "关闭原始日志" }));
    expect(screen.queryByRole("dialog", { name: "需求解析原始日志" })).not.toBeInTheDocument();
    expect(screen.getByText("单元测试门禁")).toBeInTheDocument();
    expect(screen.getByText("确定性执行,没有对话轨迹")).toBeInTheDocument();
  });

  it("says so when no job has run yet, instead of an empty box", async () => {
    taskMock.mockResolvedValue({ ...devTask, requirement: "下单要预占库存" });
    traceMock.mockResolvedValue({ task_id: "ag-0001", stages: [] });

    render(<TaskCenter openId="ag-0001" onOpen={vi.fn()} />);

    await userEvent.click(await screen.findByRole("tab", { name: "执行过程" }));
    expect(await screen.findByText("还没有 Job 跑过")).toBeInTheDocument();
  });

  it("surfaces a trace fetch failure instead of staying blank", async () => {
    taskMock.mockResolvedValue({ ...devTask, requirement: "下单要预占库存" });
    traceMock.mockRejectedValue(new ApiError(500, "读不到执行轨迹"));

    render(<TaskCenter openId="ag-0001" onOpen={vi.fn()} />);

    await userEvent.click(await screen.findByRole("tab", { name: "执行过程" }));
    expect(await screen.findByText(/读不到执行轨迹/)).toBeInTheDocument();
  });

  it("counts each lane", async () => {
    genomeMock.mockResolvedValue({ items: [genomeTask] });

    render(<TaskCenter openId={null} onOpen={vi.fn()} />);

    await openBoard();
    const lane = (await screen.findByText("DEEP_READ")).closest("h4");
    expect(within(lane as HTMLElement).getByText("1")).toBeInTheDocument();
  });

  it("shows structured lifecycle events before offering raw JSON", async () => {
    taskMock.mockResolvedValue({ ...devTask, requirement: "下单要预占库存" });
    logsMock.mockResolvedValue({
      items: [
        {
          line: 1,
          text: JSON.stringify({
            ts: "2026-01-01T00:00:00+00:00",
            actor: "orchestrator",
            kind: "job_started",
            payload: { stage: "develop" },
          }),
        },
      ],
      next_cursor: null,
      total: 1,
    });

    render(<TaskCenter openId="ag-0001" onOpen={vi.fn()} />);
    await userEvent.click(await screen.findByRole("tab", { name: "事件日志" }));

    expect(await screen.findByText("开始执行开发阶段")).toBeInTheDocument();
    expect(screen.queryByText(/\"kind\":\"job_started\"/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "查看原始 JSON" }));
    expect(screen.getByText(/\"kind\":\"job_started\"/)).toBeInTheDocument();
  });

  it("lists task artifacts in their own tab", async () => {
    taskMock.mockResolvedValue({ ...devTask, requirement: "下单要预占库存" });
    artifactsMock.mockResolvedValue({
      items: [{ path: "artifacts/02-develop/result.json", size: 128 }],
    });

    render(<TaskCenter openId="ag-0001" onOpen={vi.fn()} />);
    await userEvent.click(await screen.findByRole("tab", { name: "产物" }));

    expect(await screen.findByText("artifacts/02-develop/result.json")).toBeInTheDocument();
    expect(screen.getByText("128 B")).toBeInTheDocument();
  });
});
