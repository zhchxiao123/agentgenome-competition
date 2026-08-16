import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Markdown } from "./Markdown";

describe("Markdown", () => {
  it("renders emphasis and lists as elements, not raw syntax", () => {
    render(<Markdown text={"**加粗** 一句话\n\n- 第一条\n- 第二条"} />);

    expect(screen.getByText("加粗").tagName).toBe("STRONG");
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });

  it("renders a fenced code block through LogViewer's pre.log, not a nested <pre>", () => {
    const { container } = render(<Markdown text={"```\nls -la\n```"} />);

    const pre = container.querySelector("pre.log");
    expect(pre).not.toBeNull();
    expect(pre!.textContent).toBe("ls -la");
    expect(container.querySelectorAll("pre")).toHaveLength(1);
  });

  it("renders a short inline code span without pulling in LogViewer", () => {
    const { container } = render(<Markdown text={"跑一下 `npm test`"} />);

    expect(container.querySelector("pre.log")).toBeNull();
    expect(screen.getByText("npm test").tagName).toBe("CODE");
  });

  it("does not execute raw HTML embedded in the text", () => {
    const { container } = render(<Markdown text={'<img src=x onerror="window.__pwned = true">'} />);

    expect(container.querySelector("img")).toBeNull();
    expect((window as unknown as { __pwned?: boolean }).__pwned).toBeUndefined();
  });

  it("says nothing instead of rendering an empty wrapper", () => {
    const { container } = render(<Markdown text="" />);

    expect(container.firstChild).toBeNull();
  });
});
