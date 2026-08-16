import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MyTodos } from "./MyTodos";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: { ...actual.api, todos: vi.fn(), todo: vi.fn(), submitTodo: vi.fn() },
  };
});

const { api, ApiError } = await import("../api/client");
const todosMock = vi.mocked(api.todos);
const todoMock = vi.mocked(api.todo);
const submitMock = vi.mocked(api.submitTodo);

const ITEM = {
  id: "todo-1",
  task_id: "ag-0001",
  stage: "develop",
  node: "",
  assignee: "alice",
  employee_id: "dev-employee",
  procedure_id: "code-develop",
  kind: "worktree",
  state: "pending",
  reminded: false,
  reassignments: 0,
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
  due_at: "2026-01-04T00:00:00+00:00",
};

const DETAIL = {
  ...ITEM,
  context_file: "tasks/ag-0001/artifacts/02-develop/context-attempt-0.md",
  output_dir: "tasks/ag-0001/artifacts/02-develop",
  workdir: "worktrees/ag-0001",
  schema: { required: ["task_id", "changed_files", "self_test"] },
};

beforeEach(() => {
  vi.clearAllMocks();
  todosMock.mockResolvedValue({ items: [ITEM] });
  todoMock.mockResolvedValue(DETAIL);
  submitMock.mockResolvedValue({ ok: true, todo: ITEM, detail: "", task_state: "UNIT_TESTING" });
});

describe("列表", () => {
  it("一眼看出这张待办要去哪儿干活", async () => {
    render(<MyTodos />);

    expect(await screen.findByText("code-develop")).toBeInTheDocument();
    expect(screen.getByText("去工作树里改代码")).toBeInTheDocument();
  });

  it("改派过的标出来:这一轮的窗口是重新算的", async () => {
    todosMock.mockResolvedValue({ items: [{ ...ITEM, reassignments: 1 }] });

    render(<MyTodos />);

    expect(await screen.findByText("已改派 1 次")).toBeInTheDocument();
  });

  it("没有派给我的活时说没有,而不是空白", async () => {
    todosMock.mockResolvedValue({ items: [] });

    render(<MyTodos />);

    expect(await screen.findByText("没有派给我的活。")).toBeInTheDocument();
  });

  it("拉不到时降级成一句说明", async () => {
    todosMock.mockRejectedValue(new ApiError(500, "后端挂了"));

    render(<MyTodos />);

    expect(await screen.findByText(/拉不到待办/)).toBeInTheDocument();
  });
});

