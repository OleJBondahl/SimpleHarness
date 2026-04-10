# SimpleHarness — Overview

> **Navigation aid.** This article shows WHERE things live (routes, models, files). Read actual source files before implementing new features or making changes.

**SimpleHarness** is a python project built with raw-http.

## Scale

13 environment variables

## Required Environment Variables

- `DEAL_RAISE` — `claude-tools\debug_doctor_v3.py`
- `SIMPLEHARNESS_APPROVER_FAKE` — `claude-tools\verify_task3_fake.py`
- `SIMPLEHARNESS_APPROVER_MODEL` — `claude-tools\verify_task3_fake.py`
- `SIMPLEHARNESS_APPROVER_TIMEOUT` — `claude-tools\verify_task3_fix_stderr.py`
- `SIMPLEHARNESS_AVAILABLE_SKILLS` — `src\simpleharness\hooks\inform_skills.py`
- `SIMPLEHARNESS_ENFORCEMENT` — `src\simpleharness\hooks\enforce_must_use.py`
- `SIMPLEHARNESS_MUST_USE_MAIN` — `src\simpleharness\hooks\enforce_must_use.py`
- `SIMPLEHARNESS_MUST_USE_SUB` — `src\simpleharness\hooks\enforce_must_use.py`
- `SIMPLEHARNESS_ROLE` — `claude-tools\verify_task3_fake.py`
- `SIMPLEHARNESS_SANDBOX` — `src\simpleharness\shell.py`
- `SIMPLEHARNESS_STREAM_LOG` — `claude-tools\verify_task3_fake.py`
- `SIMPLEHARNESS_TASK_SLUG` — `claude-tools\verify_task3_fake.py`
- _...1 more_

---
_Back to [index.md](./index.md) · Generated 2026-04-10_