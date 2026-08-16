import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import {
  api,
  setWorkspace,
  type GenomeTaskSummary,
  type RequirementDetail,
  type RequirementSummary,
  type TaskDetail,
  type TaskSummary,
} from "./api/client";
import { subscribe } from "./api/live";

vi.mock("./api/client", async () => {
  const actual = await vi.importActual<typeof import("./api/client")>("./api/client");
  return {
    ...actual,
    api: {
      ...actual.api,
      tasks: vi.fn(),
      task: vi.fn(),
      logs: vi.fn(),
      workspaces: vi.fn(),
      costs: vi.fn(),
      genomeTasks: vi.fn(),
      auditEvents: vi.fn(),
      topologies: vi.fn(),
      submit: vi.fn(),
      requirements: vi.fn(),
      requirement: vi.fn(),
      run: vi.fn(),
      createWorkspace: vi.fn(),
      knowledgeInit: vi.fn(),
    },
  };
});

vi.mock("./api/live", () => ({ subscribe: vi.fn(() => () => undefined) }));

const mocked = {
  tasks: vi.mocked(api.tasks), task: vi.mocked(api.task), logs: vi.mocked(api.logs),
  workspaces: vi.mocked(api.workspaces), costs: vi.mocked(api.costs), genomeTasks: vi.mocked(api.genomeTasks),
  auditEvents: vi.mocked(api.auditEvents), topologies: vi.mocked(api.topologies), submit: vi.mocked(api.submit),
  requirements: vi.mocked(api.requirements), requirement: vi.mocked(api.requirement), run: vi.mocked(api.run),
  createWorkspace: vi.mocked(api.createWorkspace), knowledgeInit: vi.mocked(api.knowledgeInit),
};
const subscribeMock = vi.mocked(subscribe);

const task = (state: string, id: string): TaskSummary => ({
  id,
  title: "一个任务",
  state,
  priority: 5,
  fix_rounds: 0,
  plan_retries: 0,
  needs_itest: "UNDECIDED",
  itest_override: "auto",
  mode: "autonomous",
  topology: "",
  tokens_used: 0,
  can_run: false,
  execution_status: "idle",
  risk_level: null,
  escalate_reason: null,
  created_at: "2026-08-10T10:00:00Z",
  updated_at: "2026-08-10T10:00:00Z",
});

const genomeTask = (state: string, id: string): GenomeTaskSummary => ({
  id,
  state,
  kind: "mount",
  origin: "workspace",
  overdue: false,
  title: "初始化项目知识",
  tokens_used: 0,
  created_at: "2026-08-10T10:00:00Z",
  updated_at: "2026-08-10T10:00:00Z",
});

beforeEach(() => {
  vi.clearAllMocks();
  window.history.replaceState(null, "", "#/work");
  setWorkspace("");
  mocked.tasks.mockResolvedValue([task("REVIEWING", "ag-1"), task("DEVELOPING", "ag-2")]);
  mocked.task.mockResolvedValue({
    ...task("DEVELOPING", "ag-1"),
    requirement: "修复路由",
    requirement_id: null,
    branch: "task/ag-1",
    can_run: false,
    execution_status: "idle",
    plan_retries: 0,
    mode: "autonomous",
  });
  mocked.logs.mockResolvedValue({ items: [], next_cursor: null, total: 0 });
  mocked.workspaces.mockResolvedValue({ items: ["mall"] });
  mocked.costs.mockResolvedValue({ by_employee: [], by_task: [], total_tokens: 0 });
  mocked.genomeTasks.mockResolvedValue({ items: [] });
  mocked.auditEvents.mockResolvedValue({ items: [] });
  mocked.topologies.mockResolvedValue({ default: "single", options: [] });
  mocked.requirements.mockResolvedValue([]);
  mocked.run.mockResolvedValue(task("CREATED", "ag-flow"));
  mocked.knowledgeInit.mockResolvedValue(genomeTask("QUEUED", "gn-init"));
  subscribeMock.mockReset();
  subscribeMock.mockReturnValue(() => undefined);
});

