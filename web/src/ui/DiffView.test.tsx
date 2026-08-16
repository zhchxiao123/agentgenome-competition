import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DiffView, parseDiff } from "./DiffView";

describe("DiffView", () => {
  it("colours added and removed lines differently", () => {
    const { container } = render(
      <DiffView lines={[{ text: "+ new", kind: "add" }, { text: "- old", kind: "del" }, { text: "  same" }]} />,
    );

    expect(container.querySelectorAll(".l.add")).toHaveLength(1);
    expect(container.querySelectorAll(".l.del")).toHaveLength(1);
    // 上下文行不该被着色——全都染上的话「哪里变了」就看不出来了。
    expect(container.querySelectorAll(".l:not(.add):not(.del)")).toHaveLength(1);
  });

  it("keeps blank lines occupying a row so line numbers stay aligned with the file", () => {
    const { container } = render(<DiffView lines={[{ text: "a" }, { text: "" }, { text: "c" }]} onLineClick={() => {}} />);

    expect(container.querySelectorAll(".l")).toHaveLength(3);
    expect(screen.getByText("3")).toBeInTheDocument();
  });

  it("only shows line numbers when lines are clickable", () => {
    const { container, rerender } = render(<DiffView lines={[{ text: "a" }]} />);
    expect(container.querySelector(".mono")).toBeNull();

    rerender(<DiffView lines={[{ text: "a" }]} onLineClick={() => {}} />);
    expect(container.querySelector(".mono")).not.toBeNull();
  });

  it("reports which line was clicked", async () => {
    const onLineClick = vi.fn();
    render(<DiffView lines={[{ text: "a" }, { text: "b" }]} onLineClick={onLineClick} />);

    await userEvent.click(screen.getByText("b"));

    expect(onLineClick).toHaveBeenCalledWith(2);
  });

  it("renders extra content under a specific line", () => {
    render(
      <DiffView
        lines={[{ text: "a" }, { text: "b" }]}
        renderUnderLine={(no) => (no === 2 ? <div>挂在第二行</div> : null)}
      />,
    );

    expect(screen.getByText("挂在第二行")).toBeInTheDocument();
  });

  it("uses the line's own number when given, not its position", () => {
    render(<DiffView lines={[{ text: "a", no: 118 }]} onLineClick={() => {}} />);

    expect(screen.getByText("118")).toBeInTheDocument();
  });

  it("renders the file header only when a file is given", () => {
    const { rerender, container } = render(<DiffView lines={[{ text: "a" }]} />);
    expect(container.querySelector(".fh")).toBeNull();

    rerender(<DiffView file="src/order/service.py" stat="+6" lines={[{ text: "a" }]} />);
    expect(screen.getByText(/src\/order\/service\.py/)).toBeInTheDocument();
    expect(screen.getByText("+6")).toBeInTheDocument();
  });
});

describe("parseDiff", () => {
  it("classifies added and removed lines", () => {
    expect(parseDiff("+add\n-del\n ctx")).toEqual([
      { text: "+add", kind: "add" },
      { text: "-del", kind: "del" },
      { text: " ctx" },
    ]);
  });

  it("does not mistake the +++/--- file header for a change", () => {
    // 文件头以 + / - 开头,但它不是增删行。当成增删的话每个 diff 顶上都会挂两行假的绿红。
    expect(parseDiff("+++ b/x\n--- a/x")).toEqual([{ text: "+++ b/x" }, { text: "--- a/x" }]);
  });
});
