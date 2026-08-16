import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { GenomeTaskDetail } from "./GenomeTaskDetail";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      genomeTask: vi.fn(),
      genomeProgress: vi.fn(),
      logs: vi.fn(),
      cancelGenomeTask: vi.fn(),
      runGenomeTask: vi.fn(),
      genomeTrace: vi.fn(),
      reinitModules: vi.fn(),
      resolveGenomeIntervention: vi.fn(),
    },
  };
});
vi.mock("../api/live", () => ({ subscribe: vi.fn(() => () => undefined) }));
vi.mock("./BoundaryGate", () => ({
  BoundaryGate: ({ taskId }: { taskId: string }) => <div>闸门 {taskId}</div>,
}));

const { api, ApiError } = await import("../api/client");
const taskMock = vi.mocked(api.genomeTask);
const progressMock = vi.mocked(api.genomeProgress);
const logsMock = vi.mocked(api.logs);
const cancelMock = vi.mocked(api.cancelGenomeTask);
const runMock = vi.mocked(api.runGenomeTask);
const traceMock = vi.mocked(api.genomeTrace);
const reinitMock = vi.mocked(api.reinitModules);
const resolveInterventionMock = vi.mocked(api.resolveGenomeIntervention);

const task = {
  id: "gn-0001",
  title: "初始化认知",
  kind: "init",
  origin: "human",
  state: "DEEP_READ",
  subject: null,
  source_task_id: null,
  failure_reason: null,
  tokens_used: 12_345,
  budget_tokens: null,
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
  overdue: false,
};

beforeEach(() => {
  taskMock.mockReset();
  progressMock.mockReset();
  logsMock.mockReset();
  cancelMock.mockReset();
  runMock.mockReset();
  traceMock.mockReset();
  traceMock.mockResolvedValue({ task_id: "gn-0001", stages: [] });
  reinitMock.mockReset();
  resolveInterventionMock.mockReset();
  taskMock.mockResolvedValue(structuredClone(task));
  progressMock.mockResolvedValue({ task_id: "gn-0001", started: false, modules: [] });
  logsMock.mockResolvedValue({ items: [], next_cursor: null, total: 0 });
  reinitMock.mockResolvedValue({ items: [] });
});

describe("执行轨迹", () => {
  it("shows what the employee actually did, grouped by module", async () => {
    // 深读的员工读了什么、调了什么工具——不必去服务器上翻 job-attempt 日志文件。
    traceMock.mockResolvedValue({
      task_id: "gn-0001",
      stages: [
        {
          stage: "order-service",
          number: 1,
          blocks: [
            { seq: 1, kind: "text", text: "先读 order-service 的代码结构。" },
            { seq: 2, kind: "tool-step", text: "Read pyproject.toml" },
          ],
        },
      ],
    });
    render(<GenomeTaskDetail id="gn-0001" onClose={vi.fn()} onChanged={vi.fn()} />);
    await screen.findByText(/初始化认知/);
    await userEvent.click(screen.getByRole("tab", { name: "执行过程" }));

    expect(await screen.findByText("员工干了什么")).toBeInTheDocument();
    expect(screen.getByText(/1 条执行信息 · 1 次工具调用|2 条执行信息 · 1 次工具调用/)).toBeInTheDocument();
    expect(screen.getByText(/先读 order-service 的代码结构/)).toBeInTheDocument();
  });

  it("renders nothing extra when no job has run yet", async () => {
    // 空态由「逐模块进度」承担;轨迹段整个不出现,不摆一个空壳解释同一件事。
    render(<GenomeTaskDetail id="gn-0001" onClose={vi.fn()} onChanged={vi.fn()} />);
    await screen.findByText(/初始化认知/);
    await userEvent.click(screen.getByRole("tab", { name: "执行过程" }));

    expect(screen.queryByText("员工干了什么")).not.toBeInTheDocument();
  });
});

