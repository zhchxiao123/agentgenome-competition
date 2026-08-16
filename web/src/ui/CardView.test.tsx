import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { CardView } from "./CardView";

describe("CardView", () => {
  it("shows the title and the summary", () => {
    render(<CardView title="audit-trail" summary="审计记录一律独立成表" />);

    expect(screen.getByText("audit-trail")).toBeInTheDocument();
    expect(screen.getByText("审计记录一律独立成表")).toBeInTheDocument();
  });

  it("always surfaces the hit count", () => {
    // hits 是自进化机制唯一对外可见的「这条经验到底有没有用」(§10.2)。藏起来就没人
    // 看得见该淘汰谁,所以它不是可选的展示项。
    render(<CardView title="audit-trail" hits={17} />);

    expect(screen.getByText("命中 17 次")).toBeInTheDocument();
  });

  it("shows a zero hit count rather than hiding it", () => {
    // 零命中恰恰是最该被看见的那一档——用 `hits && ...` 写的话它会消失。
    render(<CardView title="没人用过的卡片" hits={0} />);

    expect(screen.getByText("命中 0 次")).toBeInTheDocument();
  });

  it("tones the confidence tag by level", () => {
    const { container, rerender } = render(<CardView title="x" confidence="high" />);
    expect(container.querySelector(".t.ok")).not.toBeNull();

    rerender(<CardView title="x" confidence="low" />);
    expect(container.querySelector(".t.bad")).not.toBeNull();
  });

  it("falls back to a neutral tone for an unknown confidence value", () => {
    const { container } = render(<CardView title="x" confidence="不确定" />);

    expect(container.querySelector(".t.mute")).not.toBeNull();
  });

  it("is only clickable when it has somewhere to go", async () => {
    const onOpen = vi.fn();
    const { rerender } = render(<CardView title="可点的" onOpen={onOpen} />);

    await userEvent.click(screen.getByRole("button"));
    expect(onOpen).toHaveBeenCalled();

    rerender(<CardView title="不可点的" />);
    expect(screen.queryByRole("button")).toBeNull();
  });
});
