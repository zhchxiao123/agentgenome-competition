import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { GenomeTaskSummary, WorkspaceCreated } from "../api/client";
import { ProjectSetup } from "./ProjectSetup";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: { ...actual.api, createWorkspace: vi.fn(), knowledgeInit: vi.fn() },
  };
});

const { api, ApiError } = await import("../api/client");
const createMock = vi.mocked(api.createWorkspace);
const initMock = vi.mocked(api.knowledgeInit);

beforeEach(() => {
  createMock.mockReset();
  initMock.mockReset();
});

describe("ProjectSetup", () => {
  async function fillRequiredProjectFields(businessRepo = "a.git") {
    await userEvent.type(screen.getByPlaceholderText(/切换器里的名字/), "shop");
    await userEvent.type(
      screen.getByPlaceholderText(/agentgenome-workspace/),
      "https://github.com/acme/shop-workspace.git",
    );
    if (businessRepo) {
      await userEvent.type(screen.getByPlaceholderText(/order-service/), businessRepo);
    }
  }

  it("creates the project with one repo per line and @branch kept intact", async () => {
    createMock.mockResolvedValue({ name: "shop", mount_task_id: "gn-1" } as WorkspaceCreated);
    const onCreated = vi.fn();
    render(<ProjectSetup first onCreated={onCreated} />);

    await fillRequiredProjectFields("a.git\nb.git@release");
    await userEvent.click(screen.getByRole("button", { name: "创建项目" }));

    expect(createMock).toHaveBeenCalledWith({
      name: "shop",
      workspace_repo: "https://github.com/acme/shop-workspace.git",
      repos: ["a.git", "b.git@release"],
    });
    expect(onCreated).toHaveBeenCalledWith(expect.objectContaining({ name: "shop", mount_task_id: "gn-1" }));
    // 建成后的引导:挂载任务号可见,下一步是知识初始化。
    expect(await screen.findByText("gn-1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发起知识初始化" })).toBeInTheDocument();
  });

  it("kicks off knowledge init from the guide (走 POST /genome/tasks/init)", async () => {
    createMock.mockResolvedValue({ name: "shop", mount_task_id: "gn-1" } as WorkspaceCreated);
    initMock.mockResolvedValue({ id: "gn-2" } as GenomeTaskSummary);
    render(<ProjectSetup first onCreated={vi.fn()} />);

    await fillRequiredProjectFields();
    await userEvent.click(screen.getByRole("button", { name: "创建项目" }));
    await userEvent.click(await screen.findByRole("button", { name: "发起知识初始化" }));

    expect(initMock).toHaveBeenCalled();
    expect(await screen.findByText(/知识初始化已发起/)).toBeInTheDocument();
  });

  it("tells apart adoption of an orphaned directory from a fresh create", async () => {
    // 重启前建过的项目:目录在、注册没了。再点创建是认领,不再有挂载任务。
    createMock.mockResolvedValue({
      name: "shop",
      mount_task_id: null,
      adopted: true,
    } as WorkspaceCreated);
    render(<ProjectSetup first={false} onCreated={vi.fn()} />);

    await fillRequiredProjectFields();
    await userEvent.click(screen.getByRole("button", { name: "创建项目" }));

    expect(await screen.findByText(/已重新接入/)).toBeInTheDocument();
    expect(screen.getByText(/业务仓已就绪/)).toBeInTheDocument();
    expect(screen.queryByText(/挂载进度/)).not.toBeInTheDocument();
  });

  it("shows the backend's refusal verbatim (重名/名字不合法)", async () => {
    createMock.mockRejectedValue(new ApiError(409, "已有这个项目: shop"));
    render(<ProjectSetup first={false} onCreated={vi.fn()} />);

    await fillRequiredProjectFields();
    await userEvent.click(screen.getByRole("button", { name: "创建项目" }));

    expect(await screen.findByText(/已有这个项目/)).toBeInTheDocument();
  });

  it("refuses locally when no repo is given", async () => {
    render(<ProjectSetup first onCreated={vi.fn()} />);

    await fillRequiredProjectFields("");
    await userEvent.click(screen.getByRole("button", { name: "创建项目" }));

    expect(createMock).not.toHaveBeenCalled();
    expect(screen.getByText(/至少一个业务仓库地址/)).toBeInTheDocument();
  });

  it("requires a dedicated Git repository for the Workspace", async () => {
    render(<ProjectSetup first onCreated={vi.fn()} />);

    await userEvent.type(screen.getByPlaceholderText(/切换器里的名字/), "shop");
    await userEvent.type(screen.getByPlaceholderText(/order-service/), "a.git");
    await userEvent.click(screen.getByRole("button", { name: "创建项目" }));

    expect(createMock).not.toHaveBeenCalled();
    expect(screen.getByText(/项目名、顶层项目仓库地址与/)).toBeInTheDocument();
  });

  it("creates at most once while the request is in flight", async () => {
    let resolve!: (created: WorkspaceCreated) => void;
    createMock.mockReturnValue(new Promise((done) => { resolve = done; }));
    const onCreated = vi.fn();
    render(<ProjectSetup first onCreated={onCreated} />);
    await fillRequiredProjectFields();

    const button = screen.getByRole("button", { name: "创建项目" });
    fireEvent.click(button);
    fireEvent.click(button);

    expect(createMock).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "创建中…" })).toBeDisabled();
    resolve({ name: "shop", mount_task_id: null, adopted: false });
    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
  });
});
