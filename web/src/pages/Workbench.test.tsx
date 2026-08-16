import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Workbench } from "./Workbench";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { ...actual.api, tasks: vi.fn(), costs: vi.fn(), genomeTasks: vi.fn() } };
});
vi.mock("../api/live", () => ({ subscribe: vi.fn(() => () => undefined) }));

const { api, ApiError } = await import("../api/client");
const { subscribe } = await import("../api/live");
const tasksMock = vi.mocked(api.tasks);
const costsMock = vi.mocked(api.costs);
const genomeMock = vi.mocked(api.genomeTasks);
const subscribeMock = vi.mocked(subscribe);

function genomeTask(overrides: Record<string, unknown> = {}) {
  return {
    id: "gn-0001",
    title: "初始化 order 的认知",
    kind: "init",
    origin: "human",
    state: "AWAITING_CONFIRMATION",
    subject: null,
    source_task_id: null,
    failure_reason: null,
    tokens_used: 0,
    budget_tokens: null,
    created_at: "2026-01-01T00:00:00+00:00",
    updated_at: "2026-01-01T00:00:00+00:00",
    overdue: false,
    ...overrides,
  } as Awaited<ReturnType<typeof api.genomeTasks>>["items"][number];
}

function task(overrides: Partial<Awaited<ReturnType<typeof api.tasks>>[number]> = {}) {
  return {
    id: "ag-20260809-001",
    title: "下单预占库存",
    state: "DEVELOPING",
    priority: 5,
    fix_rounds: 0,
    tokens_used: 120,
    branch: null,
    risk_level: null,
    escalate_reason: null,
    ...overrides,
  } as Awaited<ReturnType<typeof api.tasks>>[number];
}

beforeEach(() => {
  tasksMock.mockReset();
  costsMock.mockReset();
  costsMock.mockResolvedValue({ by_employee: [], by_task: [], total_tokens: 0 });
  genomeMock.mockReset();
  genomeMock.mockResolvedValue({ items: [] });
  subscribeMock.mockReset();
  subscribeMock.mockReturnValue(() => undefined);
});

