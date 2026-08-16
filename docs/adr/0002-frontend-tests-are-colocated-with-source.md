---
status: accepted
---

# Frontend tests are co-located with source, not mirrored into a separate `tests/` tree

The Python backend splits tests into `tests/unit/` and `tests/e2e/` at the repo root, separating
fast unit tests from slow ones that materialize real git repos. The new `web/` test suite
deliberately does not mirror that layout: test files live next to the source they cover
(`Component.test.tsx` beside `Component.tsx`), the idiomatic Vitest/Vite convention, picked up by
Vitest's default file glob with no extra config.

There's no slow-integration tier on the frontend to justify a two-tier split — every test in the
first batch runs in jsdom with no real I/O. If one shows up later (e.g. a Playwright e2e layer
that drives a real browser against a running `agctl serve`), that's the trigger to introduce
`web/tests/e2e/` alongside the co-located unit tests — not to move the unit tests into a mirrored
`web/tests/unit/` to match the Python side.
