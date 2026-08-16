import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { ChatDrawer, contextFor } from "./Drawer";

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return {
    ...actual,
    api: { createSession: vi.fn(), session: vi.fn(), sessionMessages: vi.fn() },
  };
});

const mocked = api as unknown as {
  createSession: ReturnType<typeof vi.fn>;
  session: ReturnType<typeof vi.fn>;
  sessionMessages: ReturnType<typeof vi.fn>;
};

beforeEach(() => {
  vi.clearAllMocks();
  mocked.createSession.mockResolvedValue({ id: "sess-1", writable: false });
  mocked.session.mockResolvedValue({ id: "sess-1", writable: false });
  mocked.sessionMessages.mockResolvedValue({ session_id: "sess-1", items: [], last_seq: 0 });
});

describe("键位", () => {
  it("opens on ⌘J", async () => {
    render(<ChatDrawer page="work" openTaskId={null} />);

    await userEvent.keyboard("{Meta>}j{/Meta}");

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
  });

  it("does not open on ⌘K", async () => {
    // ⌘K 是顶栏搜索的。两个功能抢同一个键位的结果是先注册的赢、后注册的静默失效
    // ——最难查的一类前端 bug。
    render(<ChatDrawer page="work" openTaskId={null} />);

    await userEvent.keyboard("{Meta>}k{/Meta}");

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("closes again on a second ⌘J", async () => {
    render(<ChatDrawer page="work" openTaskId={null} />);
    await userEvent.keyboard("{Meta>}j{/Meta}");
    await screen.findByRole("dialog");

    await userEvent.keyboard("{Meta>}j{/Meta}");

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("携带当前页面的上下文", () => {
  it("links the session to the task being viewed", () => {
    // 在看一个任务时唤起,想问的几乎一定是「这个任务为什么这么干」。
    expect(contextFor("tasks", "ag-20260810-002")).toEqual({
      employee: "dev-employee",
      task_id: "ag-20260810-002",
    });
  });

  it("asks the architect from the genome page", () => {
    expect(contextFor("genome", null)).toEqual({ employee: "arch-employee" });
  });

  it("falls back to the dev employee everywhere else", () => {
    expect(contextFor("work", null).employee).toBe("dev-employee");
  });

  it("does not claim a task when none is open", () => {
    expect(contextFor("tasks", null).task_id).toBeUndefined();
  });

  it("creates the session with the page's context", async () => {
    render(<ChatDrawer page="genome" openTaskId={null} />);

    await userEvent.keyboard("{Meta>}j{/Meta}");
    await screen.findByRole("dialog");

    expect(mocked.createSession).toHaveBeenCalledWith(
      expect.objectContaining({ employee: "arch-employee" }),
    );
  });

  it("never opens a writable session from the drawer", async () => {
    // 抽屉的定位是"就地问一句"。想边聊边改去工作台建——那里才有空间把"改动落在哪、
    // 怎么回主线"说清楚,而一个浮在角落里的抽屉没有。
    render(<ChatDrawer page="genome" openTaskId={null} />);

    await userEvent.keyboard("{Meta>}j{/Meta}");
    await screen.findByRole("dialog");

    expect(mocked.createSession).toHaveBeenCalledWith(
      expect.objectContaining({ writable: false }),
    );
  });
});
