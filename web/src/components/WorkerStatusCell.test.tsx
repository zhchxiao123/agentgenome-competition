import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { WorkerStatusView } from "../api/client";
import { WorkerStatusCell } from "./WorkerStatusCell";

/** 一行状态。服务端总是把五个字段都回全,所以这里也补全,只覆盖关心的那几个。 */
const row = (over: Partial<WorkerStatusView>): WorkerStatusView => ({
  employee_id: "arch-employee",
  status: "absent",
  worker: "",
  room: "",
  detail: "",
  ...over,
});

describe("WorkerStatusCell", () => {
  it("已供应的显示 Worker 名与房间", () => {
    render(
      <WorkerStatusCell
        status={row({
          status: "running",
          worker: "agenome-arch-employee",
          room: "!abc:example.com",
          detail: "Running",
        })}
      />,
    );

    expect(screen.getByText(/运行中/)).toBeInTheDocument();
    expect(screen.getByText("agenome-arch-employee")).toBeInTheDocument();
    expect(screen.getByText("!abc:example.com")).toBeInTheDocument();
  });

  it("休眠与运行中读起来不一样——一个是省钱,一个是在岗", () => {
    render(
      <WorkerStatusCell status={row({ status: "sleeping", worker: "agenome-a", room: "!r:x" })} />,
    );

    expect(screen.getByText(/休眠中/)).toBeInTheDocument();
    expect(screen.queryByText(/运行中/)).not.toBeInTheDocument();
  });

  it("没供应过就说没供应过", () => {
    render(<WorkerStatusCell status={row({ status: "absent" })} />);

    expect(screen.getByText(/未供应/)).toBeInTheDocument();
  });

  it("**平台没答上来与没供应过要分得开**——否则平台一挂,人会去点一次多余的供应", () => {
    render(
      <WorkerStatusCell status={row({ status: "unknown", detail: "连接被拒绝" })} />,
    );

    expect(screen.getByText(/读不到/)).toBeInTheDocument();
    expect(screen.getByText(/连接被拒绝/)).toBeInTheDocument();
    expect(screen.queryByText(/未供应/)).not.toBeInTheDocument();
  });

  it("跑在本地的员工这一格是空的,不是「未供应」", () => {
    const { container } = render(<WorkerStatusCell status={undefined} />);

    expect(container.textContent).toBe("");
  });
});
