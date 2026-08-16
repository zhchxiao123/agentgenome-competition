import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ImportResult, TaskDetail, TopologyCatalog } from "../api/client";
import { Submit } from "./Submit";

// 只换掉会碰网络的 `api`,`ApiError` 保留真实实现——`Submit.tsx` 用它做 catch 参数的类型,
// 这里也要用真的构造函数,才能构造出一个跟真实失败一样的错误对象。
vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: { ...actual.api, submit: vi.fn(), importTicket: vi.fn(), topologies: vi.fn() },
  };
});

const { api, ApiError } = await import("../api/client");
const submitMock = vi.mocked(api.submit);
const topologiesMock = vi.mocked(api.topologies);
const importMock = vi.mocked(api.importTicket);

const CATALOG: TopologyCatalog = {
  default: "single",
  options: [
    {
      id: "single",
      name: "单路",
      summary: "一个员工干一道工序",
      steps: ["开发员工实现", "质量门禁"],
      experimental: false,
      available: true,
      unavailable_reason: "",
      cost_multiplier: 1,
      cost_estimate_tokens: 20000,
    },
    {
      id: "critique-loop",
      name: "精化环",
      summary: "加一轮只批不改的评审",
      steps: ["开发员工实现", "评审员工只批不改", "开发按意见改一遍"],
      experimental: false,
      available: true,
      unavailable_reason: "",
      cost_multiplier: 1,
      cost_estimate_tokens: 20000,
    },
    {
      id: "best-of-n",
      name: "多路择优",
      summary: "N 路并行,门禁即适应度",
      steps: ["N 路并行实现"],
      experimental: true,
      available: true,
      unavailable_reason: "",
      cost_multiplier: 3,
      cost_estimate_tokens: 60000,
    },
  ],
};

beforeEach(() => {
  submitMock.mockReset();
  topologiesMock.mockReset();
  importMock.mockReset();
  topologiesMock.mockResolvedValue(CATALOG);
});

function typeRequirement(text: string) {
  return userEvent.type(screen.getByPlaceholderText(/谁、在什么情况下、要得到什么/), text);
}

