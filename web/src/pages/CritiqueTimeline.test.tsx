import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CritiqueTimeline, loopRuns, sortFindings } from "./CritiqueTimeline";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: { ...actual.api, events: vi.fn(), artifact: vi.fn() },
  };
});

const { api, ApiError } = await import("../api/client");
const eventsMock = vi.mocked(api.events);
const artifactMock = vi.mocked(api.artifact);

function event(payload: Record<string, unknown>) {
  return {
    task_id: "ag-0001",
    ts: "2026-01-01T00:00:00+00:00",
    actor: "orchestrator",
    kind: "topology",
    payload,
  };
}

const CHOICE = event({
  phase: "chosen",
  stage: "develop",
  why: "modules",
  template: { id: "critique-loop" },
});
const RUN = event({
  phase: "ran",
  stage: "develop",
  template_id: "critique-loop",
  stopped_because: "converged",
  rounds: 2,
  tokens_used: 4321,
  tokens_available: true,
  nodes: [
    { id: "generate", kind: "work", ok: true },
    {
      id: "critique",
      kind: "checker",
      ok: true,
      approved: false,
      findings: 4,
      slot: "03-develop.critique",
    },
    { id: "refine", kind: "work", ok: true },
    { id: "critique", kind: "checker", ok: true, approved: true, findings: 0, slot: "05" },
  ],
});

const FINDINGS = [
  { file: "a.py", line: 3, severity: "minor", issue: "小问题", suggestion: "改小的" },
  { file: "b.py", line: 7, severity: "blocker", issue: "拦路问题", suggestion: "必须改" },
  { file: "c.py", severity: "major", issue: "大问题", suggestion: "该改" },
  { file: "d.py", severity: "minor", issue: "第四条", suggestion: "随手改" },
];

function page(items: ReturnType<typeof event>[]) {
  return { items, total: items.length, offset: 0, limit: 1000 };
}

beforeEach(() => {
  vi.clearAllMocks();
  eventsMock.mockResolvedValue(page([CHOICE, RUN]));
  artifactMock.mockResolvedValue(JSON.stringify({ findings: FINDINGS }));
});

describe("挑环", () => {
  it("把选模板与跑完两条事件配成一条", () => {
    const runs = loopRuns([CHOICE, RUN]);

    expect(runs).toHaveLength(1);
    expect(runs[0]!.why).toBe("modules");
    expect(runs[0]!.rounds).toBe(2);
  });

  it("没进环的任务挑不出任何环", () => {
    const single = event({ phase: "chosen", stage: "develop", why: "", template: { id: "single" } });

    expect(loopRuns([single])).toEqual([]);
  });
});

describe("意见排序", () => {
  it("按严重度排,不按文件名", () => {
    // 排序本身是评审的判断。按文件名摊平等于把 20 条意见变成一堆待办。
    expect(sortFindings(FINDINGS).map((item) => item.severity)).toEqual([
      "blocker",
      "major",
      "minor",
      "minor",
    ]);
  });
});

describe("时间线", () => {
  it("一眼读出轮数、进环原因、停的原因与花费", async () => {
    render(<CritiqueTimeline id="ag-0001" />);

    expect(await screen.findByText(/2 轮批判/)).toBeInTheDocument();
    expect(screen.getByText(/计划命中的模块数达到阈值/)).toBeInTheDocument();
    expect(screen.getByText(/评审通过/)).toBeInTheDocument();
    expect(screen.getByText("4,321")).toBeInTheDocument();
  });

  it("每轮的结论各自成行", async () => {
    render(<CritiqueTimeline id="ag-0001" />);

    expect(await screen.findByText("第 1 轮 4 条意见")).toBeInTheDocument();
    expect(screen.getByText("第 2 轮 通过")).toBeInTheDocument();
  });

  it("没进过环时这一块整个不出现,不是空块", async () => {
    // 显示"0 轮"的话,"没走批判"和"走了但没意见"看起来一样。
    eventsMock.mockResolvedValue(
      page([event({ phase: "chosen", stage: "develop", template: { id: "single" } })]),
    );

    const { container } = render(<CritiqueTimeline id="ag-0001" />);

    await waitFor(() => expect(eventsMock).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("意见原文按指针拉,默认只显示前几条", async () => {
    render(<CritiqueTimeline id="ag-0001" />);

    expect(await screen.findByText("拦路问题")).toBeInTheDocument();
    expect(artifactMock).toHaveBeenCalledWith(
      "ag-0001",
      "artifacts/03-develop.critique/result.json",
    );
    expect(screen.queryByText("第四条")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "还有 1 条" }));

    expect(screen.getByText("第四条")).toBeInTheDocument();
  });

  it("产物读不到时降级成一句说明", async () => {
    artifactMock.mockRejectedValue(new Error("没了"));

    render(<CritiqueTimeline id="ag-0001" />);

    expect(await screen.findByText(/意见原文读不到了/)).toBeInTheDocument();
  });

  it("事件拉不到时不把整个详情页判死", async () => {
    eventsMock.mockRejectedValue(new ApiError(500, "后端挂了"));

    render(<CritiqueTimeline id="ag-0001" />);

    expect(await screen.findByText(/拉不到精化环记录/)).toBeInTheDocument();
  });

  it("批判自己失败的那一轮说清楚产出未经批判", async () => {
    eventsMock.mockResolvedValue(
      page([
        CHOICE,
        event({
          ...(RUN.payload as Record<string, unknown>),
          stopped_because: "checker-failed",
          rounds: 1,
          nodes: [
            { id: "generate", kind: "work", ok: true },
            { id: "critique", kind: "checker", ok: false, approved: true },
          ],
        }),
      ]),
    );

    render(<CritiqueTimeline id="ag-0001" />);

    expect(await screen.findByText(/产出未经批判就送了门禁/)).toBeInTheDocument();
    expect(screen.getByText(/批判本身失败/)).toBeInTheDocument();
    expect(screen.getByText("第 1 轮 执行失败")).toBeInTheDocument();
    expect(screen.queryByText("第 1 轮 通过")).not.toBeInTheDocument();
  });

  it("用量不可得时不显示 0", async () => {
    // 填 0 会让成本看板悄悄少算,比缺数据更糟。
    eventsMock.mockResolvedValue(
      page([
        CHOICE,
        event({
          ...(RUN.payload as Record<string, unknown>),
          tokens_used: null,
          tokens_available: false,
        }),
      ]),
    );

    render(<CritiqueTimeline id="ag-0001" />);

    expect(await screen.findByText("不可得")).toBeInTheDocument();
  });
});
