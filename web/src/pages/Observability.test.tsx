import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { Observability } from "./Observability";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      notificationPreferences: vi.fn(),
      saveNotificationPreference: vi.fn(),
      auditEvents: vi.fn(),
    },
  };
});
vi.mock("../api/live", () => ({ subscribe: vi.fn(() => () => undefined) }));

const preferencesMock = vi.mocked(api.notificationPreferences);
const auditMock = vi.mocked(api.auditEvents);

beforeEach(() => {
  preferencesMock.mockReset();
  auditMock.mockReset();
  auditMock.mockResolvedValue({ items: [] });
});

describe("Observability notification identity", () => {
  it("loads the saved preference for the shared declared identity", async () => {
    preferencesMock.mockResolvedValue({
      items: [{ actor: "alice", events: ["approved"], webhook_url: "https://example.test/hook" }],
    });

    render(<Observability tab="notify" actor="alice" />);

    expect(await screen.findByDisplayValue("https://example.test/hook")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "approved" })).toBeChecked();
    expect(screen.getByText("alice")).toBeInTheDocument();
  });

  it("does not call an empty configuration a successful load", async () => {
    preferencesMock.mockResolvedValue({ items: [] });

    render(<Observability tab="notify" actor="alice" />);

    expect(await screen.findByText("alice 尚未配置通知偏好")).toBeInTheDocument();
  });
});

describe("Observability audit window", () => {
  it("sends datetime-local values as real instants", async () => {
    const { container } = render(<Observability tab="audit" />);
    await vi.waitFor(() => expect(auditMock).toHaveBeenCalled());
    const inputs = container.querySelectorAll<HTMLInputElement>('input[type="datetime-local"]');

    await userEvent.type(inputs[0]!, "2026-01-01T08:00");
    await userEvent.click(screen.getByRole("button", { name: "检索" }));

    expect(auditMock.mock.calls.at(-1)?.[0]?.since).toBe(new Date("2026-01-01T08:00").toISOString());
  });
});
