---
status: accepted
---

# Frontend tests mock at the `api/client` / `api/live` module boundary, not the network layer

`web/` had zero test infrastructure. Adding it (Vitest + jsdom + Testing Library), we chose to
mock `src/api/client.ts` and `src/api/live.ts` directly (`vi.mock(...)`) in page/component tests,
rather than intercepting `fetch`/`EventSource` with something like MSW.

`client.ts`'s types are generated from the backend's OpenAPI schema specifically so a backend
field rename fails loudly instead of silently returning `undefined` to a page. Hand-written MSW
response fixtures would reintroduce exactly that class of drift one layer up — the fixture and the
real response shape live in different files with nothing forcing them to match, and it would go
undetected by `tsc`. Mocking at the module boundary instead means every test fixture is a value of
`TaskSummary` / `TaskDetail` / etc., so a schema change that breaks a fixture is a `tsc` error —
caught by the `typecheck` step already wired into `npm run build`, no separate response-shape
maintenance required.

The cost: `client.ts`'s own `call()` (header merging, `ApiError` construction on non-ok responses)
and `live.ts`'s own `subscribe()` (the `EventSource` `onmessage` glue) are never exercised by any
page test, since pages never see the real implementation. Each gets one small dedicated test that
stubs `fetch` / `EventSource` directly, so that logic isn't left completely uncovered.

## Considered options

- **MSW** (mock at the network layer): more realistic end-to-end exercise of the fetch path, but
  requires a second, hand-maintained set of response fixtures that can drift from the real backend
  contract without `tsc` ever noticing.
