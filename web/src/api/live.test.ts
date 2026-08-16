/**
 * jsdom 没有 `EventSource`,这里用一个假的顶替全局,专门测 `subscribe()` 自己的逻辑:
 * URL 拼接、`onmessage` 的 JSON 解析与容错、返回的清理函数。别的地方(页面测试)一律
 * mock 掉整个 `api/live` 模块,不会再走到这段真代码。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setWorkspace } from "./client";
import { subscribe } from "./live";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  closed = false;

  constructor(public url: string) {
    FakeEventSource.instances.push(this);
  }

  close() {
    this.closed = true;
  }
}

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
  setWorkspace("");
});

describe("subscribe", () => {
  it("opens an EventSource against /events/stream", () => {
    subscribe(() => undefined);

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0]?.url).toContain("/events/stream");
  });

  it("scopes the stream to a task when a task id is given", () => {
    subscribe(() => undefined, "ag-1");

    expect(FakeEventSource.instances[0]?.url).toContain("task_id=ag-1");
  });

  it("scopes the stream to the current project (task_id 本身就是数据)", () => {
    setWorkspace("b");
    subscribe(() => undefined);

    expect(FakeEventSource.instances[0]?.url).toContain("workspace=b");
  });

  it("adds no workspace param when none is chosen", () => {
    subscribe(() => undefined);

    expect(FakeEventSource.instances[0]?.url).not.toContain("workspace=");
  });

  it("forwards a parsed notice to the callback", () => {
    const onChange = vi.fn();
    subscribe(onChange);
    const source = FakeEventSource.instances[0];

    source?.onmessage?.({ data: JSON.stringify({ task_id: "ag-1", kind: "job_finished" }) } as MessageEvent);

    expect(onChange).toHaveBeenCalledWith({ task_id: "ag-1", kind: "job_finished" });
  });

  it("swallows a malformed payload instead of throwing", () => {
    const onChange = vi.fn();
    subscribe(onChange);
    const source = FakeEventSource.instances[0];

    expect(() => source?.onmessage?.({ data: "not json" } as MessageEvent)).not.toThrow();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("closes the EventSource when the returned cleanup function runs", () => {
    const unsubscribe = subscribe(() => undefined);
    const source = FakeEventSource.instances[0];

    unsubscribe();

    expect(source?.closed).toBe(true);
  });
});
