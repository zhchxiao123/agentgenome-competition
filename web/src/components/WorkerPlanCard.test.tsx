import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { WorkerPlanCard } from "./WorkerPlanCard";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: { ...actual.api, workerPlan: vi.fn(), provisionWorker: vi.fn() },
  };
});

const { api, ApiError } = await import("../api/client");
const planMock = vi.mocked(api.workerPlan);
const provisionMock = vi.mocked(api.provisionWorker);

beforeEach(() => {
  vi.clearAllMocks();
  planMock.mockResolvedValue({
    can_provision: true,
    items: [
      { employee_id: "arch-employee", action: "created", detail: "" },
      { employee_id: "dev-employee", action: "updated", detail: "" },
      { employee_id: "qa-employee", action: "unchanged", detail: "" },
    ],
  });
  provisionMock.mockImplementation(async (employee: string) => ({
    employee_id: employee,
    action: "created",
    worker: `agenome-${employee}`,
    room: `!room-${employee}:x`,
  }));
});

describe("WorkerPlanCard", () => {
  it("**不点就不算**——计划要读一遍平台,那是一次真实往返", async () => {
    render(<WorkerPlanCard />);

    expect(planMock).not.toHaveBeenCalled();
  });

  it("点了给出三态,且每一态读起来是不同的后果", async () => {
    render(<WorkerPlanCard />);

    await userEvent.click(screen.getByRole("button", { name: /查看计划/ }));

    expect(await screen.findByText(/新建/)).toBeInTheDocument();
    expect(screen.getByText(/更新/)).toBeInTheDocument();
    expect(screen.getByText(/无变化/)).toBeInTheDocument();
  });

  it("平台读不到的那一行说清是读不到,不混进「无变化」", async () => {
    planMock.mockResolvedValue({
      can_provision: true,
      items: [{ employee_id: "arch-employee", action: "unknown", detail: "连接被拒绝" }],
    });
    render(<WorkerPlanCard />);

    await userEvent.click(screen.getByRole("button", { name: /查看计划/ }));

    expect(await screen.findByText(/连接被拒绝/)).toBeInTheDocument();
    expect(screen.queryByText(/无变化/)).not.toBeInTheDocument();
  });

  it("整个请求挂了时说清原因,而不是空白或一直转圈", async () => {
    planMock.mockRejectedValue(new ApiError(400, "这个工作区没有配置 agentteams 运行时"));
    render(<WorkerPlanCard />);

    await userEvent.click(screen.getByRole("button", { name: /查看计划/ }));

    expect(await screen.findByText(/没有配置 agentteams/)).toBeInTheDocument();
  });

  it("没有供应权限时看得到计划,但被告知点不动", async () => {
    planMock.mockResolvedValue({
      can_provision: false,
      items: [{ employee_id: "arch-employee", action: "created", detail: "" }],
    });
    render(<WorkerPlanCard />);

    await userEvent.click(screen.getByRole("button", { name: /查看计划/ }));

    expect(await screen.findByText(/新建/)).toBeInTheDocument();
    expect(screen.getByText(/没有供应权限/)).toBeInTheDocument();
  });

  it("再点一次重新去问平台——计划会随平台变化而变化", async () => {
    render(<WorkerPlanCard />);
    const button = screen.getByRole("button", { name: /查看计划/ });

    await userEvent.click(button);
    await screen.findByText(/新建/);
    await userEvent.click(button);

    await waitFor(() => expect(planMock).toHaveBeenCalledTimes(2));
  });
});