describe("拆分提案", () => {
  const PROPOSAL = {
    children: [
      { title: "预占接口", text: "实现库存预占接口。验收:预占后可查询余量。" },
      { title: "下单接线", text: "下单流程调用预占。", blocked_by: [0] },
    ],
    rationale: "单任务交付不可审。",
  };
  const SPLIT_ITEM = {
    ...ITEM,
    id: "todo-split",
    stage: "plan",
    node: "split",
    employee_id: "decision-employee",
    procedure_id: "requirement-analysis",
    kind: "split",
    proposal: PROPOSAL,
  };
  const SPLIT_DETAIL = {
    ...SPLIT_ITEM,
    context_file: "",
    output_dir: "tasks/ag-0001/artifacts/01-plan",
    workdir: "",
    schema: { required: ["approved"] },
  };

  beforeEach(() => {
    todosMock.mockResolvedValue({ items: [SPLIT_ITEM] });
    todoMock.mockResolvedValue(SPLIT_DETAIL);
  });

  it("列表里一眼看出这是一份要裁决的提案", async () => {
    render(<MyTodos />);

    expect(await screen.findByText("裁决拆分提案")).toBeInTheDocument();
  });

  it("打开之后是只读的提案,不是一个 JSON 框", async () => {
    render(<MyTodos />);

    await userEvent.click(await screen.findByText("requirement-analysis"));

    expect(await screen.findByText("预占接口")).toBeInTheDocument();
    expect(screen.getByText("下单接线")).toBeInTheDocument();
    expect(screen.getByText(/单任务交付不可审/)).toBeInTheDocument();
    // 依赖要能看见:没有它,人裁决的是一堆无序的标题。
    expect(screen.getByText(/依赖 #0/)).toBeInTheDocument();
    expect(screen.queryByLabelText("产物 JSON")).not.toBeInTheDocument();
  });

  it("原样确认交 approved=true", async () => {
    render(<MyTodos />);
    await userEvent.click(await screen.findByText("requirement-analysis"));

    await userEvent.click(await screen.findByRole("button", { name: "确认拆分" }));

    await waitFor(() =>
      expect(submitMock).toHaveBeenCalledWith("todo-split", { approved: true }),
    );
    expect(todosMock).toHaveBeenCalledTimes(2);
  });

  it("就地调整后确认交的是调整稿", async () => {
    render(<MyTodos />);
    await userEvent.click(await screen.findByText("requirement-analysis"));
    await userEvent.click(await screen.findByRole("button", { name: "调整提案" }));

    // 删掉 1 号,再把 0 号标题改一下 → 确认交 {approved, children: 调整稿}。
    const removeButtons = await screen.findAllByRole("button", { name: "删除" });
    await userEvent.click(removeButtons[1]!);
    const title = screen.getByLabelText("子需求 0 标题");
    await userEvent.clear(title);
    await userEvent.type(title, "预占接口 v2");
    await userEvent.click(screen.getByRole("button", { name: "增加子需求" }));
    await userEvent.type(screen.getByLabelText("子需求 1 标题"), "对账");
    await userEvent.type(screen.getByLabelText("子需求 1 需求全文"), "对账报表。验收:一致。");
    await userEvent.click(screen.getByRole("button", { name: "确认拆分" }));

    await waitFor(() =>
      expect(submitMock).toHaveBeenCalledWith("todo-split", {
        approved: true,
        children: [
          {
            title: "预占接口 v2",
            text: "实现库存预占接口。验收:预占后可查询余量。",
            blocked_by: [],
          },
          { title: "对账", text: "对账报表。验收:一致。", blocked_by: [] },
        ],
      }),
    );
  });

  it("拒绝必须带反馈,交的是裁决不是产物", async () => {
    render(<MyTodos />);
    await userEvent.click(await screen.findByText("requirement-analysis"));

    const button = await screen.findByRole("button", { name: "打回提案" });
    expect(button).toBeDisabled(); // 空反馈交不出去:没有理由的打回让下一轮原样再来。
    await userEvent.type(screen.getByLabelText("打回的反馈"), "先别拆");
    await userEvent.click(button);

    await waitFor(() =>
      expect(submitMock).toHaveBeenCalledWith("todo-split", {
        approved: false,
        feedback: "先别拆",
      }),
    );
    expect(todosMock).toHaveBeenCalledTimes(2);
  });
});

describe("交付", () => {
  it("打开一张待办时先说清楚要交什么", async () => {
    // 不写的话,人会交一份看起来对的东西然后被打回。
    render(<MyTodos />);

    await userEvent.click(await screen.findByText("code-develop"));

    expect(await screen.findByText(/task_id、changed_files、self_test/)).toBeInTheDocument();
    expect(screen.getByText("worktrees/ag-0001")).toBeInTheDocument();
  });

  it("交上去之后回到列表", async () => {
    render(<MyTodos />);
    await userEvent.click(await screen.findByText("code-develop"));
    await userEvent.clear(screen.getByLabelText("产物 JSON"));
    await userEvent.type(screen.getByLabelText("产物 JSON"), '{{"task_id":"ag-0001"}');

    await userEvent.click(screen.getByRole("button", { name: "交付" }));

    await waitFor(() => expect(submitMock).toHaveBeenCalled());
    expect(todosMock).toHaveBeenCalledTimes(2);
  });

  it("契约没过时留在原地,把同一份错误说出来", async () => {
    // **没有"跳过校验"按钮**:有那个按钮的话,"人也是一种运行时"就只剩一半。
    submitMock.mockResolvedValue({
      ok: false,
      todo: ITEM,
      detail: "result.json 不符合 schema: changed_files 是必填项",
      task_state: "",
    });
    render(<MyTodos />);
    await userEvent.click(await screen.findByText("code-develop"));

    await userEvent.click(screen.getByRole("button", { name: "交付" }));

    expect(await screen.findByText(/changed_files 是必填项/)).toBeInTheDocument();
    expect(screen.getByLabelText("产物 JSON")).toBeInTheDocument();
  });

  it("不是合法 JSON 时当场说,不白跑一次后端", async () => {
    render(<MyTodos />);
    await userEvent.click(await screen.findByText("code-develop"));
    await userEvent.clear(screen.getByLabelText("产物 JSON"));
    await userEvent.type(screen.getByLabelText("产物 JSON"), "不是 json");

    await userEvent.click(screen.getByRole("button", { name: "交付" }));

    expect(await screen.findByText(/不是合法的 JSON/)).toBeInTheDocument();
    expect(submitMock).not.toHaveBeenCalled();
  });
});