describe("URL 导航", () => {
  it("opens a Dev task directly from a shareable URL", async () => {
    window.history.replaceState(null, "", "#/tasks/ag-1");

    render(<App />);

    expect(await screen.findByText("task/ag-1")).toBeInTheDocument();
    expect(mocked.task).toHaveBeenCalledWith("ag-1");
  });

  it("keeps submit → requirement → task → live stage → refresh on one URL-driven flow", async () => {
    const listeners: Array<(notice: { task_id: string; kind: string }) => void> = [];
    subscribeMock.mockImplementation((onChange) => {
      listeners.push(onChange);
      return () => undefined;
    });
    setWorkspace("mall");
    window.history.replaceState(null, "", "#/submit");
    const flowTask = {
      ...task("CREATED", "ag-flow"),
      requirement: "验证完整主流程",
      requirement_id: "req-flow",
      priority: 5,
      branch: null,
      can_run: true,
      execution_status: "idle",
      plan_retries: 0,
      mode: "autonomous",
      topology: "",
      needs_itest: "UNDECIDED",
    } satisfies TaskDetail;
    mocked.submit.mockResolvedValue(flowTask);
    mocked.requirements.mockResolvedValue([{ id: "req-flow", title: "主流程", text: "验证完整主流程", priority: 5, state: "in_progress", parked: "", attempts: 1, parent_id: "", blocked_by: [], children_total: 0, children_delivered: 0, created_at: "2026-08-14T10:00:00Z", updated_at: "2026-08-14T10:00:00Z" } satisfies RequirementSummary]);
    mocked.requirement.mockResolvedValue({ id: "req-flow", title: "主流程", text: "验证完整主流程", priority: 5, state: "in_progress", parked: "", attempts: 1, parent_id: "", blocked_by: [], children_total: 0, children_delivered: 0, total_tokens: 0, children: [], tree_tokens: 0, created_at: "2026-08-14T10:00:00Z", updated_at: "2026-08-14T10:00:00Z", chain: [{ id: "ag-flow", state: "CREATED", execution_status: "idle", escalate_reason: null, tokens_used: 0, created_at: "2026-08-14T10:00:00Z" }] } satisfies RequirementDetail);
    mocked.task.mockResolvedValue(flowTask);

    const view = render(<App />);
    await userEvent.type(await screen.findByPlaceholderText(/写清楚/), "验证完整主流程");
    await userEvent.click(screen.getByRole("button", { name: "提交需求" }));
    await waitFor(() => expect(window.location.hash).toBe("#/requirements/req-flow"));
    await userEvent.click(await screen.findByRole("link", { name: "ag-flow" }));
    await waitFor(() => expect(window.location.hash).toBe("#/tasks/ag-flow"));
    await userEvent.click(await screen.findByRole("button", { name: "开始执行" }));
    expect(screen.getByRole("button", { name: "执行中…" })).toBeDisabled();

    mocked.task.mockResolvedValue({ ...flowTask, state: "DEVELOPING", execution_status: "running" });
    await act(async () => listeners.forEach((notify) => notify({ task_id: "ag-flow", kind: "task_changed" })));
    expect((await screen.findAllByText("开发中")).length).toBeGreaterThan(0);

    view.unmount();
    render(<App />);
    expect(window.location.hash).toBe("#/tasks/ag-flow");
    expect(await screen.findByRole("heading", { name: "一个任务" })).toBeInTheDocument();
  });

  it("keeps the address and visible page together across navigation history", async () => {
    render(<App />);
    await screen.findByText("mall");

    screen.getByRole("button", { name: /任务中心/ }).click();
    expect(window.location.hash).toBe("#/tasks");
    expect(await screen.findByRole("heading", { name: "任务中心" })).toBeInTheDocument();

    window.history.pushState(null, "", "#/requirements");
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    expect(await screen.findByRole("heading", { name: "需求管理" })).toBeInTheDocument();
  });

  it("shows an actionable error for an invalid deep link", async () => {
    window.history.replaceState(null, "", "#/somewhere-that-does-not-exist");

    render(<App />);

    await waitFor(() => expect(screen.getByText("这个控制台地址无效")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "返回工作台" })).toBeInTheDocument();
  });

  it("leaves a project result route when the user switches workspace", async () => {
    setWorkspace("a");
    window.history.replaceState(null, "", "#/project-created/a");
    mocked.workspaces.mockResolvedValue({
      items: ["a", "b"],
      entries: [
        { name: "a", initializing: false },
        { name: "b", initializing: false },
      ],
    });

    render(<App />);
    await screen.findByRole("heading", { name: /项目 a/ });
    await userEvent.selectOptions(screen.getByLabelText("切换项目"), "b");

    await waitFor(() => expect(window.location.hash).toBe("#/work"));
    expect(screen.getByLabelText("切换项目")).toHaveValue("b");
  });

  it("shows an actionable error for a missing project result", async () => {
    window.history.replaceState(null, "", "#/project-created/missing");

    render(<App />);

    expect(await screen.findByText("找不到项目 missing")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "返回工作台" })).toBeInTheDocument();
  });

  it("does not hide a missing project result behind the zero-project onboarding", async () => {
    mocked.workspaces.mockResolvedValue({ items: [], entries: [] });
    window.history.replaceState(null, "", "#/project-created/missing");

    render(<App />);

    await waitFor(() => expect(screen.getByText("找不到项目 missing")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "创建第一个项目" })).toBeInTheDocument();
  });

  it("creates a project once, switches workspace, keeps the result route and starts init", async () => {
    mocked.workspaces
      .mockResolvedValueOnce({ items: ["mall"] })
      .mockResolvedValue({ items: ["mall", "shop"], entries: [{ name: "mall", initializing: false }, { name: "shop", initializing: true }] });
    mocked.createWorkspace.mockResolvedValue({ name: "shop", mount_task_id: "gn-mount", adopted: false });
    mocked.genomeTasks.mockResolvedValue({ items: [genomeTask("SCANNING", "gn-mount")] });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "＋ 新项目" }));
    await userEvent.type(screen.getByPlaceholderText(/切换器里的名字/), "shop");
    await userEvent.type(
      screen.getByPlaceholderText(/agentgenome-workspace/),
      "https://example.test/shop-workspace.git",
    );
    await userEvent.type(screen.getByPlaceholderText(/order-service/), "https://example.test/shop.git");
    await userEvent.click(screen.getByRole("button", { name: "创建项目" }));

    await waitFor(() => expect(window.location.hash).toBe("#/project-created/shop"));
    expect(screen.getByLabelText("切换项目")).toHaveValue("shop");
    expect(await screen.findByText("gn-mount")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "发起知识初始化" }));
    expect(await screen.findByText(/gn-init/)).toBeInTheDocument();
    expect(mocked.createWorkspace).toHaveBeenCalledTimes(1);
  });
});