describe("继续推进", () => {
  it("resumes a stranded deep-read from the detail page", async () => {
    // 驱动断了(进程重启)时的接回入口——不必去敲 `agctl knowledge run`。
    runMock.mockResolvedValue(structuredClone(task) as never);
    render(<GenomeTaskDetail id="gn-0001" onClose={vi.fn()} onChanged={vi.fn()} />);

    await userEvent.click(await screen.findByRole("button", { name: /继续推进/ }));

    expect(runMock).toHaveBeenCalledWith("gn-0001");
  });

  it("offers no resume button while the task waits for the gate", async () => {
    // 待确认不该有它:推它等于替人回答。终态同理。
    taskMock.mockResolvedValue({ ...structuredClone(task), state: "AWAITING_CONFIRMATION" });
    render(<GenomeTaskDetail id="gn-0001" onClose={vi.fn()} onChanged={vi.fn()} />);
    await screen.findByText(/初始化认知/);

    expect(screen.queryByRole("button", { name: /继续推进/ })).not.toBeInTheDocument();
  });

  it("offers no resume button on a settled task", async () => {
    taskMock.mockResolvedValue({ ...structuredClone(task), state: "SUBMITTED" });
    render(<GenomeTaskDetail id="gn-0001" onClose={vi.fn()} onChanged={vi.fn()} />);
    await screen.findByText(/初始化认知/);

    expect(screen.queryByRole("button", { name: /继续推进/ })).not.toBeInTheDocument();
  });
});

