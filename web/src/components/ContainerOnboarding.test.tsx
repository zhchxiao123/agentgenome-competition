import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SettingsView } from "../api/client";
import { runtimeConfig, runtimeEntry } from "../api/fixtures";
import { ContainerOnboarding } from "./ContainerOnboarding";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: { ...actual.api, settings: vi.fn(), workerStatuses: vi.fn() },
  };
});

const { api } = await import("../api/client");
const settingsMock = vi.mocked(api.settings);
const statusMock = vi.mocked(api.workerStatuses);

// **配置 fixture 走 `api/fixtures`,不是 `as never`。** ADR-0001 的回报是"后端改一个
// 字段名前端编译期就红",而 `as never` 正好把那条抹掉。
const CONFIGURED = {
  runtime: runtimeConfig({ agentteams: runtimeEntry({ endpoint: "http://x" }) }),
} as SettingsView;
const UNCONFIGURED = { runtime: runtimeConfig() } as SettingsView;

beforeEach(() => {
  vi.clearAllMocks();
  settingsMock.mockResolvedValue(CONFIGURED);
  statusMock.mockResolvedValue({
    can_provision: true,
    items: [
      {
        employee_id: "arch-employee",
        status: "absent",
        worker: "",
        room: "",
        detail: "",
      },
    ],
  });
});

describe("ContainerOnboarding", () => {
  it("配了容器运行时却一个都没供应时,给出下一步", async () => {
    render(<ContainerOnboarding />);

    expect(await screen.findByText(/还没有员工被供应/)).toBeInTheDocument();
  });

  it("**已经供应过就不再出现**——一条永远在的提示会被当成背景色", async () => {
    statusMock.mockResolvedValue({
      can_provision: true,
      items: [
        {
          employee_id: "arch-employee",
          status: "running",
          worker: "agenome-arch-employee",
          room: "!r:x",
          detail: "",
        },
      ],
    });
    render(<ContainerOnboarding />);

    await waitFor(() => expect(statusMock).toHaveBeenCalled());
    expect(screen.queryByText(/还没有员工被供应/)).not.toBeInTheDocument();
  });

  it("休眠中也算供应过——它只是省钱,不是没建", async () => {
    statusMock.mockResolvedValue({
      can_provision: true,
      items: [
        {
          employee_id: "arch-employee",
          status: "sleeping",
          worker: "agenome-arch-employee",
          room: "!r:x",
          detail: "",
        },
      ],
    });
    render(<ContainerOnboarding />);

    await waitFor(() => expect(statusMock).toHaveBeenCalled());
    expect(screen.queryByText(/还没有员工被供应/)).not.toBeInTheDocument();
  });

  it("**没配容器运行时就不出现**——对着一个没启用的能力催人下一步是纯噪声", async () => {
    settingsMock.mockResolvedValue(UNCONFIGURED);
    render(<ContainerOnboarding />);

    await waitFor(() => expect(settingsMock).toHaveBeenCalled());
    expect(screen.queryByText(/还没有员工被供应/)).not.toBeInTheDocument();
    expect(statusMock).not.toHaveBeenCalled();
  });

  it("读不到配置时不出现,而不是拿一句猜出来的提示顶上", async () => {
    settingsMock.mockRejectedValue(new Error("403"));
    render(<ContainerOnboarding />);

    await waitFor(() => expect(settingsMock).toHaveBeenCalled());
    expect(screen.queryByText(/还没有员工被供应/)).not.toBeInTheDocument();
  });

  it("提示里给的是去哪儿,不是一句「请先配置」", async () => {
    render(<ContainerOnboarding />);
    await screen.findByText(/还没有员工被供应/);

    expect(screen.getByRole("link", { name: /员工管理/ })).toHaveAttribute("href", "#/roster");
  });
});
