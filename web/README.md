# AgentGenome 控制台

## 客户端由后端的 OpenAPI 生成，不手写

```bash
npm run gen        # docs/openapi.json → src/api/schema.d.ts
npm run typecheck  # 契约不匹配在这一步红
```

手写客户端的话，后端改一个字段名前端不会报错——只会在运行时拿到一个 `undefined`，而那时
离改动已经很远了。`npm run build` 把生成放在 `tsc` 之前，所以**契约漂移在构建期就暴露**。

## 没有私有通道

所有数据走 `docs/openapi.json` 里列出的那些端点。命令行与网页走完全相同的接口——两条路各走
各的实现，它们会慢慢分叉，而分叉一旦形成就很难收回。

## 推拉结合

SSE（`GET /events/stream`）只承载「有变化了」。页面数据**始终以主动拉取为准**，断线重连自动
补齐。做成数据通道的话，断线那一刻页面就永久停在旧状态。

`src/api/live.ts` 里那个 `subscribe` 的回调只做一件事：触发一次重新拉取。

## 测试

```bash
npm run test        # vitest run —— 单次跑完，退出码反映结果
npm run test:watch  # vitest —— 开发时用
```

Mock 只换 `src/api/client.ts`/`src/api/live.ts` 这两个模块本身，不拦截 `fetch`/`EventSource`——
理由与"客户端由 OpenAPI 生成"那条是同一个:测试用的 fixture 是 `TaskSummary` 等生成类型的值，
后端字段改名时 `tsc` 会在 fixture 上报错，不需要再手维护一套独立的响应形状。取舍记在
`docs/adr/0001-frontend-tests-mock-at-the-api-module-boundary.md`。

测试文件挨着源码放（`Component.test.tsx`），不进单独的 `tests/` 目录——理由记在
`docs/adr/0002-frontend-tests-are-colocated-with-source.md`。

## 设计参照

`docs/design/console-p0.html` 是可点击原型，并且逐条对齐过后端实际能力——里面记了 8 处
「参考设计有、但后端给不出」的地方（ESCALATED 泳道、风险评级是二值不是分数、没有
Tester/Reviewer 员工……）。实现时以那份为准。
