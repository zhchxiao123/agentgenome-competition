import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SettingsView } from "../api/client";
import { Roster } from "./Roster";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      roster: vi.fn(),
      settings: vi.fn(),
      updateSettings: vi.fn(),
      setExecution: vi.fn(),
    },
  };
});

const { api, ApiError } = await import("../api/client");
const rosterMock = vi.mocked(api.roster);
const settingsMock = vi.mocked(api.settings);
const updateMock = vi.mocked(api.updateSettings);
const rungMock = vi.mocked(api.setExecution);

const MEMBER = {
  runtime: "claude-code",
  execution: "auto",
  assignee: "",
  confirmer: "",
  summary: "",
  appearances: 0,
  tokens: 0,
};

const SETTINGS = {
  can_edit: true,
  concurrency: {},
  budgets: {},
  limits: {},
  itest: {},
  approval: {},
  quality_line: { tester: "dedicated", adversary: "off" },
  topology: {
    default: "single",
    critique: {
      enabled: false,
      on_protected: true,
      min_modules: 2,
      min_changed_files: 5,
      max_rounds: 2,
      budget_share: 0.3,
    },
    assisted: { employees: [], confirmer: "" },
    best_of_n: { attempts: [], judge_employee: "reviewer-employee", max_attempts: 5 },
  },
} as unknown as SettingsView;

beforeEach(() => {
  rosterMock.mockReset();
  settingsMock.mockReset();
  updateMock.mockReset();
  rosterMock.mockResolvedValue({
    employees: [
      { ...MEMBER, id: "dev-employee", name: "开发员工", appearances: 3, tokens: 12000 },
      { ...MEMBER, id: "adversary-employee", name: "对抗员工" },
    ],
    dials: [
      { key: "tester", value: "dedicated", note: "dev=开发兼任" },
      { key: "adversary", value: "off", note: "off=不上场" },
      { key: "reviewer", value: "off", note: "精化环" },
    ],
  });
  settingsMock.mockResolvedValue(SETTINGS);
  rungMock.mockReset();
  rungMock.mockResolvedValue({
    actor: "root",
    section: "employees/dev-employee",
    at: "",
    entrance: "web",
    rev: "abc",
  });
  updateMock.mockResolvedValue({
    actor: "root",
    section: "quality_line",
    at: "",
    entrance: "web",
    rev: "abc",
  });
});

