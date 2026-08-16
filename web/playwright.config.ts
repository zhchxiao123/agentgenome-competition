import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 30_000,
  use: {
    baseURL: "http://127.0.0.1:15175",
    // macOS 开发机直接复用系统 Chrome；Linux/CI 缺省使用 `playwright install` 安装的浏览器。
    channel: process.env.PLAYWRIGHT_CHANNEL ?? (process.platform === "darwin" ? "chrome" : undefined),
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: "uv run python -m tests.fixtures.browser_flow_server",
      cwd: "..",
      url: "http://127.0.0.1:18081/health",
      reuseExistingServer: false,
    },
    {
      command: "AGENTGENOME_API=http://127.0.0.1:18081 npm run dev -- --host 127.0.0.1 --port 15175",
      url: "http://127.0.0.1:15175",
      reuseExistingServer: false,
    },
  ],
});
