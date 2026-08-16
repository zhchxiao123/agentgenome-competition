import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { WorkerRecycleActions } from "./WorkerRecycleActions";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: { ...actual.api, sleepWorker: vi.fn(), deleteWorker: vi.fn() },
  };
});

const { api, ApiError } = await import("../api/client");
const sleepMock = vi.mocked(api.sleepWorker);
const deleteMock = vi.mocked(api.deleteWorker);

beforeEach(() => {
  vi.clearAllMocks();
  sleepMock.mockResolvedValue({ employee_id: "arch-employee", action: "slept" });
  deleteMock.mockResolvedValue({ employee_id: "arch-employee", action: "deleted" });
});

const props = { employee: "arch-employee", canEdit: true, onChanged: () => {} };

describe("WorkerRecycleActions", () => {
  it("没供应过的员工没有可回收的东西", () => {
    render(<WorkerRecycleActions {...props} status="absent" />);

    expect(screen.queryByRole("button", { name: /休眠/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /删除/ })).not.toBeInTheDocument();
  });

  it("**休眠一点就走,不问第二遍**——可逆动作加确认只会训练人闭眼点确认", async () => {
    render(<WorkerRecycleActions {...props} status="running" />);

    await userEvent.click(screen.getByRole("button", { name: /休眠/ }));

    await waitFor(() => expect(sleepMock).toHaveBeenCalledWith("arch-employee"));
  });

  it("已经休眠的就不再给休眠按钮", () => {
    render(<WorkerRecycleActions {...props} status="sleeping" />);

    expect(screen.queryByRole("button", { name: /休眠/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /删除/ })).toBeInTheDocument();
  });

  it("**点一次删除什么都不删**——不可逆,而房间 id 找不回来", async () => {
    render(<WorkerRecycleActions {...props} status="running" />);

    await userEvent.click(screen.getByRole("button", { name: /删除/ }));

    expect(deleteMock).not.toHaveBeenCalled();
    expect(screen.getByText(/不可逆/)).toBeInTheDocument();
  });

  it("确认之后才真的删", async () => {
    render(<WorkerRecycleActions {...props} status="running" />);
    await userEvent.click(screen.getByRole("button", { name: /删除/ }));

    await userEvent.click(screen.getByRole("button", { name: /确认删除/ }));

    await waitFor(() => expect(deleteMock).toHaveBeenCalledWith("arch-employee"));
  });

  it("确认框可以反悔,反悔之后什么都没发生", async () => {
    render(<WorkerRecycleActions {...props} status="running" />);
    await userEvent.click(screen.getByRole("button", { name: /删除/ }));

    await userEvent.click(screen.getByRole("button", { name: /取消/ }));

    expect(deleteMock).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /确认删除/ })).not.toBeInTheDocument();
  });

  it("做完通知父组件——状态列要跟着变", async () => {
    const onChanged = vi.fn();
    render(<WorkerRecycleActions {...props} onChanged={onChanged} status="running" />);

    await userEvent.click(screen.getByRole("button", { name: /休眠/ }));

    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("失败时说清原因,而不是静静地什么都没变", async () => {
    sleepMock.mockRejectedValue(new ApiError(503, "连接被拒绝"));
    render(<WorkerRecycleActions {...props} status="running" />);

    await userEvent.click(screen.getByRole("button", { name: /休眠/ }));

    expect(await screen.findByText(/连接被拒绝/)).toBeInTheDocument();
  });

  it("没有权限时按钮点不动", () => {
    render(<WorkerRecycleActions {...props} canEdit={false} status="running" />);

    expect(screen.getByRole("button", { name: /休眠/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /删除/ })).toBeDisabled();
  });
});
