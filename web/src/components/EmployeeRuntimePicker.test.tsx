import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EmployeeRuntimePicker } from "./EmployeeRuntimePicker";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      employeeRuntime: vi.fn(),
      setEmployeeRuntime: vi.fn(),
      declareCompat: vi.fn(),
    },
  };
});

const { api, ApiError } = await import("../api/client");
const choiceMock = vi.mocked(api.employeeRuntime);
const setMock = vi.mocked(api.setEmployeeRuntime);
const compatMock = vi.mocked(api.declareCompat);

beforeEach(() => {
  vi.clearAllMocks();
  choiceMock.mockResolvedValue({
    current: "claude-code",
    options: ["claude-code", "agentteams"],
    can_edit: true,
    compat_gap: [],
    blocked: [],
  });
  setMock.mockResolvedValue({} as never);
  compatMock.mockResolvedValue({} as never);
});

describe("EmployeeRuntimePicker", () => {
  it("选项来自服务端,不是前端自己编的", async () => {
    render(<EmployeeRuntimePicker employee="arch-employee" />);

    const select = await screen.findByLabelText(/运行时/);
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "claude-code",
      "agentteams",
    ]);
  });

  it("切换时把选择发给服务端", async () => {
    render(<EmployeeRuntimePicker employee="arch-employee" />);
    const select = await screen.findByLabelText(/运行时/);

    await userEvent.selectOptions(select, "agentteams");

    await waitFor(() => expect(setMock).toHaveBeenCalledWith("arch-employee", "agentteams"));
  });

  it("切换失败时说清原因,且选择回到生效值", async () => {
    setMock.mockRejectedValue(new ApiError(422, "qwen-code 不是这次部署配置过的机器运行时"));
    render(<EmployeeRuntimePicker employee="arch-employee" />);
    const select = await screen.findByLabelText(/运行时/);

    await userEvent.selectOptions(select, "agentteams");

    expect(await screen.findByText(/不是这次部署配置过的/)).toBeInTheDocument();
    expect(select).toHaveValue("claude-code");
  });

  it("有兼容缺口时列出来,并给一个显式的补声明按钮", async () => {
    choiceMock.mockResolvedValue({
      current: "claude-code",
      options: ["claude-code", "agentteams"],
      can_edit: true,
      compat_gap: ["requirement-analysis", "code-develop"],
      blocked: [],
    });
    render(<EmployeeRuntimePicker employee="arch-employee" candidate="agentteams" />);

    expect(await screen.findByText(/requirement-analysis/)).toBeInTheDocument();
    expect(screen.getByText(/code-develop/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /声明兼容/ })).toBeInTheDocument();
  });

  it("**不点按钮就什么都不发生**——自动补声明等于把兼容闸变成摆设", async () => {
    choiceMock.mockResolvedValue({
      current: "claude-code",
      options: ["claude-code", "agentteams"],
      can_edit: true,
      compat_gap: ["requirement-analysis"],
      blocked: [],
    });
    render(<EmployeeRuntimePicker employee="arch-employee" candidate="agentteams" />);
    await screen.findByText(/requirement-analysis/);

    expect(compatMock).not.toHaveBeenCalled();
  });

  it("点了才补,且只补这个候选运行时", async () => {
    choiceMock.mockResolvedValue({
      current: "claude-code",
      options: ["claude-code", "agentteams"],
      can_edit: true,
      compat_gap: ["requirement-analysis"],
      blocked: [],
    });
    render(<EmployeeRuntimePicker employee="arch-employee" candidate="agentteams" />);

    await userEvent.click(await screen.findByRole("button", { name: /声明兼容/ }));

    await waitFor(() =>
      expect(compatMock).toHaveBeenCalledWith("arch-employee", "agentteams", [
        "requirement-analysis",
      ]),
    );
  });

  it("改成了要通知父组件——花名册那一行的运行时是它的数据,不该有第二份", async () => {
    const onChanged = vi.fn();
    render(<EmployeeRuntimePicker employee="arch-employee" onChanged={onChanged} />);
    const select = await screen.findByLabelText(/运行时/);

    await userEvent.selectOptions(select, "agentteams");

    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("**改失败时不通知**——通知了父组件会去重读,而它读回来的还是旧值", async () => {
    const onChanged = vi.fn();
    setMock.mockRejectedValue(new ApiError(422, "没配过这个运行时"));
    render(<EmployeeRuntimePicker employee="arch-employee" onChanged={onChanged} />);
    const select = await screen.findByLabelText(/运行时/);

    await userEvent.selectOptions(select, "agentteams");

    expect(await screen.findByText(/没配过/)).toBeInTheDocument();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("没有改配置权限时选不动", async () => {
    choiceMock.mockResolvedValue({
      current: "claude-code",
      options: ["claude-code", "agentteams"],
      can_edit: false,
      compat_gap: [],
      blocked: [],
    });
    render(<EmployeeRuntimePicker employee="arch-employee" />);

    expect(await screen.findByLabelText(/运行时/)).toBeDisabled();
  });
});

describe("EmployeeRuntimePicker 的拦截提示", () => {
  it("**接不了这个员工的运行时选不动,并说清为什么**——补声明也解决不了它", async () => {
    choiceMock.mockResolvedValue({
      current: "claude-code",
      options: ["claude-code", "agentteams"],
      can_edit: true,
      compat_gap: [],
      blocked: [
        { runtime: "agentteams", reason: "运行时 agentteams 不能强制只读，不能承接员工 reviewer-employee 的只读 Job" },
      ],
    });
    render(<EmployeeRuntimePicker employee="reviewer-employee" />);

    const select = await screen.findByLabelText(/运行时/);
    const blocked = within(select)
      .getAllByRole("option")
      .find((o) => o.getAttribute("value") === "agentteams");
    expect(blocked).toBeDisabled();
    expect(await screen.findByText(/不能强制只读/)).toBeInTheDocument();
  });

  it("**否定断言**:没被拦的运行时照常可选,拦截提示也不出现", async () => {
    render(<EmployeeRuntimePicker employee="dev-employee" />);

    const select = await screen.findByLabelText(/运行时/);
    for (const option of within(select).getAllByRole("option")) {
      expect(option).not.toBeDisabled();
    }
    expect(screen.queryByText(/接不了/)).not.toBeInTheDocument();
  });
});

describe("EmployeeRuntimePicker 的两条提示不互相拆台", () => {
  it("**接不了的运行时不该再给补声明按钮**——补了也跑不到,而那次点击写进 git", async () => {
    choiceMock.mockResolvedValue({
      current: "claude-code",
      options: ["claude-code", "agentteams"],
      can_edit: true,
      // 服务端在这一种情况下不再回缺口(见 app.runtime_choice)。这里同形复现。
      compat_gap: [],
      blocked: [{ runtime: "agentteams", reason: "运行时 agentteams 不能强制只读" }],
    });
    render(<EmployeeRuntimePicker employee="reviewer-employee" candidate="agentteams" />);

    expect(await screen.findByText(/不能强制只读/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /声明兼容/ })).not.toBeInTheDocument();
  });
});
