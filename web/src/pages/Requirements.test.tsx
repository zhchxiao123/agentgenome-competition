import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { RequirementDetail, RequirementSummary, TaskDetail } from "../api/client";
import { subscribe } from "../api/live";
import { Requirements } from "./Requirements";

// mock 在 `api/client` 模块边界(ADR-0001):只换掉会碰网络的 `api`,ApiError 用真的。
vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      requirements: vi.fn(),
      requirement: vi.fn(),
      patchRequirement: vi.fn(),
      resplitRequirement: vi.fn(),
      submit: vi.fn(),
    },
  };
});
vi.mock("../api/live", () => ({ subscribe: vi.fn(() => () => undefined) }));

const { api, ApiError } = await import("../api/client");
const listMock = vi.mocked(api.requirements);
const detailMock = vi.mocked(api.requirement);
const patchMock = vi.mocked(api.patchRequirement);
const submitMock = vi.mocked(api.submit);
const subscribeMock = vi.mocked(subscribe);

function summary(overrides: Partial<RequirementSummary> = {}): RequirementSummary {
  return {
    id: "req-20260813-001",
    title: "退款",
    text: "支持部分退款",
    priority: 5,
    state: "queued",
    parked: "",
    attempts: 1,
    parent_id: "",
    blocked_by: [],
    children_total: 0,
    children_delivered: 0,
    created_at: "2026-08-13T10:00:00+00:00",
    updated_at: "2026-08-13T10:00:00+00:00",
    ...overrides,
  };
}

function detail(overrides: Partial<RequirementDetail> = {}): RequirementDetail {
  return {
    ...summary(),
    chain: [
      {
        id: "ag-20260813-001",
        state: "ESCALATED",
        execution_status: "finished",
        escalate_reason: "修复轮次已达上限",
        tokens_used: 1200,
        created_at: "2026-08-13T10:00:00+00:00",
      },
    ],
    total_tokens: 1200,
    children: [],
    tree_tokens: 1200,
    ...overrides,
  };
}

beforeEach(() => {
  listMock.mockReset();
  detailMock.mockReset();
  patchMock.mockReset();
  submitMock.mockReset();
  subscribeMock.mockReset();
  subscribeMock.mockReturnValue(() => undefined);
});