describe("顶栏", () => {
  it("names the workspace you are operating on", async () => {
    // 多工作区部署里,不显示这个的话人不知道自己在哪个上面操作。
    render(<App />);

    expect(await screen.findByText("mall")).toBeInTheDocument();
  });

  it("keeps your identity in exactly one place", async () => {
    // **否定断言,要显式写。** 两处都放身份的话,用户会问哪个才算数——而身份是会被
    // 服务端二次校验的东西,填错地方的后果是审批被拒。
    render(<App />);
    await screen.findByText("mall");

    expect(screen.getAllByLabelText("你的身份")).toHaveLength(1);
  });

  it("does not pretend the search box works", async () => {
    // 放一个不工作的搜索框比不放更糟。
    render(<App />);

    expect(await screen.findByText(/⌘K/)).toBeInTheDocument();
  });
});

describe("多项目", () => {
  it("shows a switcher with the initializing badge when there is more than one project", async () => {
    mocked.workspaces.mockResolvedValue({
      items: ["a", "b"],
      entries: [
        { name: "a", initializing: false },
        { name: "b", initializing: true },
      ],
    });
    render(<App />);

    const switcher = await screen.findByLabelText("切换项目");
    expect(switcher).toBeInTheDocument();
    expect(screen.getByText("b · 初始化中")).toBeInTheDocument();
  });

  it("guides to create the first project when the instance is empty", async () => {
    // 零项目是合法状态:给引导页,不是报错页。
    mocked.workspaces.mockResolvedValue({ items: [], entries: [] });
    const { container } = render(<App />);

    expect(await screen.findByText("创建第一个项目")).toBeInTheDocument();
    // 引导页没有侧栏,**不许套 `.app`**——那是"196px 侧栏 + 主区"的网格,套上它
    // 整页内容会掉进侧栏那一列,挤成一条(真实发生过)。
    expect(container.querySelector(".app")).toBeNull();
    expect(container.querySelector(".main")).not.toBeNull();
  });
});

describe("审批徽章", () => {
  it("shows how many tasks are waiting for review", async () => {
    render(<App />);

    const nav = await screen.findByRole("button", { name: /审批中心/ });
    await waitFor(() => expect(nav.querySelector(".n")?.textContent).toBe("1"));
  });

  it("shows nothing when the queue is empty", async () => {
    // 队列空的时候挂一个 0 的红点,会训练用户忽略它。
    mocked.tasks.mockResolvedValue([task("DEVELOPING", "ag-2")]);
    render(<App />);

    await waitFor(() => expect(mocked.tasks).toHaveBeenCalled());
    const nav = screen.getByRole("button", { name: /审批中心/ });
    expect(nav.querySelector(".n")).toBeNull();
  });
});
