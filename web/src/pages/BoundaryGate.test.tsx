import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { BoundaryGate, diffAgainst } from "./BoundaryGate";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { ...actual.api, gateDraft: vi.fn(), answerGate: vi.fn() } };
});

const { api, ApiError } = await import("../api/client");
const draftMock = vi.mocked(api.gateDraft);
const answerMock = vi.mocked(api.answerGate);

const draft = {
  task_id: "gn-0001",
  state: "AWAITING_CONFIRMATION",
  note: "这是机器推出来的草案",
  answered: false,
  modules: [
    { id: "order", paths: ["order/"], summary: "", rationale: "有独立的构建文件(pyproject.toml)" },
    { id: "pay", paths: ["pay/"], summary: "", rationale: "语言 python" },
  ],
};

beforeEach(() => {
  draftMock.mockReset();
  answerMock.mockReset();
  draftMock.mockResolvedValue(structuredClone(draft));
  answerMock.mockResolvedValue({ task_id: "gn-0001", moved: true, state: "DEEP_READ" });
});

describe("BoundaryGate", () => {
  it("renders each module with the reason the system split it that way", async () => {
    // 划分依据是人唯一能复核的东西。只给一个模块列表的话,他无从判断该不该改它。
    render(<BoundaryGate taskId="gn-0001" onAnswered={vi.fn()} />);

    expect(await screen.findByText(/有独立的构建文件/)).toBeInTheDocument();
    expect(screen.getByDisplayValue("order")).toBeInTheDocument();
    expect(screen.getByLabelText("覆盖的目录 1")).toHaveValue("order/");
  });

  it("sends a rename in the final list, not as a rename instruction", async () => {
    // 四种编辑在数据上统一为"产出最终列表"——不为每种动作定义单独的提交格式。
    render(<BoundaryGate taskId="gn-0001" onAnswered={vi.fn()} />);
    const field = await screen.findByLabelText("模块 id 1");

    await userEvent.clear(field);
    await userEvent.type(field, "orders");
    await userEvent.click(screen.getByRole("button", { name: "提交并继续" }));

    const body = answerMock.mock.calls[0]![1]!;
    expect(body.modules.map((m: { id: string }) => m.id)).toEqual(["orders", "pay"]);
  });

  it("merges two modules and makes the human give them one shared directory", async () => {
    // 一个模块对应一个目录(项目地图上的 path 是单值,也是子模块的挂载点)。合并之后那一行
    // 是不合法的,提交被禁用——**不替他猜**:猜错的那次,他合并的第二个目录会静默失去覆盖,
    // 而界面上显示的是"已确认"。
    render(<BoundaryGate taskId="gn-0001" onAnswered={vi.fn()} />);
    await screen.findByDisplayValue("order");

    await userEvent.click(screen.getAllByRole("button", { name: "与下一个合并" })[0]!);

    expect(screen.getByRole("button", { name: "提交并继续" })).toBeDisabled();
    expect(screen.getByText(/只能覆盖一个目录/)).toBeInTheDocument();

    const paths = screen.getByLabelText("覆盖的目录 1");
    await userEvent.clear(paths);
    await userEvent.type(paths, "mall/");
    await userEvent.click(screen.getByRole("button", { name: "提交并继续" }));

    const body = answerMock.mock.calls[0]![1]!;
    expect(body.modules).toHaveLength(1);
    expect(body.modules[0]!.paths).toEqual(["mall/"]);
  });

  it("splits one module into two and makes the human assign the directories", async () => {
    // 单目录不等于单模块:塞了两个域的目录该拆开。复制一份原目录的话,提交出去的是两个
    // 声称覆盖同一个目录的模块——下游谁都解析不对,而界面上它看起来完全正常。
    render(<BoundaryGate taskId="gn-0001" onAnswered={vi.fn()} />);
    await screen.findByDisplayValue("order");

    await userEvent.click(screen.getAllByRole("button", { name: "拆开" })[0]!);

    expect(screen.getByRole("button", { name: "提交并继续" })).toBeDisabled();
    expect(screen.getByText(/还没填覆盖的目录/)).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("覆盖的目录 2"), "order/reporting/");
    await userEvent.click(screen.getByRole("button", { name: "提交并继续" }));

    const body = answerMock.mock.calls[0]![1]!;
    expect(body.modules.map((m: { id: string }) => m.id)).toEqual(["order", "order-2", "pay"]);
    expect(body.modules[1]!.paths).toEqual(["order/reporting/"]);
  });

  it("tells a returning reviewer that this is his own answer", async () => {
    // 不说的话,他会以为系统把草案改成了这样。
    draftMock.mockResolvedValue({ ...structuredClone(draft), answered: true });

    render(<BoundaryGate taskId="gn-0001" onAnswered={vi.fn()} />);

    expect(await screen.findByText(/这是你上次提交的那一版/)).toBeInTheDocument();
  });

  it("drops a module the team does not want knowledge for", async () => {
    render(<BoundaryGate taskId="gn-0001" onAnswered={vi.fn()} />);
    await screen.findByDisplayValue("order");

    await userEvent.click(screen.getAllByRole("button", { name: "不建知识" })[0]!);
    await userEvent.click(screen.getByRole("button", { name: "提交并继续" }));

    const body = answerMock.mock.calls[0]![1]!;
    expect(body.modules.map((m: { id: string }) => m.id)).toEqual(["pay"]);
  });

  it("shows what changed before the human submits", async () => {
    // 人改完一屏之后记不住自己改了什么。不给差异的话,"我确认过了"没有依据。
    render(<BoundaryGate taskId="gn-0001" onAnswered={vi.fn()} />);
    await screen.findByDisplayValue("order");

    await userEvent.click(screen.getAllByRole("button", { name: "不建知识" })[0]!);

    const panel = screen.getByText("与草案的差异").closest(".card");
    expect(within(panel as HTMLElement).getByText(/剔除:order/)).toBeInTheDocument();
  });

  it("carries the human's note so later readers know why", async () => {
    render(<BoundaryGate taskId="gn-0001" onAnswered={vi.fn()} />);
    await screen.findByDisplayValue("order");

    await userEvent.type(screen.getByLabelText(/备注/), "pay 归到 order 下面");
    await userEvent.click(screen.getByRole("button", { name: "提交并继续" }));

    expect(answerMock.mock.calls[0]![1].note).toBe("pay 归到 order 下面");
  });

  it("tells the caller to refresh once the answer lands", async () => {
    const onAnswered = vi.fn();
    render(<BoundaryGate taskId="gn-0001" onAnswered={onAnswered} />);
    await screen.findByDisplayValue("order");

    await userEvent.click(screen.getByRole("button", { name: "提交并继续" }));

    expect(onAnswered).toHaveBeenCalled();
  });

  it("shows the error instead of pretending the answer landed", async () => {
    // 静默吞掉的话,人以为答完了,而任务还停在待确认——他不会再回来看第二眼。
    answerMock.mockRejectedValue(new ApiError(409, "现在没有闸门要回答"));
    const onAnswered = vi.fn();
    render(<BoundaryGate taskId="gn-0001" onAnswered={onAnswered} />);
    await screen.findByDisplayValue("order");

    await userEvent.click(screen.getByRole("button", { name: "提交并继续" }));

    expect(await screen.findByText(/提交失败/)).toHaveTextContent("现在没有闸门要回答");
    expect(onAnswered).not.toHaveBeenCalled();
  });

  it("shows the fetch error rather than an empty form", async () => {
    draftMock.mockRejectedValue(new ApiError(404, "还没有草案"));

    render(<BoundaryGate taskId="gn-0001" onAnswered={vi.fn()} />);

    expect(await screen.findByText(/拉不到边界草案/)).toHaveTextContent("还没有草案");
  });
});

