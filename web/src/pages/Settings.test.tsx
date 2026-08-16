import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SettingsView } from "../api/client";
import { Settings } from "./Settings";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      settings: vi.fn(),
      updateSettings: vi.fn(),
    },
  };
});

const { api, ApiError } = await import("../api/client");
const settingsMock = vi.mocked(api.settings);
const updateMock = vi.mocked(api.updateSettings);

const SETTINGS = {
  can_edit: true,
  runtime: {
    default: "claude-code",
    default_is_explicit: true,
    runtimes: {
      "claude-code": { cmd: "claude", max_turns: 100_000_000, transport: "http", mc_cmd: "mc" },
    },
  },
  concurrency: { global_jobs: 3 },
  budgets: {
    enforce: false,
    per_task_tokens: 100_000_000,
    per_job_tokens: 100_000_000,
    session_tokens: 100_000_000,
  },
  limits: {
    max_fix_rounds: 100_000_000,
    job_timeout_s: 100_000_000,
    max_scope_grants: 2,
    split_max_children: 12,
    split_max_depth: 2,
  },
  itest: {
    compose_file: "itest/compose.yaml",
    seed_cmd: "",
    seed_service: "",
    log_tail_lines: 60,
    timeout_s: 100_000_000,
  },
  approval: { approvers: [], notify: { webhook: null } },
  genome_tasks: {
    hot_path_since_days: 180,
    concurrent_jobs: 2,
    per_task_tokens: 100_000_000,
    confirmation_reminder_hours: 24,
  },
  quality_line: { tester: "dev", adversary: "off" },
  topology: {
    default: "single",
    critique: {
      enabled: false,
      on_protected: true,
      min_modules: 2,
      min_changed_files: 5,
      max_rounds: 100_000_000,
      budget_share: 0.3,
    },
    assisted: { employees: [], confirmer: "" },
    best_of_n: {
      attempts: [],
      judge_employee: "reviewer-employee",
      judge_procedure: "code-critique",
      max_attempts: 100_000_000,
    },
  },
} satisfies SettingsView;

beforeEach(() => {
  settingsMock.mockReset();
  updateMock.mockReset();
  settingsMock.mockResolvedValue(SETTINGS);
  updateMock.mockResolvedValue({ actor: "root", section: "budgets", at: "", entrance: "web", rev: "abc" });
});

describe("系统设置页", () => {
  it("shows the effective resource limits in one place", async () => {
    render(<Settings />);

    expect(await screen.findByLabelText("单任务 token 上限")).toHaveValue(100_000_000);
    expect(screen.getByLabelText("claude-code 最大轮数")).toHaveValue(100_000_000);
    expect(screen.getByLabelText("基因组任务 token 上限")).toHaveValue(100_000_000);
    expect(screen.getByLabelText("修复轮次上限")).toHaveValue(100_000_000);
    expect(screen.getByLabelText("全局并发作业数")).toHaveValue(3);
  });

  it("saves only the edited section", async () => {
    render(<Settings />);

    const input = await screen.findByLabelText("单任务 token 上限");
    await userEvent.clear(input);
    await userEvent.type(input, "200000000");
    await userEvent.click(screen.getByRole("button", { name: "保存 Token 预算" }));

    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(1));
    expect(updateMock).toHaveBeenCalledWith("budgets", {
      ...SETTINGS.budgets,
      per_task_tokens: 200_000_000,
    });
  });

  it("is read-only when the server says this actor cannot edit", async () => {
    settingsMock.mockResolvedValue({ ...SETTINGS, can_edit: false });

    render(<Settings />);

    expect(await screen.findByText("你没有改系统设置的权限。" )).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存 Token 预算" })).toBeDisabled();
  });

  it("keeps the server error visible when a save is rejected", async () => {
    updateMock.mockRejectedValue(new ApiError(400, "额度必须大于 0"));
    render(<Settings />);

    await userEvent.click(await screen.findByRole("button", { name: "保存 Token 预算" }));

    expect(await screen.findByText("Token 预算没保存：额度必须大于 0")).toBeInTheDocument();
  });
});
