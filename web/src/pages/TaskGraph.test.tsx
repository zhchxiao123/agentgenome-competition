import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TaskGraph, graphRuns } from "./TaskGraph";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { ...actual.api, events: vi.fn() } };
});

const { api, ApiError } = await import("../api/client");
const eventsMock = vi.mocked(api.events);

function event(payload: Record<string, unknown>) {
  return {
    task_id: "ag-0001",
    ts: "2026-01-01T00:00:00+00:00",
    actor: "orchestrator",
    kind: "topology",
    payload,
  };
}

const CHOSEN = event({
  phase: "chosen",
  stage: "develop",
  why: "plan",
  template: {
    id: "dag",
    nodes: [{ id: "order" }, { id: "inventory" }, { id: "wire" }],
    edges: [
      ["order", "wire"],
      ["inventory", "wire"],
    ],
  },
});

const RAN = event({
  phase: "ran",
  stage: "develop",
  template_id: "dag",
  stopped_because: "node-failed",
  failed: ["order"],
  frozen: ["wire"],
  nodes: [
    { id: "order", kind: "work", ok: false },
    { id: "inventory", kind: "work", ok: true },
  ],
});

function page(items: ReturnType<typeof event>[]) {
  return { items, total: items.length, offset: 0, limit: 1000 };
}

//: 需求解析那一步也会记一条"选了哪张图"(单节点),而它**不会**有对应的"跑成了什么"
//: ——按下标配对的话,开发那张图会配到这一条,于是整块图永远画不出来。
const PLAN_CHOSEN = event({
  phase: "chosen",
  stage: "plan",
  why: "",
  template: { id: "single", nodes: [{ id: "main" }], edges: [] },
});

beforeEach(() => {
  vi.clearAllMocks();
  eventsMock.mockResolvedValue(page([PLAN_CHOSEN, CHOSEN, RAN]));
});

describe("挑图", () => {
  it("单节点的不算图", () => {
    // 画一个只有一个方框的"图"是噪音。
    const single = event({ phase: "ran", stage: "develop", template_id: "single", nodes: [{ id: "main" }] });

    expect(graphRuns([single])).toEqual([]);
  });

  it("按阶段配对,不按位置", () => {
    // 需求解析那一步的单节点模板排在前面,而它没有对应的"跑成了什么"。
    const runs = graphRuns([PLAN_CHOSEN, CHOSEN, RAN]);

    expect(runs).toHaveLength(1);
    expect(runs[0]!.edges).toHaveLength(2);
  });
});

describe("渲染", () => {
  it("一眼看出有哪些节点、谁依赖谁", async () => {
    render(<TaskGraph id="ag-0001" />);

    expect(await screen.findByText("order")).toBeInTheDocument();
    expect(screen.getByText("order → wire")).toBeInTheDocument();
  });

  it("失败与「因此没跑」分得开", async () => {
    // 画成一样的话,人会以为有五个地方坏了,而实际上只坏了一个。
    render(<TaskGraph id="ag-0001" />);

    expect(await screen.findByText(/上游失败,没跑/)).toBeInTheDocument();
  });

  it("没跑过多节点图时整块不出现", async () => {
    eventsMock.mockResolvedValue(page([]));

    const { container } = render(<TaskGraph id="ag-0001" />);

    await waitFor(() => expect(eventsMock).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("拉不到时不把整个详情页判死", async () => {
    eventsMock.mockRejectedValue(new ApiError(500, "后端挂了"));

    render(<TaskGraph id="ag-0001" />);

    expect(await screen.findByText(/拉不到执行图/)).toBeInTheDocument();
  });
});
