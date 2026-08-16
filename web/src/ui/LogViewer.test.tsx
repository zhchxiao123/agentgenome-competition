import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LogViewer } from "./LogViewer";

describe("LogViewer", () => {
  it("renders numbered lines with the number ahead of the text", () => {
    // 直接看 textContent:`getByText` 会把连续空格归一化,而行号与正文之间那两个空格
    // 正是要断言的东西——归一化之后这条测试就测不到它了。
    const { container } = render(<LogViewer lines={[{ line: 1, text: "起手" }, { line: 2, text: "收工" }]} />);

    expect(container.textContent).toBe("1  起手\n2  收工");
  });

  it("renders raw text as-is", () => {
    render(<LogViewer text={"+ added\n- removed"} />);

    expect(screen.getByText(/\+ added/)).toBeInTheDocument();
  });

  it("says something instead of going blank when there is nothing", () => {
    // 白屏没法区分「还没有」与「挂了」——这是 kit.tsx 的 Empty 同一个理由。
    render(<LogViewer lines={[]} />);

    expect(screen.getByText("(还没有日志)")).toBeInTheDocument();
  });

  it("lets the page override the empty message", () => {
    render(<LogViewer text="" empty="(这个任务没有产物)" />);

    expect(screen.getByText("(这个任务没有产物)")).toBeInTheDocument();
  });

  it("caps its height only when asked", () => {
    const { container, rerender } = render(<LogViewer text="x" />);
    expect((container.firstChild as HTMLElement).style.maxHeight).toBe("");

    rerender(<LogViewer text="x" maxHeight={160} />);
    expect((container.firstChild as HTMLElement).style.maxHeight).toBe("160px");
  });
});