describe("员工管理页", () => {
  it("shows appearances and cost per employee", async () => {
    render(<Roster />);

    const row = (await screen.findByText("开发员工")).closest("tr");
    expect(row).not.toBeNull();
    expect(within(row!).getByText("3")).toBeInTheDocument();
    expect(within(row!).getByText("12,000")).toBeInTheDocument();
  });

  it("lists an employee that never showed up instead of hiding it", async () => {
    // 「这个项目根本没用过对抗」与「页面上没有这一行」是完全不同的两件事。
    render(<Roster />);

    const row = (await screen.findByText("对抗员工")).closest("tr");
    expect(within(row!).getByText("未上场")).toBeInTheDocument();
  });

  it("puts each dial on the setting that is actually in force, not on the summary", async () => {
    // dial 是给人看的档位摘要,阈值与确认名单在里面根本不存在。表单要听真正的配置。
    rosterMock.mockResolvedValue({
      employees: [],
      dials: [{ key: "tester", value: "dev", note: "摘要跟配置对不上时,听配置的" }],
    });

    render(<Roster />);

    expect(await screen.findByLabelText("测试分工")).toHaveValue("dedicated");
  });

  it("saves only the section that was changed", async () => {
    // 一次写入覆盖多段,会把别人这期间改的另一段一起盖回旧值。
    render(<Roster />);

    await userEvent.selectOptions(await screen.findByLabelText("对抗 QA"), "always");
    await userEvent.click(screen.getByRole("button", { name: "保存质量线" }));

    expect(updateMock).toHaveBeenCalledTimes(1);
    expect(updateMock).toHaveBeenCalledWith("quality_line", {
      tester: "dedicated",
      adversary: "always",
    });
  });

  it("opens the loop and its thresholds in one move", async () => {
    // 「开关」与「开到多深」是一个动作,不是两次跳转。
    render(<Roster />);

    expect(screen.queryByLabelText("轮次上限")).not.toBeInTheDocument();
    await userEvent.selectOptions(await screen.findByLabelText("评审精化环"), "on");

    expect(await screen.findByLabelText("轮次上限")).toHaveValue(2);
    expect(screen.getByLabelText("模块数达到就进环")).toHaveValue(2);
  });

  it("writes the loop switch into the topology section", async () => {
    render(<Roster />);

    await userEvent.selectOptions(await screen.findByLabelText("评审精化环"), "on");
    await userEvent.click(screen.getByRole("button", { name: "保存精化环" }));

    expect(updateMock).toHaveBeenCalledTimes(1);
    const [section, value] = updateMock.mock.calls[0] as [string, { critique: { enabled: boolean } }];
    expect(section).toBe("topology");
    expect(value.critique.enabled).toBe(true);
  });

  it("puts the dial back when the write did not land", async () => {
    // 配置写入要么都成功要么都回滚。留一个视觉上已生效的档位,是在伪造后端不认的状态。
    updateMock.mockRejectedValue(new ApiError(503, "配置没能提交进版本库，这次修改已回滚"));
    render(<Roster />);

    await userEvent.selectOptions(await screen.findByLabelText("对抗 QA"), "always");
    await userEvent.click(screen.getByRole("button", { name: "保存质量线" }));

    expect(await screen.findByText(/没改成/)).toBeInTheDocument();
    expect(screen.getByLabelText("对抗 QA")).toHaveValue("off");
  });

  it("disables the dials for someone who may not edit", async () => {
    settingsMock.mockResolvedValue({ ...SETTINGS, can_edit: false });
    render(<Roster />);

    expect(await screen.findByLabelText("测试分工")).toBeDisabled();
    expect(screen.getByText(/没有改配置的权限/)).toBeInTheDocument();
  });

  it("does not crash on a workspace whose roster is empty", async () => {
    rosterMock.mockResolvedValue({ employees: [], dials: [] });

    render(<Roster />);

    expect(await screen.findByText(/还没有员工定义/)).toBeInTheDocument();
  });

  it("offers the three rungs of the trust climb on one control", async () => {
    // 人要表达的是"这个角色我信到什么程度",不是"这件事存在哪个文件里"。
    render(<Roster />);

    const picker = await screen.findByLabelText("开发员工的执行档位");
    expect(Array.from(picker.querySelectorAll("option")).map((item) => item.getAttribute("value")))
      .toEqual(["auto", "assisted", "manual"]);
  });

  it("asks the server to move the rung, whichever store it lands in", async () => {
    // 三档住在两个存储上,分派在服务端做完了——前端拼一遍的话,拼错的那一次会造出
    // "human 却又在确认名单里"这种谁都解释不了的状态。
    render(<Roster />);

    await userEvent.selectOptions(await screen.findByLabelText("开发员工的执行档位"), "assisted");

    expect(rungMock).toHaveBeenCalledWith("dev-employee", "assisted", "");
    expect(updateMock).not.toHaveBeenCalled();
  });

  it("asks who the work belongs to before moving anyone to manual", async () => {
    // 没有主人的待办不会被任何人看到,只会静默超时。
    render(<Roster />);

    await userEvent.selectOptions(await screen.findByLabelText("开发员工的执行档位"), "manual");

    expect(rungMock).not.toHaveBeenCalled();
    expect(await screen.findByLabelText("开发员工的指派人")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "转成 manual" })).toBeDisabled();
  });

  it("moves to manual once someone owns the work", async () => {
    render(<Roster />);

    await userEvent.selectOptions(await screen.findByLabelText("开发员工的执行档位"), "manual");
    await userEvent.type(await screen.findByLabelText("开发员工的指派人"), "alice");
    await userEvent.click(screen.getByRole("button", { name: "转成 manual" }));

    expect(rungMock).toHaveBeenCalledWith("dev-employee", "manual", "alice");
  });

  it("lets someone name the confirmer on the assisted rung too", async () => {
    // 填了不落下去比不给填更糟:人以为自己给这些活指了个主人,而实际上没有。
    rosterMock.mockResolvedValue({
      employees: [
        {
          ...MEMBER,
          id: "dev-employee",
          name: "开发员工",
          execution: "assisted",
          confirmer: "alice",
        },
      ],
      dials: [],
    });
    render(<Roster />);

    const input = await screen.findByLabelText("开发员工的指派人");
    await userEvent.clear(input);
    await userEvent.type(input, "bob");
    await userEvent.click(screen.getByRole("button", { name: "保存指派人" }));

    expect(rungMock).toHaveBeenCalledWith("dev-employee", "assisted", "bob");
  });

  it("re-reads the settings after a rung moves, so a later save cannot undo it", async () => {
    // 真实发生过的坏法:挪完档位之后本地那份 topology 还是旧的确认名单,接着按「保存
    // 精化环」就把旧名单原样写了回去——档位悄悄回退,而两次保存都提示成功。
    render(<Roster />);

    await userEvent.selectOptions(await screen.findByLabelText("开发员工的执行档位"), "assisted");

    await waitFor(() => expect(settingsMock).toHaveBeenCalledTimes(2));
  });

  it("edits the employee's own assignee, and says when a project-wide confirmer wins", async () => {
    // 两者合成一格的话:显示的是全局值、改的却是员工自己的字段,于是保存成功而显示不变。
    rosterMock.mockResolvedValue({
      employees: [
        {
          ...MEMBER,
          id: "dev-employee",
          name: "开发员工",
          execution: "assisted",
          assignee: "alice",
          confirmer: "全局审核组",
        },
      ],
      dials: [],
    });
    render(<Roster />);

    expect(await screen.findByLabelText("开发员工的指派人")).toHaveValue("alice");
    expect(screen.getByText(/全局审核组/)).toBeInTheDocument();
  });

  it("puts the rung back when the write did not land", async () => {
    rungMock.mockRejectedValue(new ApiError(503, "配置没能提交进版本库"));
    render(<Roster />);

    await userEvent.selectOptions(await screen.findByLabelText("开发员工的执行档位"), "assisted");

    expect(await screen.findByText(/没改成/)).toBeInTheDocument();
    expect(screen.getByLabelText("开发员工的执行档位")).toHaveValue("auto");
  });

  it("still shows the roster when the settings cannot be read", async () => {
    settingsMock.mockRejectedValue(new ApiError(500, "读不到"));
    render(<Roster />);

    expect(await screen.findByText("开发员工")).toBeInTheDocument();
  });
});
