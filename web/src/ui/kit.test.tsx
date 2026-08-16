import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Card, Empty, Note, Tag, stateTone } from "./kit";

describe("stateTone", () => {
  it("gives ESCALATED the most alarming tone — it's the state that most needs a human", () => {
    expect(stateTone("ESCALATED")).toBe("bad");
  });

  it("gives COMPLETED a positive tone", () => {
    expect(stateTone("COMPLETED")).toBe("ok");
  });

  it("falls back to a neutral tone for an unknown state instead of throwing", () => {
    expect(stateTone("SOME_FUTURE_STATE")).toBe("mute");
  });
});

describe("Tag", () => {
  it("renders its children with the given tone class", () => {
    render(<Tag tone="bad">ESCALATED</Tag>);

    expect(screen.getByText("ESCALATED")).toHaveClass("bad");
  });
});

describe("Card", () => {
  it("renders a title and its children", () => {
    render(<Card title="任务中心">内容</Card>);

    expect(screen.getByText("任务中心")).toBeInTheDocument();
    expect(screen.getByText("内容")).toBeInTheDocument();
  });

  it("omits the heading entirely when no title is given", () => {
    render(<Card>只有内容</Card>);

    expect(screen.queryByRole("heading")).not.toBeInTheDocument();
  });
});

describe("Empty", () => {
  it("renders the given message", () => {
    render(<Empty>空</Empty>);

    expect(screen.getByText("空")).toBeInTheDocument();
  });
});

describe("Note", () => {
  it("applies the warn class when tone is warn", () => {
    const { container } = render(<Note tone="warn">小心</Note>);

    expect(container.querySelector(".note.warn")).not.toBeNull();
  });
});
