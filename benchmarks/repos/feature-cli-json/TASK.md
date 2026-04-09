---
title: "Add JSON output format to report CLI"
workflow: feature-build
worksite: .
deliverables:
  - path: report.py
    description: "CLI tool with --format json support"
---

# Goal

Add a `--format json` option to the report CLI tool in `report.py`. When `--format json` is specified, output the items as a JSON array instead of the default text table.

## Success criteria

- [ ] `test_report.py::test_json_output` passes
- [ ] All existing tests still pass
- [ ] JSON output is valid and contains the same data as text output
- [ ] Default format remains text

## Boundaries

- Do not add new dependencies
- Do not change the item data
- Keep the existing text format unchanged