describe("Requirements", () => {
  it("shows business priorities instead of internal scheduling weights", async () => {
    listMock.mockResolvedValue([
      summary({ id: "req-p3", title: "低优先级", priority: 2 }),
      summary({ id: "req-unknown", title: "旧数据", priority: 9 }),
    ]);

    render(<Requirements openId={null} onOpen={vi.fn()} onOpenTask={vi.fn()} />);

    expect(await screen.findByText(/低优先级/)).toBeInTheDocument();
    expect(screen.getByText(/尝试 1 次 · P3/)).toBeInTheDocument();
    expect(screen.getByText(/未知优先级（9）/)).toBeInTheDocument();
  });

  it("renders every state and filters by it", async () => {
    listMock.mockResolvedValue([
      summary({ id: "req-1", state: "queued", title: "排队的" }),
      summary({ id: "req-2", state: "in_progress", title: "进行中的" }),
      summary({ id: "req-3", state: "delivered", title: "交付的" }),
      summary({ id: "req-4", state: "parked", title: "搁置的", parked: "不做了" }),
    ]);
    render(<Requirements openId={null} onOpen={vi.fn()} onOpenTask={vi.fn()} />);

    expect(await screen.findByText("排队的")).toBeInTheDocument();
    expect(screen.getByText("进行中的")).toBeInTheDocument();
    expect(screen.getByText("交付的")).toBeInTheDocument();
    expect(screen.getByText("搁置的")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "已交付" }));
    expect(screen.getByText("交付的")).toBeInTheDocument();
    expect(screen.queryByText("排队的")).not.toBeInTheDocument();
  });

  it("retries with the current text prefilled and carries requirement_id", async () => {
    listMock.mockResolvedValue([summary()]);
    detailMock.mockResolvedValue(detail());
    submitMock.mockResolvedValue({ id: "ag-2" } as TaskDetail);
    render(<Requirements openId="req-20260813-001" onOpen={vi.fn()} onOpenTask={vi.fn()} />);

    await userEvent.click(await screen.findByRole("button", { name: "再试一次" }));
    // 预填当前文本——重试的人带着上一轮的教训,从这份文本上改。
    const editor = screen.getByDisplayValue("支持部分退款");
    await userEvent.type(editor, "(按 SKU)");
    await userEvent.click(screen.getByRole("button", { name: "发起新尝试" }));

    await waitFor(() =>
      expect(submitMock).toHaveBeenCalledWith(
        expect.objectContaining({
          requirement: "支持部分退款(按 SKU)",
          requirement_id: "req-20260813-001",
        }),
      ),
    );
  });

  it("keeps the park button disabled until a reason is given", async () => {
    listMock.mockResolvedValue([summary()]);
    detailMock.mockResolvedValue(detail());
    render(<Requirements openId="req-20260813-001" onOpen={vi.fn()} onOpenTask={vi.fn()} />);

    const park = await screen.findByRole("button", { name: "搁置" });
    // 空原因禁点:搁置与恢复是相反的动作,空原因会让两者分不开。
    expect(park).toBeDisabled();
    await userEvent.type(screen.getByPlaceholderText(/搁置原因/), "不做了");
    expect(park).toBeEnabled();
  });

  it("shows the failure and stays in the old state when parking fails", async () => {
    listMock.mockResolvedValue([summary()]);
    detailMock.mockResolvedValue(detail());
    patchMock.mockRejectedValue(new ApiError(422, "搁置需要一句原因——空原因的搁置与恢复分不开。"));
    render(<Requirements openId="req-20260813-001" onOpen={vi.fn()} onOpenTask={vi.fn()} />);

    await userEvent.type(await screen.findByPlaceholderText(/搁置原因/), "x");
    await userEvent.click(screen.getByRole("button", { name: "搁置" }));

    // 失败说"没改成"并留在原状态——不留一个后端并不认的乐观状态。
    expect(await screen.findByText(/没改成/)).toBeInTheDocument();
    expect(screen.queryByText(/已搁置:/)).not.toBeInTheDocument();
  });

  it("opens the attempt's task when the chain entry is clicked", async () => {
    listMock.mockResolvedValue([summary()]);
    detailMock.mockResolvedValue(detail());
    const onOpenTask = vi.fn();
    render(<Requirements openId="req-20260813-001" onOpen={vi.fn()} onOpenTask={onOpenTask} />);

    await userEvent.click(await screen.findByText("ag-20260813-001"));
    expect(onOpenTask).toHaveBeenCalledWith("ag-20260813-001");
  });

  it("shows a running attempt as running instead of its stale lifecycle state", async () => {
    listMock.mockResolvedValue([summary({ state: "in_progress" })]);
    detailMock.mockResolvedValue(
      detail({
        chain: [
          {
            id: "ag-20260813-001",
            state: "CREATED",
            execution_status: "running",
            escalate_reason: null,
            tokens_used: 0,
            created_at: "2026-08-13T10:00:00+00:00",
          },
        ],
      }),
    );

    render(<Requirements openId="req-20260813-001" onOpen={vi.fn()} onOpenTask={vi.fn()} />);

    expect(await screen.findByText("需求解析中")).toBeInTheDocument();
    expect(screen.queryByText("CREATED")).not.toBeInTheDocument();
  });

  it("refreshes the open detail after a shared invalidation", async () => {
    const invalidate: Array<() => void> = [];
    subscribeMock.mockImplementation((onChange) => {
      invalidate.push(() => onChange({ task_id: "ag-20260813-001", kind: "task_changed" }));
      return () => undefined;
    });
    listMock.mockResolvedValue([summary({ state: "in_progress" })]);
    detailMock.mockResolvedValue(detail({ total_tokens: 1200 }));
    render(<Requirements openId="req-20260813-001" onOpen={vi.fn()} onOpenTask={vi.fn()} />);
    expect(await screen.findByText("累计 1,200 tokens")).toBeInTheDocument();

    detailMock.mockResolvedValue(detail({ total_tokens: 2400 }));
    invalidate.forEach((notify) => notify());

    expect(await screen.findByText("累计 2,400 tokens")).toBeInTheDocument();
  });

  it("folds children under their parent and expands on demand", async () => {
    const onOpen = vi.fn();
    listMock.mockResolvedValue([
      summary({ id: "req-parent", title: "SQL 引擎", children_total: 2, children_delivered: 1 }),
      summary({ id: "req-c1", title: "解析器", state: "delivered", parent_id: "req-parent" }),
      summary({ id: "req-c2", title: "执行器", state: "in_progress", parent_id: "req-parent" }),
    ]);

    render(<Requirements openId={null} onOpen={onOpen} onOpenTask={vi.fn()} />);

    // 顶层不被一长串子需求刷屏:默认收进母需求名下。
    expect(await screen.findByText("SQL 引擎")).toBeInTheDocument();
    expect(screen.queryByText("解析器")).not.toBeInTheDocument();
    expect(screen.getByText(/1\/2/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /展开/ }));
    expect(screen.getByText("解析器")).toBeInTheDocument();

    await userEvent.click(screen.getByText("解析器"));
    expect(onOpen).toHaveBeenCalledWith("req-c1");
  });

  it("renders the child tree with states, deps and progress", async () => {
    listMock.mockResolvedValue([summary({ children_total: 3, children_delivered: 1 })]);
    detailMock.mockResolvedValue(
      detail({
        state: "in_progress",
        children_total: 3,
        children_delivered: 1,
        tree_tokens: 45_000,
        children: [
          { id: "req-c1", title: "预占接口", state: "delivered", blocked_by: [], parked: "", attempts: 1, last_attempt_state: "COMPLETED" },
          { id: "req-c2", title: "下单接线", state: "in_progress", blocked_by: ["req-c1"], parked: "", attempts: 1, last_attempt_state: "DEVELOPING" },
          { id: "req-c3", title: "对账报表", state: "queued", blocked_by: [], parked: "", attempts: 1, last_attempt_state: "ESCALATED" },
        ],
      }),
    );

    render(<Requirements openId="req-20260813-001" onOpen={vi.fn()} onOpenTask={vi.fn()} />);

    expect(await screen.findByText(/子需求 · 1\/3 已交付/)).toBeInTheDocument();
    expect(screen.getByText("预占接口")).toBeInTheDocument();
    expect(screen.getByText(/依赖 req-c1/)).toBeInTheDocument();
    expect(screen.getByText(/树级 45,000 tokens/)).toBeInTheDocument();
    // 停点要一眼可见:推导状态说"排队中"时,ESCALATED 才是那个要人管的地方。
    expect(screen.getByText("停在人工")).toBeInTheDocument();
  });

  it("offers resplit on a tree and calls the endpoint", async () => {
    const resplitMock = vi.mocked(api.resplitRequirement);
    resplitMock.mockResolvedValue({} as never);
    listMock.mockResolvedValue([summary({ children_total: 2 })]);
    detailMock.mockResolvedValue(
      detail({
        children_total: 2,
        children: [
          { id: "req-c1", title: "预占接口", state: "delivered", blocked_by: [], parked: "", attempts: 1, last_attempt_state: "COMPLETED" },
          { id: "req-c2", title: "下单接线", state: "queued", blocked_by: [], parked: "", attempts: 0, last_attempt_state: "" },
        ],
      }),
    );

    render(<Requirements openId="req-20260813-001" onOpen={vi.fn()} onOpenTask={vi.fn()} />);

    await userEvent.click(await screen.findByRole("button", { name: "重新拆分剩余" }));
    await waitFor(() => expect(resplitMock).toHaveBeenCalledWith("req-20260813-001"));
  });

  it("does not offer resplit on a flat requirement", async () => {
    listMock.mockResolvedValue([summary()]);
    detailMock.mockResolvedValue(detail());

    render(<Requirements openId="req-20260813-001" onOpen={vi.fn()} onOpenTask={vi.fn()} />);

    expect(await screen.findByText("当前文本")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重新拆分剩余" })).not.toBeInTheDocument();
  });

  it("does not render a tree section for a flat requirement", async () => {
    listMock.mockResolvedValue([summary()]);
    detailMock.mockResolvedValue(detail());

    render(<Requirements openId="req-20260813-001" onOpen={vi.fn()} onOpenTask={vi.fn()} />);

    expect(await screen.findByText("当前文本")).toBeInTheDocument();
    expect(screen.queryByText(/子需求 · /)).not.toBeInTheDocument();
  });

  it("offers resume instead of park for a parked requirement", async () => {
    listMock.mockResolvedValue([summary({ state: "parked", parked: "不做了" })]);
    detailMock.mockResolvedValue(detail({ state: "parked", parked: "不做了" }));
    patchMock.mockResolvedValue(detail());
    render(<Requirements openId="req-20260813-001" onOpen={vi.fn()} onOpenTask={vi.fn()} />);

    expect(await screen.findByText(/已搁置:不做了/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "恢复" }));
    await waitFor(() => expect(patchMock).toHaveBeenCalledWith("req-20260813-001", { resume: true }));
  });
});
