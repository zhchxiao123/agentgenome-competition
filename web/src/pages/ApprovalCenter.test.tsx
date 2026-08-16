import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApprovalCenter } from "./ApprovalCenter";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { ...actual.api, tasks: vi.fn(), report: vi.fn(), artifacts: vi.fn() } };
});
vi.mock("../api/live", () => ({ subscribe: vi.fn(() => () => undefined) }));

const { api, ApiError } = await import("../api/client");
const tasksMock = vi.mocked(api.tasks);
const reportMock = vi.mocked(api.report);
const artifactsMock = vi.mocked(api.artifacts);
const { subscribe } = await import("../api/live");
const subscribeMock = vi.mocked(subscribe);

beforeEach(() => {
  tasksMock.mockReset();
  reportMock.mockReset();
  reportMock.mockResolvedValue({ markdown: "ok", task_id: "ag-a" });
  artifactsMock.mockReset();
  artifactsMock.mockResolvedValue({ items: [] });
  subscribeMock.mockReset();
  subscribeMock.mockReturnValue(() => undefined);
});

describe("ApprovalCenter", () => {
  it("shows an error instead of 'nothing to approve' when the queue fails to load", async () => {
    // 回归测试:这里之前完全没有 `.catch(...)`——拉取失败是一个悬空的 rejected promise,
    // 队列停在初始空数组,界面上显示"没有等着审批的任务",跟真的没有待审批任务没法分辨。
    tasksMock.mockRejectedValue(new ApiError(500, "backend unreachable"));

    render(<ApprovalCenter actor="alice" />);

    expect(await screen.findByText(/拉不到待审批队列/)).toHaveTextContent("backend unreachable");
    expect(screen.queryByText("没有等着审批的任务。")).not.toBeInTheDocument();
  });

  it("drops a selection that was approved elsewhere after live invalidation", async () => {
    let invalidate: (() => void) | undefined;
    subscribeMock.mockImplementation((onChange) => {
      invalidate = () => onChange({ task_id: "ag-a", kind: "task_changed" });
      return () => undefined;
    });
    const review = (id: string, title: string) => ({
      id, title, state: "REVIEWING", priority: 5, fix_rounds: 0, plan_retries: 0,
      needs_itest: "UNDECIDED", itest_override: "auto", mode: "autonomous", topology: "",
      tokens_used: 0, created_at: "2026-08-14T10:00:00Z", updated_at: "2026-08-14T10:00:00Z",
      can_run: false, execution_status: "waiting", risk_level: "high",
    }) as Awaited<ReturnType<typeof api.tasks>>[number];
    tasksMock.mockResolvedValue([review("ag-a", "A"), review("ag-b", "B")]);
    render(<ApprovalCenter actor="alice" />);
    expect(await screen.findByText("A")).toBeInTheDocument();

    tasksMock.mockResolvedValue([review("ag-b", "B")]);
    invalidate?.();

    await vi.waitFor(() => expect(reportMock).toHaveBeenLastCalledWith("ag-b"));
    expect(screen.queryByText("A")).not.toBeInTheDocument();
  });
});
