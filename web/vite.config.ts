import { readFileSync } from "node:fs";
import react from "@vitejs/plugin-react";
// 从 `vitest/config` 取 `defineConfig`(它是 `vite` 那份的超集,多一个 `test` 字段),
// 不建第二份 `vitest.config.ts`——两份配置各改各的,迟早在 alias、插件这些地方分叉。
import { defineConfig } from "vitest/config";

/**
 * 该代理到后端的路径前缀,**从 OpenAPI 契约里算出来,不手写**。
 *
 * 手写过一版(`^/(tasks|events|metrics|health|api)`),然后 `/sessions`、`/employees`、
 * `/workspaces` 陆续加进来都没人想起改它。漏配的表现极其隐蔽:请求落到 dev server 上,
 * 它对未知路径一律回 200 + `index.html`,于是前端拿到 HTML 当数据,页面**安静地空着**
 * ——员工下拉里一个人都没有,而且没有任何报错。
 *
 * 从契约推导之后,加一个端点就自动进代理,这条配置不可能再落后于后端。
 */
function apiPrefixes(): string[] {
  const spec = JSON.parse(readFileSync("../docs/openapi.json", "utf-8")) as {
    paths: Record<string, unknown>;
  };
  const roots = new Set<string>();
  for (const path of Object.keys(spec.paths)) {
    const head = path.split("/")[1];
    // 带路径参数的段(`{task_id}`)不能当前缀——它不是一个固定的名字。
    if (head && !head.startsWith("{")) roots.add(head);
  }
  return [...roots].sort();
}

/**
 * 后端在哪。默认 `agctl serve` 的默认端口,**可以用环境变量顶掉**。
 *
 * 写死 8080 的代价是:那个端口被别的东西占着时(开发容器里常见 code-server、
 * Jupyter 之类),请求会被代理到一个**长得完全不像后端的服务**,而它回的 HTML 或 302
 * 到了前端就是"数据不对劲"。改端口不该需要改代码。
 */
const API = process.env.AGENTGENOME_API ?? "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react()],
  // 开发时代理到 `agctl serve`,这样前端不需要知道后端地址,也不用配 CORS。
  server: {
    // 结尾那组**必须带 `?`**:vite 拿 `req.url` 去匹配,而它是**含 query string** 的。
    // 只写 `(/|$)` 的话,`/sessions?state=active` 这种带参数的请求会整条漏出去——
    // 而漏出去的表现又是那个"200 + index.html"的安静失败。
    proxy: { [`^/(${apiPrefixes().join("|")})([/?]|$)`]: API },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["./src/test/setup.ts"],
  },
});
