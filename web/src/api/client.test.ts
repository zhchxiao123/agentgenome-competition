/**
 * `call()` 本身没有导出——它是 `client.ts` 内部的私有实现,`api.*` 都经过它。
 * 这里借 `api.tasks()`(最简单的一个 GET)当入口,专门测 `call()` 的错误处理:
 * 别的地方(页面测试)一律 mock 掉整个 `api/client` 模块,不会再走到这段真代码。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, setWorkspace } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
  setWorkspace("");
});

function stubFetch(
  response: Partial<Response> & { json?: () => Promise<unknown>; text?: () => Promise<string> },
) {
  const fetchMock = vi.fn().mockResolvedValue(response as Response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("call (via api.tasks)", () => {
  it("resolves with the parsed JSON body on a 2xx response", async () => {
    stubFetch({ ok: true, text: () => Promise.resolve(JSON.stringify([{ id: "ag-1" }])) });

    await expect(api.tasks()).resolves.toEqual([{ id: "ag-1" }]);
  });

  it("rejects with an ApiError carrying the backend's detail message on a non-ok response", async () => {
    stubFetch({
      ok: false,
      status: 403,
      json: () => Promise.resolve({ detail: "你不在审批人名单里" }),
    });

    await expect(api.tasks()).rejects.toMatchObject(
      new ApiError(403, "你不在审批人名单里"),
    );
  });

  it("falls back to a generic HTTP-status detail when the error body isn't valid JSON", async () => {
    stubFetch({
      ok: false,
      status: 500,
      json: () => Promise.reject(new Error("not json")),
    });

    await expect(api.tasks()).rejects.toMatchObject(new ApiError(500, "HTTP 500"));
  });
});

describe("当前项目注入(PRD 44 的验收线:只发生在这一层)", () => {
  it("sends x-workspace on every call once a project is chosen", async () => {
    const fetchMock = stubFetch({ ok: true, text: () => Promise.resolve("[]") });
    setWorkspace("b");

    await api.tasks();

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect((init.headers as Record<string, string>)["x-workspace"]).toBe("b");
  });

  it("sends no header when no project is chosen (single-workspace deployments)", async () => {
    const fetchMock = stubFetch({ ok: true, text: () => Promise.resolve("[]") });

    await api.tasks();

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(init.headers as Record<string, string>).not.toHaveProperty("x-workspace");
  });

  it("turns the default-refuse 400 into an actionable '未选择项目' when none is chosen", async () => {
    // 服务端那句说的是"请求必须说明要哪一个",用户能做的动作是"去顶栏选"——补去处,
    // 原话跟在后面,不改写。
    stubFetch({
      ok: false,
      status: 400,
      json: () => Promise.resolve({ detail: "注册了 2 个工作区,请求必须说明要哪一个" }),
    });

    await expect(api.tasks()).rejects.toMatchObject({
      detail: expect.stringContaining("未选择项目"),
    });
  });

  it("leaves the backend detail alone when a project IS chosen", async () => {
    stubFetch({
      ok: false,
      status: 404,
      json: () => Promise.resolve({ detail: "没有这个工作区: gone" }),
    });
    setWorkspace("gone");

    await expect(api.tasks()).rejects.toMatchObject({ detail: "没有这个工作区: gone" });
  });

  it("uses the same workspace for session send and stream reconnect", async () => {
    const fetchMock = stubFetch({ ok: true, text: () => Promise.resolve("") });
    setWorkspace("project-b");

    await api.sendSessionMessage("s-1", "继续", () => undefined);
    await api.attachSessionStream("s-1", 4, () => undefined);

    const send = fetchMock.mock.calls[0]?.[1] as RequestInit;
    const reconnect = fetchMock.mock.calls[1]?.[1] as RequestInit;
    expect(send.headers as Record<string, string>).toMatchObject({
      "Content-Type": "application/json",
      "x-workspace": "project-b",
    });
    expect(reconnect.headers as Record<string, string>).toMatchObject({
      "x-workspace": "project-b",
    });
  });
});

describe("200 但不是 JSON", () => {
  /**
   * **真实发生过。** 开发代理漏配 `/employees`,请求落到 dev server 上,它对未知路径一律
   * 回 200 + `index.html`。于是 `response.ok` 为真、解析抛 `SyntaxError`,而全站那些
   * `.catch((e: ApiError) => setError(e.detail))` 拿到的 `detail` 是 `undefined`——错误框
   * 判空不显示,新建会话的员工下拉就**安静地空着**,看起来像"一个员工都没有"。
   */
  it("fails loudly instead of resolving with garbage", async () => {
    stubFetch({
      ok: true,
      status: 200,
      headers: new Headers({ "content-type": "text/html" }),
      text: () => Promise.resolve("<!doctype html><html></html>"),
    });

    await expect(api.tasks()).rejects.toBeInstanceOf(ApiError);
  });

  it("says which path came back wrong and what to check", async () => {
    // 错误信息要能指导下一步。「解析失败」这四个字对用户毫无用处。
    stubFetch({
      ok: true,
      status: 200,
      headers: new Headers({ "content-type": "text/html" }),
      text: () => Promise.resolve("<!doctype html>"),
    });

    await expect(api.tasks()).rejects.toMatchObject({
      detail: expect.stringContaining("/tasks"),
    });
    await expect(api.tasks()).rejects.toMatchObject({
      detail: expect.stringContaining("代理"),
    });
  });
});
