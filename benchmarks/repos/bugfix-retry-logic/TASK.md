---
title: "Fix off-by-one retry bug"
workflow: feature-build
worksite: .
deliverables:
  - path: retry.py
    description: "Fixed retry implementation"
---

# Goal

The `retry_request` function in `retry.py` has an off-by-one error. When `max_retries=3`, it only retries 2 times instead of 3. Fix the bug so the retry count matches the configured `max_retries`.

## Success criteria

- [ ] `test_retry.py::test_exact_retry_count` passes
- [ ] All other existing tests still pass
- [ ] No other behavioral changes

## Boundaries

- Do not add new dependencies
- Do not change the public API of `retry_request` or `RetryConfig`