describe("Submit", () => {
  it("blocks submission and shows a validation error when the requirement is empty", async () => {
    const onSubmitted = vi.fn();
    render(<Submit onSubmitted={onSubmitted} />);

    await userEvent.click(screen.getByRole("button", { name: "提交需求" }));

    expect(screen.getByText(/需求原文不能为空/)).toBeInTheDocument();
    expect(submitMock).not.toHaveBeenCalled();
    expect(onSubmitted).not.toHaveBeenCalled();
  });

  it("submits the requirement and hands the created task to onSubmitted", async () => {
    submitMock.mockResolvedValue({ id: "ag-42" } as TaskDetail);
    const onSubmitted = vi.fn();
    render(<Submit onSubmitted={onSubmitted} />);

    await typeRequirement("下单时校验库存是否充足");
    await userEvent.click(screen.getByRole("button", { name: "提交需求" }));

    expect(submitMock).toHaveBeenCalledWith({
      requirement: "下单时校验库存是否充足",
      title: "",
      itest: "auto",
      priority: 5,
      // 表单提的都是自主任务;结对走对话工作台的「开结对会话」入口。
      mode: "autonomous",
      // 没动那一栏 = 跟随项目缺省。**不是 "single"**:展开的话,"没表态"与"明确选了
      // single"在记录上会变成同一件事。
      topology: "",
    });
    // 整个任务交回去:去向(需求详情还是任务详情)由 App 按 requirement_id 决定。
    expect(onSubmitted).toHaveBeenCalledWith({ id: "ag-42" });
  });

  it("disables the entry with the reason while the project is still initializing", async () => {
    // 禁用并说明原因,比点了提交才被 409 拒绝早一步(PRD 44 issue 04)。
    render(<Submit onSubmitted={vi.fn()} disabledReason="项目还在初始化:业务仓挂载中" />);

    expect(screen.getByRole("button", { name: "提交需求" })).toBeDisabled();
    expect(screen.getByText(/项目还在初始化/)).toBeInTheDocument();
    expect(submitMock).not.toHaveBeenCalled();
  });

  it("shows the backend's detail message when submit fails", async () => {
    submitMock.mockRejectedValue(new ApiError(400, "需求不能超过 2000 字"));
    render(<Submit onSubmitted={vi.fn()} />);

    await typeRequirement("一段需求");
    await userEvent.click(screen.getByRole("button", { name: "提交需求" }));

    expect(await screen.findByText("需求不能超过 2000 字")).toBeInTheDocument();
    expect(screen.getByDisplayValue("一段需求")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "提交需求" })).toBeEnabled();
  });

  it("allows only one in-flight submission for one user intent", async () => {
    let finish: ((task: TaskDetail) => void) | undefined;
    submitMock.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    const onSubmitted = vi.fn();
    render(<Submit onSubmitted={onSubmitted} />);
    await typeRequirement("只创建一次");

    const button = screen.getByRole("button", { name: "提交需求" });
    await userEvent.click(button);
    await userEvent.click(button);

    expect(submitMock).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "提交中…" })).toBeDisabled();
    finish?.({ id: "ag-once" } as TaskDetail);
    await vi.waitFor(() => expect(onSubmitted).toHaveBeenCalledTimes(1));
  });

  it("keeps ticket import separate and blocks submission until import settles", async () => {
    let finish: ((found: ImportResult) => void) | undefined;
    importMock.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    render(<Submit onSubmitted={vi.fn()} />);
    await userEvent.type(screen.getByPlaceholderText(/github.com/), "https://github.com/acme/shop/issues/1");
    await userEvent.click(screen.getByRole("button", { name: "导入" }));

    expect(screen.getByRole("button", { name: "提交需求" })).toBeDisabled();
    expect(screen.getByPlaceholderText(/谁、在什么情况下/)).toBeDisabled();
    finish?.({ title: "退款", body: "支持部分退款", source: "github" });
    expect(await screen.findByDisplayValue("支持部分退款")).toBeEnabled();
  });

  it("labels an import error independently from a submission error", async () => {
    importMock.mockRejectedValue(new ApiError(404, "工单不存在"));
    render(<Submit onSubmitted={vi.fn()} />);
    await userEvent.type(screen.getByPlaceholderText(/github.com/), "https://github.com/acme/shop/issues/404");
    await userEvent.click(screen.getByRole("button", { name: "导入" }));

    expect(await screen.findByText("导入失败:工单不存在")).toBeInTheDocument();
    expect(screen.queryByText(/^工单不存在$/)).not.toBeInTheDocument();
  });

  it("offers the strategies the backend lists, not a hard-coded set", async () => {
    render(<Submit onSubmitted={vi.fn()} />);

    const picker = await screen.findByLabelText(/执行拓扑/);
    const offered = Array.from(picker.querySelectorAll("option")).map((item) => item.textContent);
    expect(offered.some((text) => text?.includes("精化环"))).toBe(true);
    expect(offered.some((text) => text?.includes("多路择优"))).toBe(true);
  });

  it("submits the chosen strategy", async () => {
    submitMock.mockResolvedValue({ id: "ag-43" } as TaskDetail);
    render(<Submit onSubmitted={vi.fn()} />);

    await typeRequirement("重构下单");
    await userEvent.selectOptions(await screen.findByLabelText(/执行拓扑/), "critique-loop");
    await userEvent.click(screen.getByRole("button", { name: "提交需求" }));

    expect(submitMock).toHaveBeenCalledWith(
      expect.objectContaining({ topology: "critique-loop" }),
    );
  });

  it("refuses to offer a strategy the backend says is not available, and says why", async () => {
    topologiesMock.mockResolvedValue({
      ...CATALOG,
      options: CATALOG.options.map((item) =>
        item.id === "best-of-n"
          ? { ...item, available: false, unavailable_reason: "至少要两路变体" }
          : item,
      ),
    });
    render(<Submit onSubmitted={vi.fn()} />);

    const picker = await screen.findByLabelText(/执行拓扑/);
    const bestOfN = Array.from(picker.querySelectorAll("option")).find((item) =>
      item.textContent?.includes("多路择优"),
    );
    expect(bestOfN).toBeDisabled();
    // 静默灰掉等于让人以为这个能力不存在——原因要写在页面上。
    expect(screen.getByText(/至少要两路变体/)).toBeInTheDocument();
  });

  it("shows what the expensive strategy costs before it is chosen", async () => {
    // PRD 39:N 倍成本必须是一个被看见的决定。倍数写在选项上,不必先选中才知道。
    render(<Submit onSubmitted={vi.fn()} />);

    const picker = await screen.findByLabelText(/执行拓扑/);
    const bestOfN = Array.from(picker.querySelectorAll("option")).find((item) =>
      item.textContent?.includes("多路择优"),
    );
    expect(bestOfN?.textContent).toContain("3 ×");
  });

  it("spells the cost out once the strategy is selected", async () => {
    render(<Submit onSubmitted={vi.fn()} />);

    await userEvent.selectOptions(await screen.findByLabelText(/执行拓扑/), "best-of-n");

    expect(screen.getByText(/3 × 单路/)).toBeInTheDocument();
    expect(screen.getByText(/60,000/)).toBeInTheDocument();
    // 机制在、结论还没有——这个标记不该被读成推荐。
    expect(screen.getByText(/收益尚无数据/)).toBeInTheDocument();
  });

  it("shows only the multiple when the project has no history to estimate from", async () => {
    topologiesMock.mockResolvedValue({
      ...CATALOG,
      options: CATALOG.options.map((item) => ({ ...item, cost_estimate_tokens: null })),
    });
    render(<Submit onSubmitted={vi.fn()} />);

    await userEvent.selectOptions(await screen.findByLabelText(/执行拓扑/), "best-of-n");

    expect(screen.getByText(/3 × 单路/)).toBeInTheDocument();
    // 编不出来的数字就不显示:一个假的绝对值比不显示更糟。
    expect(screen.queryByText(/tokens/)).not.toBeInTheDocument();
  });

  it("describes what happens next for the strategy that is actually selected", async () => {
    render(<Submit onSubmitted={vi.fn()} />);

    expect(await screen.findByText("质量门禁")).toBeInTheDocument();
    await userEvent.selectOptions(await screen.findByLabelText(/执行拓扑/), "critique-loop");

    expect(await screen.findByText("评审员工只批不改")).toBeInTheDocument();
    expect(screen.queryByText("质量门禁")).not.toBeInTheDocument();
  });

  it("still says what happens next when the catalog cannot be loaded", async () => {
    // 拉不到名单之前这一栏有六条写死的步骤;换成服务端给之后不能变成一片空白——
    // 「提交之后会发生什么」是产品经理敢自己提需求的前提。
    topologiesMock.mockRejectedValue(new ApiError(500, "拉不到"));
    render(<Submit onSubmitted={vi.fn()} />);

    expect(await screen.findByText(/拉不到执行拓扑/)).toBeInTheDocument();
  });

  it("still renders the form when the catalog cannot be loaded", async () => {
    // 拉不到名单不该让人提不了需求——策略是可选项,需求原文才是这一页的理由。
    topologiesMock.mockRejectedValue(new ApiError(500, "拉不到"));
    submitMock.mockResolvedValue({ id: "ag-44" } as TaskDetail);
    render(<Submit onSubmitted={vi.fn()} />);

    await typeRequirement("一段需求");
    await userEvent.click(screen.getByRole("button", { name: "提交需求" }));

    expect(submitMock).toHaveBeenCalled();
  });
});
