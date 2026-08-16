// jest-dom 的匹配器(`toBeInTheDocument()` 等)扩展到 Vitest 的 `expect` 上。
import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Testing Library 的自动清理靠探测全局的 `afterEach` 注册——我们关掉了 Vitest 的
// `globals`(见 ADR 里"不用 globals"那条,为了不动 tsconfig 里显式的 `types: []`),
// 探测不到就不会自动清理,上一个测试 render 出的 DOM 会漏到下一个测试里。这里手动接上。
afterEach(() => {
  cleanup();
});