describe("GenomeTaskDetail", () => {
  it("does not render the blocks that do not apply to a genome task", async () => {
    // **否定断言。** 裁剪是不渲染,不是渲染空态:空态说的是"这里本该有东西但现在没有",
    // 会让人以为一个基因组任务本该产出代码而没产出。
    render(<GenomeTaskDetail id="gn-0001" onClose={vi.fn()} onChanged={vi.fn()} />);
    await screen.findByText(/初始化认知/);

    expect(screen.queryByText(/diff/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/风险/)).not.toBeInTheDocument();
    expect(screen.queryByText(/审批/)).not.toBeInTheDocument();
    expect(screen.queryByText(/需求原文/)).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "概览" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "执行过程" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "产物" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "事件日志" })).toBeInTheDocument();
  });

  it("says the deep read has not started rather than showing an empty list", async () => {
    // "还没开始"与"零个模块"不是一回事。
    render(<GenomeTaskDetail id="gn-0001" onClose={vi.fn()} onChanged={vi.fn()} />);

    await userEvent.click(await screen.findByRole("tab", { name: "执行过程" }));
    expect(await screen.findByText(/深读还没开始/)).toBeInTheDocument();
  });

  it("shows each module's status and why a failed one failed", async () => {
    progressMock.mockResolvedValue({
      task_id: "gn-0001",
      started: true,
      pull_requests: [],
      modules: [
        { module_id: "order", status: "done", detail: "", duration_s: 0 },
        { module_id: "pay", status: "failed", detail: "Job 超时", duration_s: 0 },
        { module_id: "ship", status: "pending", detail: "", duration_s: 0 },
      ],
    });

    render(<GenomeTaskDetail id="gn-0001" onClose={vi.fn()} onChanged={vi.fn()} />);

    await userEvent.click(await screen.findByRole("tab", { name: "执行过程" }));
    expect(await screen.findByText("Job 超时")).toBeInTheDocument();
    expect(screen.getByText("排队中")).toBeInTheDocument();
  });

  it("rebuilds exactly the failed modules", async () => {
    // 不必回到命令行——而且不是"重跑全部",那既贵又会把已经校对过的搅一遍。
    progressMock.mockResolvedValue({
      task_id: "gn-0001",
      started: true,
      pull_requests: [],
      modules: [
        { module_id: "order", status: "done", detail: "", duration_s: 0 },
        { module_id: "pay", status: "failed", detail: "Job 超时", duration_s: 0 },
      ],
    });
    render(<GenomeTaskDetail id="gn-0001" onClose={vi.fn()} onChanged={vi.fn()} />);

    await userEvent.click(await screen.findByRole("tab", { name: "执行过程" }));
    await userEvent.click(await screen.findByRole("button", { name: /重建这 1 个/ }));

    expect(reinitMock).toHaveBeenCalledWith(["pay"]);
  });

  it("shows how long each module took", async () => {
    // "哪个模块特别慢"是下一次调预算与并发时唯一有用的那条线索。
    progressMock.mockResolvedValue({
      task_id: "gn-0001",
      started: true,
      pull_requests: [],
      modules: [{ module_id: "order", status: "done", detail: "", duration_s: 12.5 }],
    });

    render(<GenomeTaskDetail id="gn-0001" onClose={vi.fn()} onChanged={vi.fn()} />);

    await userEvent.click(await screen.findByRole("tab", { name: "执行过程" }));
    expect(await screen.findByText("12.5s")).toBeInTheDocument();
  });

  it("links to the knowledge pull requests this task produced", async () => {
    // **是指针不是内容**——改成了什么去那个 PR 里看,事件面本来就不存内容。
    progressMock.mockResolvedValue({
      task_id: "gn-0001",
      started: true,
      modules: [],
      pull_requests: ["42"],
    });

    render(<GenomeTaskDetail id="gn-0001" onClose={vi.fn()} onChanged={vi.fn()} />);

    await userEvent.click(await screen.findByRole("tab", { name: "产物" }));
    expect(await screen.findByRole("link", { name: /知识 PR #42/ })).toHaveAttribute("href", "#/pulls/42");
  });

  it("shows the token cost", async () => {
    render(<GenomeTaskDetail id="gn-0001" onClose={vi.fn()} onChanged={vi.fn()} />);

    expect(await screen.findByText(/12,345 tokens/)).toBeInTheDocument();
  });

  it("opens the gate inside the detail when the task is waiting", async () => {
    taskMock.mockResolvedValue({ ...task, state: "AWAITING_CONFIRMATION" });

    render(<GenomeTaskDetail id="gn-0001" onClose={vi.fn()} onChanged={vi.fn()} />);

    expect(await screen.findByText("闸门 gn-0001")).toBeInTheDocument();
  });

  it("cancels the task after confirmation", async () => {
    // 取消也是放开那个模块的唯一办法:停在待确认的任务不是终态。
    vi.spyOn(window, "confirm").mockReturnValue(true);
    cancelMock.mockResolvedValue({ ...task, state: "CANCELLED" });
    const onChanged = vi.fn();
    render(<GenomeTaskDetail id="gn-0001" onClose={vi.fn()} onChanged={onChanged} />);

    await userEvent.click(await screen.findByRole("button", { name: "更多" }));
    await userEvent.click(await screen.findByRole("button", { name: "取消任务" }));

    expect(cancelMock).toHaveBeenCalledWith("gn-0001");
  });

  it("shows the fetch error instead of an empty page", async () => {
    taskMock.mockRejectedValue(new ApiError(404, "没有这个基因组任务"));

    render(<GenomeTaskDetail id="gn-0001" onClose={vi.fn()} onChanged={vi.fn()} />);

    expect(await screen.findByText(/拉不到基因组任务/)).toHaveTextContent("没有这个基因组任务");
  });

  it("degrades a progress failure to a panel note, not a dead page", async () => {
    progressMock.mockRejectedValue(new ApiError(500, "progress unreadable"));

    render(<GenomeTaskDetail id="gn-0001" onClose={vi.fn()} onChanged={vi.fn()} />);

    await userEvent.click(await screen.findByRole("tab", { name: "执行过程" }));
    expect(await screen.findByText(/拉不到进度/)).toBeInTheDocument();
    // 任务本体照常渲染。
    expect(screen.getByText(/初始化认知/)).toBeInTheDocument();
  });

  it("does not turn a system-origin failure into a human todo", async () => {
    taskMock.mockResolvedValue({
      ...task,
      origin: "system",
      state: "FAILED",
      failure_reason: "上游数据缺失",
    });

    render(<GenomeTaskDetail id="gn-0001" onClose={vi.fn()} onChanged={vi.fn()} />);

    expect(await screen.findByText(/系统任务失败已收口/)).toBeInTheDocument();
    expect(screen.getByText("无需人工操作")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "取消任务" })).not.toBeInTheDocument();
  });

  it("lets a human-origin failure leave the attention queue after it is handled", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    taskMock.mockResolvedValue({
      ...task,
      state: "FAILED",
      failure_reason: "上游数据缺失",
    });
    resolveInterventionMock.mockResolvedValue({} as never);
    const onClose = vi.fn();
    const onChanged = vi.fn();

    render(<GenomeTaskDetail id="gn-0001" onClose={onClose} onChanged={onChanged} />);
    await userEvent.click(await screen.findByRole("button", { name: "标记已处理" }));

    expect(resolveInterventionMock).toHaveBeenCalledWith("gn-0001");
    expect(onChanged).toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });
});
