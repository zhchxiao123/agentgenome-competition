import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { runtimeConfig, runtimeEntry } from "../api/fixtures";
import { ContainerRuntimeCard } from "./ContainerRuntimeCard";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      updateSettings: vi.fn(),
      containerRuntimeReadiness: vi.fn(),
    },
  };
});

const { api, ApiError } = await import("../api/client");
const updateMock = vi.mocked(api.updateSettings);
const readinessMock = vi.mocked(api.containerRuntimeReadiness);

const CONFIGURED = runtimeConfig({
  "claude-code": runtimeEntry({ cmd: "claude" }),
  agentteams: runtimeEntry({
    transport: "matrix-minio",
    endpoint: "http://controller.example.com",
    consumer_token_env: "AGENTTEAMS_CONSUMER_TOKEN",
    matrix_homeserver: "http://matrix.example.com",
    matrix_token_env: "AGENTTEAMS_MATRIX_TOKEN",
    storage_prefix: "agentteams/agentteams-storage/shared",
  }),
});

const UNCONFIGURED = runtimeConfig({ "claude-code": runtimeEntry({ cmd: "claude" }) });

beforeEach(() => {
  vi.clearAllMocks();
  updateMock.mockResolvedValue({} as never);
  readinessMock.mockResolvedValue({ ok: true, items: [] });
});

describe("ContainerRuntimeCard", () => {
  it("未配置容器运行时时默认收起——一个没启用的能力不该让界面变复杂", () => {
    render(<ContainerRuntimeCard runtime={UNCONFIGURED} canEdit onSaved={() => {}} />);

    expect(screen.queryByLabelText(/平台入口/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /容器运行时/ })).toBeInTheDocument();
  });

  it("已配置时把现值铺进表单——界面是改一个已知状态,不是重填一份配置", () => {
    render(<ContainerRuntimeCard runtime={CONFIGURED} canEdit onSaved={() => {}} />);

    expect(screen.getByLabelText(/平台入口/)).toHaveValue("http://controller.example.com");
    expect(screen.getByLabelText(/存储前缀/)).toHaveValue(
      "agentteams/agentteams-storage/shared",
    );
  });

  it("保存时按段提交,且把整份运行时配置一起带上", async () => {
    render(<ContainerRuntimeCard runtime={CONFIGURED} canEdit onSaved={() => {}} />);

    await userEvent.clear(screen.getByLabelText(/平台入口/));
    await userEvent.type(screen.getByLabelText(/平台入口/), "http://new.example.com");
    await userEvent.click(screen.getByRole("button", { name: /保存/ }));

    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(1));
    const call = updateMock.mock.calls[0]!;
    expect(call[0]).toBe("runtime");
    const written = call[1] as { runtimes: Record<string, { endpoint?: string; cmd?: string }> };
    expect(written.runtimes.agentteams?.endpoint).toBe("http://new.example.com");
    expect(written.runtimes["claude-code"]?.cmd).toBe("claude");
  });

  it("保存失败时把原因说出来,且不假装已生效", async () => {
    updateMock.mockRejectedValue(new ApiError(400, "storage_prefix 不能为空"));
    render(<ContainerRuntimeCard runtime={CONFIGURED} canEdit onSaved={() => {}} />);

    await userEvent.click(screen.getByRole("button", { name: /保存/ }));

    expect(await screen.findByText(/storage_prefix 不能为空/)).toBeInTheDocument();
  });

  it("就绪检查把每一项分别列出来", async () => {
    readinessMock.mockResolvedValue({
      ok: false,
      items: [
        { name: "platform", ok: true, detail: "平台入口可达" },
        { name: "storage", ok: false, detail: "桶列不出来" },
        { name: "matrix", ok: true, detail: "令牌有效" },
        { name: "credentials", ok: false, detail: "缺 AGENTTEAMS_MATRIX_TOKEN" },
      ],
    });
    render(<ContainerRuntimeCard runtime={CONFIGURED} canEdit onSaved={() => {}} />);

    await userEvent.click(screen.getByRole("button", { name: /就绪检查/ }));

    expect(await screen.findByText(/桶列不出来/)).toBeInTheDocument();
    expect(screen.getByText(/缺 AGENTTEAMS_MATRIX_TOKEN/)).toBeInTheDocument();
    expect(screen.getByText(/平台入口可达/)).toBeInTheDocument();
  });

  it("就绪检查本身失败时说清楚,而不是留一个空结果", async () => {
    readinessMock.mockRejectedValue(new ApiError(400, "这个工作区没有配置 agentteams 运行时"));
    render(<ContainerRuntimeCard runtime={CONFIGURED} canEdit onSaved={() => {}} />);

    await userEvent.click(screen.getByRole("button", { name: /就绪检查/ }));

    expect(await screen.findByText(/没有配置 agentteams/)).toBeInTheDocument();
  });

  it("没有改配置权限时表单只读,按钮点不动", () => {
    render(<ContainerRuntimeCard runtime={CONFIGURED} canEdit={false} onSaved={() => {}} />);
    // 就绪检查那个端点同样要 edit_settings:亮着但永远 403 的按钮比灰着更糟。
    expect(screen.getByRole("button", { name: /就绪检查/ })).toBeDisabled();

    expect(screen.getByLabelText(/平台入口/)).toBeDisabled();
    expect(screen.getByRole("button", { name: /保存/ })).toBeDisabled();
  });
});