describe("WorkerPlanCard 的执行", () => {
  it("**没看计划就没有执行入口**——每建一个都会真的拉起容器,不该是一次没有预期的点击", () => {
    render(<WorkerPlanCard />);

    expect(screen.queryByRole("button", { name: /执行供应/ })).not.toBeInTheDocument();
  });

  it("看过计划之后逐个供应,顺序与计划一致", async () => {
    render(<WorkerPlanCard />);
    await userEvent.click(screen.getByRole("button", { name: /查看计划/ }));
    await screen.findByText(/新建/);

    await userEvent.click(screen.getByRole("button", { name: /执行供应/ }));

    await waitFor(() => expect(provisionMock).toHaveBeenCalledTimes(3));
    expect(provisionMock.mock.calls.map(([id]) => id)).toEqual([
      "arch-employee",
      "dev-employee",
      "qa-employee",
    ]);
  });

  it("**一个失败不拖垮其余**,末尾列出失败者与原因", async () => {
    provisionMock.mockImplementation(async (employee: string) => {
      if (employee === "dev-employee") throw new ApiError(503, "连接被拒绝");
      return { employee_id: employee, action: "created", worker: "w", room: "!r:x" };
    });
    render(<WorkerPlanCard />);
    await userEvent.click(screen.getByRole("button", { name: /查看计划/ }));
    await screen.findByText(/新建/);

    await userEvent.click(screen.getByRole("button", { name: /执行供应/ }));

    expect(await screen.findByText(/dev-employee.*连接被拒绝|连接被拒绝/)).toBeInTheDocument();
    // 失败的那个之后的仍然跑过了
    expect(provisionMock.mock.calls.map(([id]) => id)).toContain("qa-employee");
  });

  it("完成之后通知父组件——状态列要跟着变,否则人看到的还是「未供应」", async () => {
    const onDone = vi.fn();
    render(<WorkerPlanCard onProvisioned={onDone} />);
    await userEvent.click(screen.getByRole("button", { name: /查看计划/ }));
    await screen.findByText(/新建/);

    await userEvent.click(screen.getByRole("button", { name: /执行供应/ }));

    await waitFor(() => expect(onDone).toHaveBeenCalled());
  });

  it("**执行中要报第几个,不是一个转到底的圈**——供应一个员工实测约十秒", async () => {
    let release = () => {};
    provisionMock.mockImplementation(async (employee: string) => {
      if (employee === "dev-employee") {
        await new Promise<void>((resolve) => {
          release = resolve;
        });
      }
      return { employee_id: employee, action: "created", worker: "w", room: "!r:x" };
    });
    render(<WorkerPlanCard />);
    await userEvent.click(screen.getByRole("button", { name: /查看计划/ }));
    await screen.findByText(/新建/);

    await userEvent.click(screen.getByRole("button", { name: /执行供应/ }));

    // 卡在第二个上:界面要说"正在第 2 个,共 3 个",而不是只说"进行中"
    const note = await screen.findByText(/正在供应/);
    expect(note.textContent).toMatch(/2\s*\/\s*3/);
    expect(note.textContent).toContain("dev-employee");
    release();
    await waitFor(() => expect(provisionMock).toHaveBeenCalledTimes(3));
  });

  it("没有供应权限时执行按钮点不动", async () => {
    planMock.mockResolvedValue({
      can_provision: false,
      items: [{ employee_id: "arch-employee", action: "created", detail: "" }],
    });
    render(<WorkerPlanCard />);

    await userEvent.click(screen.getByRole("button", { name: /查看计划/ }));

    expect(await screen.findByRole("button", { name: /执行供应/ })).toBeDisabled();
  });

  it("执行完把结果说清:做了什么,而不是只说「完成了」", async () => {
    provisionMock.mockImplementation(async (employee: string) => ({
      employee_id: employee,
      action: employee === "qa-employee" ? "unchanged" : "created",
      worker: `agenome-${employee}`,
      room: "!r:x",
    }));
    render(<WorkerPlanCard />);
    await userEvent.click(screen.getByRole("button", { name: /查看计划/ }));
    await screen.findByText(/新建/);

    await userEvent.click(screen.getByRole("button", { name: /执行供应/ }));

    await waitFor(() => expect(provisionMock).toHaveBeenCalledTimes(3));
    expect(await screen.findByText(/2 个新建/)).toBeInTheDocument();
    expect(screen.getByText(/1 个无变化/)).toBeInTheDocument();
  });
});
