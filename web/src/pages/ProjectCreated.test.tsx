import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { ProjectCreated } from "./ProjectCreated";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: { ...actual.api, genomeTasks: vi.fn(), knowledgeInit: vi.fn(), auditEvents: vi.fn() },
  };
});

const genomeTasksMock = vi.mocked(api.genomeTasks);
const knowledgeInitMock = vi.mocked(api.knowledgeInit);
const auditMock = vi.mocked(api.auditEvents);

beforeEach(() => {
  genomeTasksMock.mockReset();
  knowledgeInitMock.mockReset();
  auditMock.mockReset();
  auditMock.mockResolvedValue({
    items: [{ task_id: "system", ts: "2026-08-14T10:00:00Z", actor: "alice", actor_kind: "human", kind: "workspace_changed", payload: { action: "create", name: "shop" } }],
  });
  genomeTasksMock.mockResolvedValue({
    items: [{ id: "gn-mount-1", state: "DEEP_READ", kind: "mount" }],
  } as Awaited<ReturnType<typeof api.genomeTasks>>);
});

describe("ProjectCreated", () => {
  it("recovers the mount task after the result URL is refreshed", async () => {
    render(<ProjectCreated workspace="shop" initial={null} />);

    expect(await screen.findByText("gn-mount-1")).toBeInTheDocument();
    expect(genomeTasksMock).toHaveBeenCalledWith({ kind: "mount" });
  });

  it("recovers an adopted project result after refresh", async () => {
    auditMock.mockResolvedValue({
      items: [{ task_id: "system", ts: "2026-08-14T10:00:00Z", actor: "alice", actor_kind: "human", kind: "workspace_changed", payload: { action: "adopt", name: "shop" } }],
    });

    render(<ProjectCreated workspace="shop" initial={null} />);

    expect(await screen.findByRole("heading", { name: /已重新接入/ })).toBeInTheDocument();
  });

  it("keeps mount recovery usable when audit access is unavailable", async () => {
    auditMock.mockRejectedValue(new Error("forbidden"));

    render(<ProjectCreated workspace="shop" initial={null} />);

    expect(await screen.findByText("gn-mount-1")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "项目 shop 创建结果" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发起知识初始化" })).toBeEnabled();
  });

  it("keeps knowledge initialization as the next step", async () => {
    knowledgeInitMock.mockResolvedValue({ id: "gn-init-1" } as Awaited<ReturnType<typeof api.knowledgeInit>>);
    render(<ProjectCreated workspace="shop" initial={null} />);

    await userEvent.click(await screen.findByRole("button", { name: "发起知识初始化" }));

    expect(await screen.findByText(/gn-init-1/)).toBeInTheDocument();
  });
});
