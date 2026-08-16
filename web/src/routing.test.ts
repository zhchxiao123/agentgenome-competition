import { describe, expect, it } from "vitest";
import { hrefFor, parseHash, routeForPage } from "./routing";

describe("控制台 Hash 路由", () => {
  it("restores pages, tabs, and object details from shareable hashes", () => {
    expect(parseHash("#/tasks/ag-20260814-001")).toEqual({
      page: "tasks",
      taskId: "ag-20260814-001",
    });
    expect(parseHash("#/requirements/req-20260814-001")).toEqual({
      page: "requirements",
      requirementId: "req-20260814-001",
    });
    expect(parseHash("#/genome/lessons")).toEqual({ page: "genome", tab: "lessons" });
    expect(parseHash("#/insights/audit")).toEqual({ page: "insights", tab: "audit" });
  });

  it("normalizes empty and malformed locations without inventing an object", () => {
    expect(parseHash("")).toEqual({ page: "work" });
    expect(parseHash("#/tasks/%E0%A4%A")).toEqual({ page: "not-found", requested: "/tasks/%E0%A4%A" });
    expect(parseHash("#/unknown/place")).toEqual({ page: "not-found", requested: "/unknown/place" });
  });

  it("builds every navigation target through the same encoder", () => {
    expect(hrefFor({ page: "tasks", taskId: "ag/with space" })).toBe("#/tasks/ag%2Fwith%20space");
    expect(hrefFor(routeForPage("review"))).toBe("#/review");
    expect(hrefFor(routeForPage("settings"))).toBe("#/settings");
    expect(hrefFor({ page: "requirements" })).toBe("#/requirements");
  });
});
