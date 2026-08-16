import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { BlockItem } from "../api/client";
import { BlockRun, BlockView, ToolGroup, groupBlocks } from "./blocks";

const block = (kind: string, text = "", detail: Record<string, unknown> = {}): BlockItem =>
  ({ seq: 1, kind, text, detail }) as BlockItem;

describe("块渲染器注册表", () => {
  it("renders plain text", () => {
    render(<BlockView block={block("text", "补偿逻辑是这样的")} />);

    expect(screen.getByText("补偿逻辑是这样的")).toBeInTheDocument();
  });

  it("shows which file a tool step actually touched", () => {
    // 工具调用过程可视化是信任的来源。实现成笼统的「正在思考…」这个块就白做了
    // ——它必须显示具体在读什么。
    render(<BlockView block={block("tool-step", "Read src/order/timeout.py")} />);

    expect(screen.getByText(/src\/order\/timeout\.py/)).toBeInTheDocument();
  });

  it("keeps tool steps collapsed by default so the conclusion is not buried", async () => {
    render(<BlockView block={block("tool-step", "Read x.py", { output: "一大段输出" })} />);

    expect(screen.queryByText("一大段输出")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button"));
    expect(screen.getByText("一大段输出")).toBeInTheDocument();
  });

  it("renders a knowledge card with its hit count", () => {
    // hits 是自进化机制唯一对外可见的「这条经验到底有没有用」。
    render(
      <BlockView
        block={block("card-ref", "", { title: "audit-trail", summary: "审计独立成表", hits: 17 })}
      />,
    );

    expect(screen.getByText("audit-trail")).toBeInTheDocument();
    expect(screen.getByText("命中 17 次")).toBeInTheDocument();
  });

  it("renders a file reference as a clickable badge", () => {
    const onOpenFile = vi.fn();
    render(
      <BlockView
        block={block("file-ref", "src/order/service.py", { line: 118 })}
        onOpenFile={onOpenFile}
      />,
    );

    return userEvent.click(screen.getByText(/src\/order\/service\.py/)).then(() => {
      expect(onOpenFile).toHaveBeenCalledWith("src/order/service.py", 118);
    });
  });

  it("renders a diff with add and delete lines coloured", () => {
    const { container } = render(
      <BlockView block={block("diff", "+ added\n- removed\n  same", { file: "a.py" })} />,
    );

    expect(container.querySelectorAll(".l.add")).toHaveLength(1);
    expect(container.querySelectorAll(".l.del")).toHaveLength(1);
  });

  it("renders a gate report summary", () => {
    render(
      <BlockView
        block={block("gate-report", "", { gates: [{ id: "unit", passed: true, detail: "48/48" }] })}
      />,
    );

    expect(screen.getByText("unit")).toBeInTheDocument();
    expect(screen.getByText("48/48")).toBeInTheDocument();
  });

  it("renders a task card that links onwards", async () => {
    const onOpenTask = vi.fn();
    render(
      <BlockView
        block={block("task-card", "", { task_id: "ag-20260810-002", state: "REVIEWING" })}
        onOpenTask={onOpenTask}
      />,
    );

    await userEvent.click(screen.getByText("ag-20260810-002"));
    expect(onOpenTask).toHaveBeenCalledWith("ag-20260810-002");
  });

  it("renders action buttons next to the message they belong to", async () => {
    // 动作贴着它所属的消息。拆到右栏只会制造「两个地方都要看」的负担。
    const onAction = vi.fn();
    render(
      <BlockView
        block={block("action", "", { actions: [{ id: "escalate", label: "转为任务" }] })}
        onAction={onAction}
      />,
    );

    await userEvent.click(screen.getByText("转为任务"));
    expect(onAction).toHaveBeenCalledWith("escalate");
  });

  it("renders an error inline rather than as a global toast", () => {
    // 错误块内联呈现,不弹全局 toast 打断心流。
    render(<BlockView block={block("error", "超时了")} />);

    expect(screen.getByText(/超时了/)).toBeInTheDocument();
  });

  it("degrades an unknown block type to plain text instead of blanking", () => {
    // 后端先上线一种新块是正常的演进节奏。白屏的话前端会挡住整条链路的发布。
    render(<BlockView block={block("something-new-from-the-backend", "还是能读的")} />);

    expect(screen.getByText("还是能读的")).toBeInTheDocument();
  });

  it("does not crash when an unknown block has no text at all", () => {
    const { container } = render(<BlockView block={block("brand-new")} />);

    expect(container).toBeTruthy();
  });
});

describe("工具步骤成组", () => {
  it("collapses a run of tool calls into one container with a step count", async () => {
    // 每次调用各占一个块的话,一次三步的查证会把结论挤到屏幕外——而这个块要的是
    // "看得见它在查",不是"被过程淹没"。
    const steps = [
      block("tool-step", "Read a.py", { elapsed_s: 1.1 }),
      block("tool-step", "Read b.py", { elapsed_s: 2.5 }),
      block("tool-step", "Grep order_audit", { elapsed_s: 4.2 }),
    ];
    render(<ToolGroup blocks={steps} />);

    expect(screen.getByText(/查证过程 · 3 步/)).toBeInTheDocument();
    // 折叠态不该看见逐条内容。
    expect(screen.queryByText("Read a.py")).not.toBeInTheDocument();
  });

  it("shows the total elapsed time without expanding", async () => {
    // 不展开也知道它花了多久。这个数是后端量出来的,不是编的。
    render(
      <ToolGroup
        blocks={[
          block("tool-step", "Read a.py", { elapsed_s: 1.1 }),
          block("tool-step", "Grep x", { elapsed_s: 4.2 }),
        ]}
      />,
    );

    expect(screen.getByText(/用时 4\.2s/)).toBeInTheDocument();
  });

  it("lists every step once expanded", async () => {
    render(
      <ToolGroup
        blocks={[block("tool-step", "Read a.py"), block("tool-step", "Grep order_audit")]}
      />,
    );

    await userEvent.click(screen.getByRole("button"));

    expect(screen.getByText("Read a.py")).toBeInTheDocument();
    expect(screen.getByText("Grep order_audit")).toBeInTheDocument();
  });
});

describe("四类内容各有各的形态", () => {
  // 分组要看 seq 才分得开,这里给每一块一个不同的号。
  const at = (seq: number, kind: string, text = "", detail: Record<string, unknown> = {}) =>
    ({ seq, kind, text, detail }) as BlockItem;

  it("keeps thinking out of the prose lane", () => {
    // 这是这次改动要修的问题本身:一整段内心独白此前被当正文铺开,把结论挤到屏幕外,
    // 而它**看起来和答复一模一样**——用户没有任何线索判断自己读的是盘算还是答案。
    render(<BlockView block={block("thinking", "先看看有没有现成的手艺")} />);

    expect(screen.getByText(/思考过程/)).toBeInTheDocument();
    // 默认折叠:过程可见但不淹没结论。
    expect(screen.queryByText("先看看有没有现成的手艺")).not.toBeInTheDocument();
  });

  it("shows the thinking once expanded — hidden is not dropped", async () => {
    render(<BlockView block={block("thinking", "先看看有没有现成的手艺")} />);

    await userEvent.click(screen.getByRole("button"));

    expect(screen.getByText("先看看有没有现成的手艺")).toBeInTheDocument();
  });

  it("never merges a run of thinking with a run of tool calls", () => {
    // 只看"上一项是不是数组"的话,一串思考后面紧跟一串工具调用会被并进同一个容器,
    // 而它们是两件事。
    const grouped = groupBlocks([
      at(1, "thinking", "盘算"),
      at(2, "thinking", "再盘算"),
      at(3, "tool-step", "Read a.py"),
      at(4, "text", "结论"),
    ]);

    expect(grouped).toHaveLength(3);
    expect((grouped[0] as BlockItem[]).map((b) => b.seq)).toEqual([1, 2]);
    expect((grouped[1] as BlockItem[]).map((b) => b.seq)).toEqual([3]);
    // 正文不进任何泳道,永远独占一项——它是结论。
    expect(Array.isArray(grouped[2])).toBe(false);
  });

  it("gathers everything a sub-agent did into one place", () => {
    // 摊平的话,一次派出去的子任务会在主流里插进十几行它自己的盘算与查证,而用户问的
    // 是主线那个问题。
    const grouped = groupBlocks([
      at(1, "tool-step", "Task 查一下审计链路", { tool_use_id: "toolu_1" }),
      at(2, "thinking", "我先 grep", { parent_tool_use_id: "toolu_1" }),
      at(3, "tool-step", "Grep audit", { parent_tool_use_id: "toolu_1" }),
      at(4, "text", "子员工的结论", { parent_tool_use_id: "toolu_1" }),
      at(5, "text", "主线的结论"),
    ]);

    expect(grouped).toHaveLength(3);
    expect((grouped[1] as BlockItem[]).map((b) => b.seq)).toEqual([2, 3, 4]);
  });

  it("labels the sub-agent run as its own and keeps it collapsed", () => {
    const run = [
      at(2, "thinking", "我先 grep", { parent_tool_use_id: "toolu_1" }),
      at(3, "tool-step", "Grep audit", { parent_tool_use_id: "toolu_1" }),
    ];
    render(<BlockRun blocks={run} />);

    expect(screen.getByText(/子员工 · 2 步/)).toBeInTheDocument();
    expect(screen.queryByText(/查证过程/)).not.toBeInTheDocument();
  });

  it("renders each kind in its own form inside the sub-agent run", async () => {
    // 展开之后,子员工自己的思考/查证/结论仍然各按各的形态渲染——它不是一坨纯文本。
    const run = [
      at(2, "thinking", "我先 grep", { parent_tool_use_id: "toolu_1" }),
      at(3, "tool-step", "Grep audit", { parent_tool_use_id: "toolu_1" }),
      at(4, "text", "子员工的结论", { parent_tool_use_id: "toolu_1" }),
    ];
    render(<BlockRun blocks={run} />);

    await userEvent.click(screen.getByRole("button", { name: /子员工/ }));

    expect(screen.getByText(/思考过程/)).toBeInTheDocument();
    expect(screen.getByText(/Grep audit/)).toBeInTheDocument();
    expect(screen.getByText("子员工的结论")).toBeInTheDocument();
  });
});

describe("任务卡的完整信息", () => {
  it("shows state, risk and fix rounds so the situation is judgeable", async () => {
    // 一个第 3/3 轮还在 REVIEWING 的高风险任务,和一个第 1/3 轮的低风险任务,下一步动作
    // 完全不同——而只给 id 和状态时它们长得一样。
    render(
      <BlockView
        block={block("task-card", "", {
          task_id: "ag-1",
          title: "下单写一条审计记录",
          state: "REVIEWING",
          risk: "高风险",
          fix_rounds: 1,
          max_fix_rounds: 3,
        })}
      />,
    );

    expect(screen.getByText("REVIEWING")).toBeInTheDocument();
    expect(screen.getByText("高风险")).toBeInTheDocument();
    expect(screen.getByText(/修复 1\/3 轮/)).toBeInTheDocument();
  });
});
