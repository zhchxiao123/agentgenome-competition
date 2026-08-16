import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api/client";
import { NewSessionDialog } from "./NewSessionDialog";

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return {
    ...actual,
    api: { employees: vi.fn(), tasks: vi.fn(), createSession: vi.fn() },
  };
});

const mocked = api as unknown as {
  employees: ReturnType<typeof vi.fn>;
  tasks: ReturnType<typeof vi.fn>;
  createSession: ReturnType<typeof vi.fn>;
};

const EMPLOYEES = [
  { id: "arch-employee", name: "", runtime: "claude-code", can_session: true, session_blocked_reason: "" },
  {
    id: "qwen-hand",
    name: "",
    runtime: "qwen-code",
    can_session: false,
    session_blocked_reason: "运行时 qwen-code 开不了会话。换一个用支持会话的运行时的员工。",
  },
];

const TASKS = [{ id: "ag-20260810-001", title: "补偿逻辑加超时保护" }];

beforeEach(() => {
  mocked.employees.mockReset();
  mocked.tasks.mockReset();
  mocked.createSession.mockReset();
  mocked.employees.mockResolvedValue({ items: EMPLOYEES });
  mocked.tasks.mockResolvedValue(TASKS);
  mocked.createSession.mockResolvedValue({ id: "sess-1" });
});

