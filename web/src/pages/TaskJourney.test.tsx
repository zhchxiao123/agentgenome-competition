import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TaskJourney, buildTaskJourney } from "./TaskJourney";

let line = 0;

function event(
  actor: string,
  kind: string,
  payload: Record<string, unknown>,
  actorKind?: string,
) {
  line += 1;
  return {
    line,
    text: JSON.stringify({
      ts: `2026-01-01T00:00:${String(line).padStart(2, "0")}+00:00`,
      actor,
      ...(actorKind ? { actor_kind: actorKind } : {}),
      kind,
      payload,
    }),
  };
}

function transition(from: string, to: string, name: string, reason = "", actor = "orchestrator") {
  return event(actor, "transition", { from, to, event: name, reason }, actor === "orchestrator" ? "orchestrator" : "human");
}

function jobStarted(actor: string, stage: string, round = 1) {
  return event(actor, "job_started", { procedure_ref: `${stage}@1`, stage, round }, "employee");
}

function jobFinished(actor: string, ok = true, tokens = 0, duration = 0) {
  return event(
    actor,
    "job_finished",
    { procedure_ref: "p@1", ok, tokens_used: tokens, tokens_available: true, duration_s: duration },
    "employee",
  );
}

describe("TaskJourney", () => {
  it("reconstructs retries and backtracking instead of pretending the flow is linear", () => {
    const steps = buildTaskJourney([
      transition("CREATED", "DEVELOPING", "plan_done"),
      transition("DEVELOPING", "UNIT_TESTING", "dev_done"),
      transition("UNIT_TESTING", "DEVELOPING", "gate_fail"),
      transition("DEVELOPING", "UNIT_TESTING", "dev_done"),
    ], "UNIT_TESTING", "running");

    expect(steps.map(({ label, attempt, status }) => [label, attempt, status])).toEqual([
      ["需求解析", 1, "passed"],
      ["开发", 1, "passed"],
      ["单元测试", 1, "rework"],
      ["开发", 2, "passed"],
      ["单元测试", 2, "active"],
    ]);
  });

  it("attributes each segment to whoever actually ran it, with that segment's cost", () => {
    const steps = buildTaskJourney([
      jobStarted("plan-employee", "plan"),
      jobFinished("plan-employee", true, 12_400, 42),
      transition("CREATED", "DEVELOPING", "plan_done"),
      jobStarted("dev-employee", "develop"),
      jobFinished("dev-employee", true, 128_000, 252),
      transition("DEVELOPING", "UNIT_TESTING", "dev_done"),
      event("gate-runner", "gate_result", { module: "core", passed: false }, "gate"),
      transition("UNIT_TESTING", "DEVELOPING", "gate_fail"),
    ], "DEVELOPING", "idle");

    // 成本挂在**跑出它的那一段**上,不是整条轨迹一个总数。
    expect(steps.map((step) => step.actors.map((actor) => actor.name))).toEqual([
      ["plan-employee"],
      ["dev-employee"],
      ["gate-runner"],
      [],
    ]);
    expect(steps[1]!.actors[0]).toMatchObject({ kind: "employee", tokens: 128_000, durationS: 252, running: false });
    expect(steps[2]!.actors[0]).toMatchObject({ kind: "gate", failed: true, tokens: 0 });
  });

  it("names the employee still running so the live segment says who is working", () => {
    const steps = buildTaskJourney([
      transition("CREATED", "DEVELOPING", "plan_done"),
      jobStarted("dev-employee", "develop", 2),
    ], "DEVELOPING", "running");

    const current = steps[steps.length - 1]!;
    expect(current.actors).toEqual([
      { name: "dev-employee", kind: "employee", runs: 1, running: true, tokens: 0, durationS: 0, failed: false },
    ]);
  });

  it("counts repeated dispatches within one segment instead of collapsing them to one", () => {
    const steps = buildTaskJourney([
      jobStarted("dev-employee", "develop"),
      jobFinished("dev-employee", false, 1_000, 10),
      jobStarted("dev-employee", "develop"),
      jobFinished("dev-employee", true, 2_000, 20),
      transition("DEVELOPING", "UNIT_TESTING", "dev_done"),
    ], "UNIT_TESTING", "idle");

    expect(steps[0]!.actors[0]).toMatchObject({ runs: 2, tokens: 3_000, durationS: 30, failed: true, running: false });
  });

  it("surfaces the human who decided a transition the orchestrator did not", () => {
    const steps = buildTaskJourney([
      jobStarted("dev-employee", "develop"),
      transition("DEVELOPING", "CANCELLED", "cancel", "人工叫停", "zhang.san"),
    ], "CANCELLED", "finished");

    expect(steps[0]!.actors.map((actor) => [actor.name, actor.kind])).toEqual([
      ["dev-employee", "employee"],
      ["zhang.san", "human"],
    ]);
    // 编排器推的那些不单列——每格都标一遍等于没标。
    const orchestratorOnly = buildTaskJourney([
      transition("CREATED", "DEVELOPING", "plan_done"),
    ], "DEVELOPING", "idle");
    expect(orchestratorOnly[0]!.actors).toEqual([]);
  });

  it("shows only the path that happened and exposes each segment to keyboard users", () => {
    render(<TaskJourney
      logs={{
        items: [transition("CREATED", "DEVELOPING", "plan_done")],
        next_cursor: null,
        total: 1,
      }}
      error=""
      task={{ state: "DEVELOPING", execution_status: "running" }}
    />);

    expect(screen.getByRole("heading", { name: "实际轨迹" })).toBeInTheDocument();
    expect(screen.getByLabelText(/需求解析.*已解析/)).toHaveAttribute("tabindex", "0");
    expect(screen.getByLabelText(/开发.*执行中/)).toHaveAttribute("tabindex", "0");
    expect(screen.queryByText("合并")).not.toBeInTheDocument();
  });

  it("renders the executor and its cost, and reads both out to screen readers", () => {
    render(<TaskJourney
      logs={{
        items: [
          jobStarted("dev-employee", "develop"),
          jobFinished("dev-employee", true, 128_000, 252),
          transition("DEVELOPING", "UNIT_TESTING", "dev_done"),
        ],
        next_cursor: null,
        total: 3,
      }}
      error=""
      task={{ state: "UNIT_TESTING", execution_status: "running" }}
    />);

    expect(screen.getByText("dev-employee")).toBeInTheDocument();
    expect(screen.getByText("128k token · 4 分")).toBeInTheDocument();
    expect(screen.getByLabelText(/开发.*执行者 数字员工 dev-employee · 128k token · 4 分/)).toBeInTheDocument();
  });

  it("marks a clarification stop instead of presenting it as successful progress", () => {
    render(<TaskJourney
      logs={{
        items: [transition(
          "CREATED",
          "ESCALATED",
          "plan_failed",
          "需求信息不足，需要人工修改或澄清",
        )],
        next_cursor: null,
        total: 1,
      }}
      error=""
      task={{ state: "ESCALATED", execution_status: "finished" }}
    />);

    expect(screen.getByLabelText(/需求解析.*转人工.*需求信息不足/)).toBeInTheDocument();
    expect(screen.getByLabelText(/人工接管.*等人工/)).toBeInTheDocument();
  });

  it("says the tail is missing, not the head, when the log page is clipped", () => {
    render(<TaskJourney
      logs={{
        items: [transition("CREATED", "DEVELOPING", "plan_done")],
        next_cursor: 1,
        total: 40,
      }}
      error=""
      task={{ state: "DEVELOPING", execution_status: "running" }}
    />);

    expect(screen.getByText(/只还原了最早的 1 条事件/)).toBeInTheDocument();
  });
});