describe("diffAgainst", () => {
  it("reports a rename, a drop and an addition", () => {
    const changes = diffAgainst(draft.modules, [
      { id: "orders", paths: ["order/"], summary: "", rationale: "" },
      { id: "extra", paths: ["extra/"], summary: "", rationale: "" },
    ]);

    expect(changes).toEqual([
      { kind: "renamed", from: "order", to: "orders" },
      { kind: "added", id: "extra" },
      { kind: "dropped", id: "pay" },
    ]);
  });

  it("calls a merge a merge, not an addition plus a deletion", () => {
    // 报成"新增 order"的话,它说的是他添加了一个草案里本来就有的模块——而这个面板存在的
    // 全部理由,就是让"我确认过了"这句话有依据。
    const changes = diffAgainst(draft.modules, [
      { id: "order", paths: ["order/", "pay/"], summary: "", rationale: "" },
    ]);

    expect(changes).toEqual([{ kind: "merged", id: "order", paths: ["order/", "pay/"] }]);
  });

  it("reports a split as a new module plus a narrowed one", () => {
    const changes = diffAgainst(draft.modules, [
      { id: "order", paths: ["order/core/"], summary: "", rationale: "" },
      { id: "order-2", paths: ["order/reporting/"], summary: "", rationale: "" },
      { id: "pay", paths: ["pay/"], summary: "", rationale: "" },
    ]);

    expect(changes).toContainEqual({ kind: "added", id: "order" });
    expect(changes).toContainEqual({ kind: "dropped", id: "order" });
  });
});