describe("NewSessionDialog", () => {
  it("offers the employees the backend reported", async () => {
    render(<NewSessionDialog onCreated={vi.fn()} onClose={vi.fn()} />);

    expect(await screen.findByRole("option", { name: /arch-employee/ })).toBeInTheDocument();
  });

  it("greys out an employee that cannot open a session, and says why", async () => {
    // 只是灰掉的话,用户只会以为是自己没权限——而实际是这个员工的运行时开不了会话,
    // 他该做的是换一个员工。
    render(<NewSessionDialog onCreated={vi.fn()} onClose={vi.fn()} />);

    const blocked = await screen.findByRole("option", { name: /qwen-hand/ });
    expect(blocked).toBeDisabled();
    expect(blocked.textContent).toContain("开不了会话");
  });

  it("explains what each permission means", async () => {
    // 选之前就要看得懂,不该逼用户去读文档才知道该选哪个。
    render(<NewSessionDialog onCreated={vi.fn()} onClose={vi.fn()} />);

    expect(await screen.findByText(/看得见项目代码,但不会动它/)).toBeInTheDocument();

    await userEvent.click(screen.getByLabelText(/让它改代码/));

    expect(screen.getByText(/要转成任务才能进主线/)).toBeInTheDocument();
  });

  it("always offers the task picker, whatever the permission", async () => {
    // 它此前的渲染条件挂在模式上,于是「结对」那一档明明需要任务、却没有任何地方能填
    // ——界面上摆着一个 100% 失败的选项。任务现在是可选的,而"可选"意味着它任何时候都得在。
    render(<NewSessionDialog onCreated={vi.fn()} onClose={vi.fn()} />);
    await screen.findByRole("option", { name: /arch-employee/ });

    expect(await screen.findByRole("option", { name: /ag-20260810-001/ })).toBeInTheDocument();

    await userEvent.click(screen.getByLabelText(/让它改代码/));

    expect(screen.getByRole("option", { name: /ag-20260810-001/ })).toBeInTheDocument();
  });

  it("says what linking a task does, and it differs by permission", async () => {
    render(<NewSessionDialog onCreated={vi.fn()} onClose={vi.fn()} />);
    await screen.findByRole("option", { name: /arch-employee/ });

    await userEvent.selectOptions(screen.getByLabelText(/关联任务/), "ag-20260810-001");

    expect(screen.getByText(/预载这个任务的产物与日志/)).toBeInTheDocument();

    await userEvent.click(screen.getByLabelText(/让它改代码/));

    expect(screen.getByText(/在这个任务的隔离工作区里改/)).toBeInTheDocument();
  });

  it("creates a read-only session with no task by default", async () => {
    // **两个都不给是正常形态**:在项目根上只读地聊。早先最省事的那条路径是不存在的
    // ——三个 chip 里两个只读的看不见代码,可写的那个建不出来。
    render(<NewSessionDialog onCreated={vi.fn()} onClose={vi.fn()} />);
    await screen.findByRole("option", { name: /arch-employee/ });

    await userEvent.click(screen.getByRole("button", { name: "开始对话" }));

    await waitFor(() =>
      expect(mocked.createSession).toHaveBeenCalledWith(
        expect.objectContaining({ employee: "arch-employee", writable: false, task_id: null }),
      ),
    );
  });

  it("creates a writable session linked to a task", async () => {
    render(<NewSessionDialog onCreated={vi.fn()} onClose={vi.fn()} />);
    await screen.findByRole("option", { name: /arch-employee/ });

    await userEvent.click(screen.getByLabelText(/让它改代码/));
    await userEvent.selectOptions(screen.getByLabelText(/关联任务/), "ag-20260810-001");
    await userEvent.click(screen.getByRole("button", { name: "开始对话" }));

    await waitFor(() =>
      expect(mocked.createSession).toHaveBeenCalledWith(
        expect.objectContaining({
          employee: "arch-employee",
          writable: true,
          task_id: "ag-20260810-001",
        }),
      ),
    );
  });

  it("enters the session it just created", async () => {
    // 建完还要自己去列表里找它,等于把一次成功当成半次。
    const onCreated = vi.fn();
    render(<NewSessionDialog onCreated={onCreated} onClose={vi.fn()} />);
    await screen.findByRole("option", { name: /arch-employee/ });

    await userEvent.click(screen.getByRole("button", { name: "开始对话" }));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("sess-1"));
  });

  it("keeps a failure inside the dialog so it can be retried", async () => {
    // 失败把对话框关掉的话,用户要从头再填一遍——而他要改的可能只是一个选项。
    mocked.createSession.mockRejectedValue(new ApiError(409, "运行时 claude-code 没有配置。"));
    const onCreated = vi.fn();
    render(<NewSessionDialog onCreated={onCreated} onClose={vi.fn()} />);
    await screen.findByRole("option", { name: /arch-employee/ });

    await userEvent.click(screen.getByRole("button", { name: "开始对话" }));

    expect(await screen.findByText(/没有配置/)).toBeInTheDocument();
    expect(onCreated).not.toHaveBeenCalled();
    // 表单还在,能改完直接重试。
    expect(screen.getByRole("button", { name: "开始对话" })).toBeInTheDocument();
  });

  it("defaults to an employee that can actually open a session", async () => {
    // 默认选中一个必然失败的员工,等于让最常见的那条路径从一个错误开始。
    mocked.employees.mockResolvedValue({ items: [EMPLOYEES[1], EMPLOYEES[0]] });
    render(<NewSessionDialog onCreated={vi.fn()} onClose={vi.fn()} />);

    await screen.findByRole("option", { name: /arch-employee/ });
    expect(screen.getByRole("button", { name: "开始对话" })).toBeEnabled();
  });
});

describe("显示名", () => {
  it("shows the human-readable name, not the internal id", async () => {
    // `arch-employee` 不是人话——`name` 这个字段就是为此加的。
    mocked.employees.mockResolvedValue({
      items: [{ ...EMPLOYEES[0], name: "架构员工" }],
    });
    render(<NewSessionDialog onCreated={vi.fn()} onClose={vi.fn()} />);

    expect(await screen.findByRole("option", { name: "架构员工" })).toBeInTheDocument();
  });

  it("falls back to the id when no name is configured", async () => {
    mocked.employees.mockResolvedValue({ items: [{ ...EMPLOYEES[0], name: "" }] });
    render(<NewSessionDialog onCreated={vi.fn()} onClose={vi.fn()} />);

    expect(await screen.findByRole("option", { name: "arch-employee" })).toBeInTheDocument();
  });
});
