import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Activity, localToIso, planeOf } from "./Activity";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, api: { ...actual.api, auditEvents: vi.fn(), workspaces: vi.fn() } };
});

const { api, ApiError } = await import("../api/client");
const eventsMock = vi.mocked(api.auditEvents);
const spacesMock = vi.mocked(api.workspaces);

afterEach(() => vi.unstubAllEnvs());

function event(overrides: Record<string, unknown> = {}) {
  return {
    task_id: "system",
    ts: "2026-01-01T00:00:00+00:00",
    actor: "root",
    actor_kind: "human",
    kind: "config_changed",
    payload: { rev: "abcdef1234567890", section: "budgets" },
    ...overrides,
  } as AwaitedItems;
}
type AwaitedItems = Awaited<ReturnType<typeof api.auditEvents>>["items"][number];

beforeEach(() => {
  eventsMock.mockReset();
  eventsMock.mockResolvedValue({ items: [event()] });
  spacesMock.mockReset();
  spacesMock.mockResolvedValue({ items: ["default"] });
});

describe("Activity", () => {
  it("defaults to a preset window rather than the whole log", async () => {
    // 默认全量倒序的话它就是一条谁也读不完的流水,而一个读不完的页面等于没有。
    render(<Activity />);
    await screen.findByText("config_changed");

    expect(eventsMock.mock.calls[0]![0]!.since).toBeTruthy();
  });

  it("filters by workspace when the server serves more than one", async () => {
    // 多项目环境下审计员只看他关心的那个。让人手打空间名的话,打错一个字的结果是一页空的
    // 检索结果——与"这个空间里真的没有记录"分不开。
    spacesMock.mockResolvedValue({ items: ["alpha", "beta"] });
    render(<Activity />);

    await userEvent.selectOptions(await screen.findByLabelText("按工作空间过滤"), "beta");

    expect(eventsMock.mock.calls.at(-1)![0]!.workspace).toBe("beta");
  });

  it("never asks for all workspaces at once", async () => {
    // 后端没有跨空间查询:不带 workspace 的请求在多空间部署上直接 400,于是"全部"这个
    // 选项每次都是一次失败——比没有它更糟。
    spacesMock.mockResolvedValue({ items: ["alpha", "beta"] });
    render(<Activity />);

    const picker = await screen.findByLabelText("按工作空间过滤");
    expect(within(picker).queryByText(/全部/)).not.toBeInTheDocument();
    expect(eventsMock.mock.calls.at(-1)![0]!.workspace).toBe("alpha");
  });

  it("asks the server for the patrol kinds instead of filtering a page in the browser", async () => {
    // 前端过滤的话,一个繁忙的 24 小时里那一页可能全是 Job 事件,于是页面自信地说"没有
    // 人为动作",而它们只是排在后面。
    render(<Activity />);
    await screen.findByText("config_changed");

    const sent = eventsMock.mock.calls[0]![0]!.kinds ?? "";
    expect(sent).toContain("approval");
    // 人取消任务、回答闸门记的都是迁移——漏掉它们的话这个视角看不见它自己要看的那批介入。
    expect(sent).toContain("transition");
  });

  it("hides the workspace picker in a single-workspace deployment", async () => {
    render(<Activity />);
    await screen.findByText("config_changed");

    expect(screen.queryByLabelText("按工作空间过滤")).not.toBeInTheDocument();
  });

  it("filters by actor kind", async () => {
    render(<Activity />);
    await screen.findByText("config_changed");

    await userEvent.selectOptions(screen.getByLabelText("按行为主体类别过滤"), "employee");

    expect(eventsMock.mock.calls.at(-1)![0]!.actor_kind).toBe("employee");
  });

  it("filters by a specific actor", async () => {
    render(<Activity />);
    await screen.findByText("config_changed");

    await userEvent.type(screen.getByLabelText("按行为主体过滤"), "alice");

    expect(eventsMock.mock.calls.at(-1)![0]!.actor).toBe("alice");
  });

  it("filters by event kind", async () => {
    render(<Activity />);
    await screen.findByText("config_changed");

    await userEvent.type(screen.getByLabelText("按事件类型过滤"), "approval");

    expect(eventsMock.mock.calls.at(-1)![0]!.kind).toBe("approval");
  });

  it("filters by an explicit time range instead of the preset", async () => {
    render(<Activity />);
    await screen.findByText("config_changed");

    await userEvent.type(screen.getByLabelText("按时间范围过滤"), "2026-01-01T08:00");

    expect(eventsMock.mock.calls.at(-1)![0]!.since).toContain("2026-01-01");
  });

  it("converts the local datetime picker to a real instant", async () => {
    // 原样送过去的话,一个东八区的审计员会拿到一个整整差八小时的窗口——而界面上它与正确的
    // 窗口一模一样。
    expect(localToIso("2026-01-01T08:00")).toBe(new Date("2026-01-01T08:00").toISOString());
  });

  it("keeps UTC, DST and cross-day boundaries correct in different browser timezones", () => {
    vi.stubEnv("TZ", "UTC");
    expect(localToIso("2026-01-01T08:00")).toBe("2026-01-01T08:00:00.000Z");

    vi.stubEnv("TZ", "America/Los_Angeles");
    expect(localToIso("2026-01-01T08:00")).toBe("2026-01-01T16:00:00.000Z");
    expect(localToIso("2026-07-01T08:00")).toBe("2026-07-01T15:00:00.000Z");

    vi.stubEnv("TZ", "Asia/Tokyo");
    expect(localToIso("2026-01-01T00:30")).toBe("2025-12-31T15:30:00.000Z");
  });

  it("says which plane each record's content lives on", async () => {
    // 不标平面的话,人顺着往下查时不知道该去哪儿看——而那正是汇聚的意义所在。
    eventsMock.mockResolvedValue({
      items: [
        event(),
        event({ kind: "approval", task_id: "ag-0001", payload: {} }),
        event({ kind: "job_finished", task_id: "ag-0001", payload: {} }),
      ],
    });

    render(<Activity />);

    const config = (await screen.findByText("config_changed")).closest("tr");
    expect(within(config as HTMLElement).getByText("版本面")).toBeInTheDocument();
    const approval = screen.getByText("approval").closest("tr");
    expect(within(approval as HTMLElement).getByText("事件面")).toBeInTheDocument();
  });

  it("links a config change to the commit that carries the content", async () => {
    // 内容以版本面为准:事件里本来就没有前值后值。标签说"内容在提交里"却不带人过去的话,
    // 这一页仍然是终点,而它存在的意义正是让人顺着看下去。
    render(<Activity />);

    const link = await screen.findByText(/提交 abcdef12/);
    expect(link).toHaveAttribute("href", "#/commits/abcdef1234567890");
  });

  it("shows human intervention in the stream", async () => {
    eventsMock.mockResolvedValue({
      items: [event({ kind: "approval", actor: "alice", task_id: "ag-0001", payload: {} })],
    });

    render(<Activity />);

    expect(await screen.findByText("alice")).toBeInTheDocument();
  });

  it("says it could not load rather than showing an empty stream", async () => {
    // 空流与"查不出来"在界面上长得一模一样,而后者是要修的。
    eventsMock.mockRejectedValue(new ApiError(500, "audit store unreachable"));

    render(<Activity />);

    expect(await screen.findByText(/拉不到活动流/)).toHaveTextContent("audit store unreachable");
  });
});

describe("planeOf", () => {
  it("sends config and genome changes to the version plane", () => {
    expect(planeOf("config_changed").plane).toBe("版本面");
    expect(planeOf("genome_pr").plane).toBe("版本面");
    expect(planeOf("job_finished").plane).toBe("日志面");
    expect(planeOf("approval").plane).toBe("事件面");
  });
});