describe("Workbench", () => {
  it("converges to the latest task facts after a shared invalidation", async () => {
    let invalidate: (() => void) | undefined;
    subscribeMock.mockImplementation((onChange) => {
      invalidate = () => onChange({ task_id: "ag-1", kind: "task_changed" });
      return () => undefined;
    });
    tasksMock.mockResolvedValue([task({ title: "开发中", state: "DEVELOPING" })]);
    render(<Workbench onOpen={vi.fn()} onGo={vi.fn()} />);
    expect(await screen.findByText("开发中")).toBeInTheDocument();

    tasksMock.mockResolvedValue([task({ title: "待审批", state: "REVIEWING" })]);
    invalidate?.();

    expect((await screen.findAllByText("待审批")).length).toBeGreaterThan(0);
  });

  it("recovers after a transient live reload failure", async () => {
    let invalidate: (() => void) | undefined;
    subscribeMock.mockImplementation((onChange) => {
      invalidate = () => onChange({ task_id: "ag-1", kind: "task_changed" });
      return () => undefined;
    });
    tasksMock.mockRejectedValueOnce(new ApiError(503, "temporary"));
    render(<Workbench onOpen={vi.fn()} onGo={vi.fn()} />);
    expect(await screen.findByText(/temporary/)).toBeInTheDocument();

    tasksMock.mockResolvedValue([task({ title: "已恢复" })]);
    invalidate?.();

    expect(await screen.findByText("已恢复")).toBeInTheDocument();
    expect(screen.queryByText(/temporary/)).not.toBeInTheDocument();
  });

  it("counts escalated tasks that the list endpoint now returns", async () => {
    // 回归测试:`GET /tasks` 曾经复用调度队列的 `open_tasks`,把 ESCALATED 一起滤掉,
    // 于是「已升级人工」这张卡片永远是 0——最需要人看的数字恰好是唯一看不到的。
    tasksMock.mockResolvedValue([task({ state: "ESCALATED", escalate_reason: "环境缺件" })]);

    render(<Workbench onOpen={vi.fn()} onGo={vi.fn()} />);

    expect(await screen.findByText("环境缺件")).toBeInTheDocument();
  });

  it("takes the token total from the cost report, not from the visible task list", async () => {
    // 任务列表只有"还需要人过问的"任务,已完成的不在里面——而那才是 token 消耗的大头。
    // 从列表加出来的话,这个数字会在任务完成的瞬间不升反降。
    tasksMock.mockResolvedValue([task({ tokens_used: 120 })]);
    costsMock.mockResolvedValue({ by_employee: [], by_task: [], total_tokens: 98_765 });

    render(<Workbench onOpen={vi.fn()} onGo={vi.fn()} />);

    expect(await screen.findByText("98,765")).toBeInTheDocument();
    expect(screen.queryByText("120")).not.toBeInTheDocument();
  });

  it("shows a dash rather than zero when the cost report fails to load", async () => {
    // 显示 0 的话,"还没花过 token"和"这个数字没拿到"在界面上是同一个样子。
    tasksMock.mockResolvedValue([task()]);
    costsMock.mockRejectedValue(new ApiError(500, "backend unreachable"));

    render(<Workbench onOpen={vi.fn()} onGo={vi.fn()} />);

    // 只盯 token 那张卡片:没有升级人工的任务时,「已升级人工」卡片也会显示「—」。
    const label = await screen.findByText("token 消耗");
    expect(label.nextElementSibling).toHaveTextContent("—");
  });

  it("counts awaiting-confirmation separately from escalated", async () => {
    // **这是本页最重要的一条。** 两者都是"机器停手等人",但待确认是健康的、计划内的。
    // 合成一个数字的话,一屏之内最刺眼的那个计数会被一批健康任务顶高,而真正出事的那个
    // 被淹没——正是那条区分要防的事,方向相反。
    tasksMock.mockResolvedValue([task({ state: "ESCALATED", escalate_reason: "环境缺件" })]);
    genomeMock.mockResolvedValue({ items: [genomeTask(), genomeTask({ id: "gn-0002" })] });

    render(<Workbench onOpen={vi.fn()} onGo={vi.fn()} />);

    const awaiting = await screen.findByText("待我确认", { selector: ".lab" });
    expect(awaiting.nextElementSibling).toHaveTextContent("2");
    const escalated = screen.getByText("已升级人工");
    expect(escalated.nextElementSibling).toHaveTextContent("1");
  });

  it("puts awaiting tasks in the todo queue and escalated ones nowhere near it", async () => {
    tasksMock.mockResolvedValue([
      task({ id: "ag-bad", title: "炸了的任务", state: "ESCALATED", escalate_reason: "环境缺件" }),
    ]);
    genomeMock.mockResolvedValue({ items: [genomeTask({ title: "等我确认的" })] });

    render(<Workbench onOpen={vi.fn()} onGo={vi.fn()} />);

    const queue = (await screen.findByText("待我确认", { selector: "h3" })).closest(".card");
    expect(queue).toHaveTextContent("等我确认的");
    // 否定断言:异常任务不该出现在待办队列里。
    expect(queue).not.toHaveTextContent("炸了的任务");
  });

  it("surfaces a development task waiting for split confirmation", async () => {
    const onOpen = vi.fn();
    tasksMock.mockResolvedValue([
      task({
        id: "ag-split",
        title: "等待确认拆分的研发任务",
        state: "CREATED",
        pending_todo: { id: "todo-split", kind: "split", assignee: "decision-employee" },
      }),
    ]);

    render(<Workbench onOpen={onOpen} onGo={vi.fn()} />);

    const label = await screen.findByText("待我确认", { selector: ".lab" });
    expect(label.nextElementSibling).toHaveTextContent("1");
    const queue = screen.getByText("待我确认", { selector: "h3" }).closest(".card")!;
    const row = Array.from(queue.querySelectorAll("tr")).find((candidate) =>
      candidate.textContent?.includes("等待确认拆分的研发任务"),
    );
    expect(row).toHaveTextContent("待确认拆分");
    fireEvent.click(row!);
    expect(onOpen).toHaveBeenCalledWith("ag-split");
  });

  it("marks a gate nobody has answered for a long time", async () => {
    // 标记不判死:一个等人的健康任务不该因为人休假而失败,但它不该被无声地遗忘。
    genomeMock.mockResolvedValue({ items: [genomeTask({ overdue: true })] });
    tasksMock.mockResolvedValue([]);

    render(<Workbench onOpen={vi.fn()} onGo={vi.fn()} />);

    expect(await screen.findByText("等太久了")).toBeInTheDocument();
  });
});
