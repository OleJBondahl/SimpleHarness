# CLI Error Classifier & Retry/Backoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the harness resilient to transient Claude CLI failures during unattended container operation by classifying errors, applying backoff, and surfacing permanent failures clearly.

**Architecture:** Approach A from brainstorm — maximal purity. All classification, backoff computation, and state-transition logic goes in `core.py` as `@deal.pure` functions. The shell layer's only new job is extracting error text from the `.jsonl` log (I/O) and wiring the classifier result into the existing `compute_post_session_state` call. Design spec is `docs/dev-container.md` §12.

**Tech Stack:** Python 3.13, `deal` (purity contracts), `pytest`, frozen dataclasses, `re` for pattern matching.

---

## Context

The harness runs headless Claude CLI sessions in a container. When `claude -p` exits non-zero, the harness currently has no retry logic — it just continues to the next tick. Transient errors (503, overloaded, rate limits) should be retried with backoff; usage-limit errors should park the task until the reset window; true failures (auth, invalid model) should block the task loudly.

**Existing code touched by this plan:**
- `src/simpleharness/core.py` — frozen dataclasses (`State`, `SessionResult`, `TickPlan`), pure functions (`classify_cli_error`, `compute_backoff_delay`, `compute_post_session_state`, `pick_next_task`, `plan_tick`)
- `src/simpleharness/io.py` — `read_state`, `write_state`, `_STATE_FIELD_ORDER`
- `src/simpleharness/shell.py` — `tick_once` orchestration, new `_extract_error_text` helper
- `tests/test_core.py` — test factories `_state`, `_session`, new tests for all pure functions

## Approach

Follow the brainstorm's Approach A (maximal purity). The classifier and backoff are textbook pure functions. Extend `compute_post_session_state` with an optional `ClassifyResult` parameter to handle retry state transitions. Add `now: datetime` to `pick_next_task` and `plan_tick` for backoff-aware task selection. Shell changes are minimal: extract error text from `.jsonl` after session, call pure classifier, pass result through existing pipeline.

## Steps

### Task 1: Add retry fields to State dataclass and update io.py

**Files:**
- Modify: `src/simpleharness/core.py` (State dataclass, ~line 298)
- Modify: `src/simpleharness/io.py` (`_STATE_FIELD_ORDER`, `read_state`, `write_state`)
- Modify: `tests/test_core.py` (`_state` factory)

- [ ] **Step 1: Update _state test factory to accept retry fields**

In `tests/test_core.py`, add `retry_count` and `retry_after` parameters to the `_state` factory:

```python
def _state(
    *,
    slug: str = "001-test",
    workflow: str = "default",
    status: str = "active",
    phase: str = "kickoff",
    last_role: str | None = None,
    next_role: str | None = None,
    total_sessions: int = 0,
    session_cap: int = 20,
    consecutive_same_role: int = 0,
    no_progress_ticks: int = 0,
    blocked_reason: str | None = None,
    total_cost_usd: float = 0.0,
    retry_count: int = 0,
    retry_after: str | None = None,
) -> State:
    return State(
        task_slug=slug,
        workflow=workflow,
        worksite="/fake/worksite",
        toolbox="/fake/toolbox",
        status=status,
        phase=phase,
        last_role=last_role,
        next_role=next_role,
        total_sessions=total_sessions,
        session_cap=session_cap,
        consecutive_same_role=consecutive_same_role,
        no_progress_ticks=no_progress_ticks,
        blocked_reason=blocked_reason,
        created="2024-01-01T00:00:00Z",
        updated="2024-01-01T00:00:00Z",
        last_session_id=None,
        total_cost_usd=total_cost_usd,
        retry_count=retry_count,
        retry_after=retry_after,
    )
```

- [ ] **Step 2: Write failing test for State retry fields**

Add to `tests/test_core.py`:

```python
# ── State retry fields ────────────────────────────────────────────────────────


def test_state_retry_fields_default():
    s = _state()
    assert s.retry_count == 0
    assert s.retry_after is None


def test_state_retry_fields_set():
    s = _state(retry_count=3, retry_after="2026-04-09T16:00:00Z")
    assert s.retry_count == 3
    assert s.retry_after == "2026-04-09T16:00:00Z"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_core.py::test_state_retry_fields_default -v`
Expected: FAIL — `State.__init__() got an unexpected keyword argument 'retry_count'`

- [ ] **Step 4: Add retry fields to State dataclass**

In `src/simpleharness/core.py`, add two fields at the end of the `State` dataclass (after `consecutive_same_role`):

```python
    # retry / backoff (harness-managed)
    retry_count: int = 0
    retry_after: str | None = None  # ISO 8601 timestamp
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_core.py::test_state_retry_fields_default tests/test_core.py::test_state_retry_fields_set -v`
Expected: PASS

- [ ] **Step 6: Update io.py for round-trip serialization**

In `src/simpleharness/io.py`:

Add to `_STATE_FIELD_ORDER` (after `"consecutive_same_role"`):
```python
    "retry_count",
    "retry_after",
```

Add to `read_state` (after the `consecutive_same_role` line):
```python
        retry_count=int(meta.get("retry_count", 0) or 0),
        retry_after=meta.get("retry_after") or None,
```

Add to `write_state` data dict (after the `"consecutive_same_role"` entry):
```python
        "retry_count": state.retry_count,
        "retry_after": state.retry_after,
```

- [ ] **Step 7: Write round-trip serialization test**

Add to `tests/test_core.py` (or a new `tests/test_io.py` if one exists — check first):

```python
def test_state_retry_fields_roundtrip(tmp_path):
    """read_state and write_state preserve retry fields."""
    from simpleharness.io import read_state, write_state

    path = tmp_path / "STATE.md"
    original = _state(retry_count=2, retry_after="2026-04-09T16:00:00Z")
    write_state(path, original)
    restored = read_state(path)
    assert restored.retry_count == 2
    assert restored.retry_after == "2026-04-09T16:00:00Z"


def test_state_retry_fields_roundtrip_defaults(tmp_path):
    """Old STATE.md files without retry fields parse with defaults."""
    from simpleharness.io import read_state, write_state

    path = tmp_path / "STATE.md"
    original = _state()  # retry_count=0, retry_after=None
    write_state(path, original)
    restored = read_state(path)
    assert restored.retry_count == 0
    assert restored.retry_after is None
```

- [ ] **Step 8: Run all tests to verify no regressions**

Run: `uv run pytest tests/test_core.py -v`
Expected: all PASS including new tests

- [ ] **Step 9: Commit**

```bash
git add src/simpleharness/core.py src/simpleharness/io.py tests/test_core.py
git commit -m "feat(core): add retry_count and retry_after fields to State dataclass (task 003)"
```

---

### Task 2: Add ClassifyResult dataclass and classify_cli_error function

**Files:**
- Modify: `src/simpleharness/core.py` (new types + function)
- Modify: `tests/test_core.py` (new tests)

- [ ] **Step 1: Write failing tests for classify_cli_error**

Add to `tests/test_core.py` imports:
```python
from simpleharness.core import (
    # ... existing imports ...
    ClassifyResult,
    classify_cli_error,
)
```

Add tests:
```python
# ── classify_cli_error ────────────────────────────────────────────────────────


def test_classify_usage_limit_with_reset():
    r = classify_cli_error(1, "Usage limit reached. Reset at 2026-04-09T17:00:00Z.")
    assert r.outcome == "usage_limit"
    assert r.retry_after_iso == "2026-04-09T17:00:00Z"


def test_classify_transient_overloaded():
    r = classify_cli_error(1, "API is overloaded, please retry later")
    assert r.outcome == "transient"


def test_classify_transient_529():
    r = classify_cli_error(1, "HTTP 529 error from upstream")
    assert r.outcome == "transient"


def test_classify_transient_503():
    r = classify_cli_error(1, "503 Service Unavailable")
    assert r.outcome == "transient"


def test_classify_transient_rate_limit():
    r = classify_cli_error(1, "Rate limit exceeded")
    assert r.outcome == "transient"


def test_classify_transient_econnreset():
    r = classify_cli_error(1, "ECONNRESET: connection reset by peer")
    assert r.outcome == "transient"


def test_classify_transient_etimedout():
    r = classify_cli_error(1, "ETIMEDOUT: connection timed out")
    assert r.outcome == "transient"


def test_classify_transient_dns():
    r = classify_cli_error(1, "DNS resolution failed for api.anthropic.com")
    assert r.outcome == "transient"


def test_classify_transient_timeout():
    r = classify_cli_error(1, "Request timeout after 30s")
    assert r.outcome == "transient"


def test_classify_fatal_401():
    r = classify_cli_error(1, "401 Unauthorized")
    assert r.outcome == "fatal"
    assert "auth_expired" in r.reason


def test_classify_fatal_invalid_api_key():
    r = classify_cli_error(1, "Invalid API key provided")
    assert r.outcome == "fatal"
    assert "auth_expired" in r.reason


def test_classify_fatal_not_authenticated():
    r = classify_cli_error(1, "Not authenticated — please run claude login")
    assert r.outcome == "fatal"
    assert "auth_expired" in r.reason


def test_classify_fatal_token_expired():
    r = classify_cli_error(1, "Token expired, re-authenticate")
    assert r.outcome == "fatal"
    assert "auth_expired" in r.reason


def test_classify_fatal_unknown():
    r = classify_cli_error(1, "Something completely unexpected happened")
    assert r.outcome == "fatal"
    assert "unexpected" in r.reason.lower()


def test_classify_fatal_empty_error_text():
    r = classify_cli_error(42, "")
    assert r.outcome == "fatal"
    assert "42" in r.reason


def test_classify_usage_limit_priority_over_transient():
    """Usage limit with reset time should match usage_limit, not transient."""
    r = classify_cli_error(1, "Rate limit: usage limit reached. Reset at 2026-04-09T18:00:00Z")
    assert r.outcome == "usage_limit"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_core.py::test_classify_usage_limit_with_reset -v`
Expected: FAIL — `ImportError: cannot import name 'ClassifyResult'`

- [ ] **Step 3: Implement ClassifyResult and classify_cli_error**

In `src/simpleharness/core.py`, add the type alias after existing type aliases, the dataclass after `SessionResult`, and the function after `compute_post_session_state`:

```python
ErrorOutcome = Literal["usage_limit", "transient", "fatal"]


@dataclass(frozen=True)
class ClassifyResult:
    """Result of classifying a CLI error."""

    outcome: ErrorOutcome
    reason: str
    retry_after_iso: str | None = None  # ISO 8601 reset timestamp (usage_limit only)
```

```python
_USAGE_LIMIT_RE = re.compile(
    r"usage.limit.*reset.*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|\+\d{2}:\d{2})?)",
    re.IGNORECASE,
)

_TRANSIENT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"overloaded",
        r"\b529\b",
        r"\b503\b",
        r"rate.?limit",
        r"ECONNRESET",
        r"ETIMEDOUT",
        r"\bDNS\b",
        r"timeout",
    )
)

_AUTH_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b401\b",
        r"invalid api key",
        r"not authenticated",
        r"token expired",
    )
)


@deal.pure
def classify_cli_error(exit_code: int | None, error_text: str) -> ClassifyResult:
    """Classify a CLI error into usage_limit, transient, or fatal.

    Checks usage-limit first (has reset time), then transient patterns,
    then auth/fatal patterns. Unknown errors default to fatal.
    """
    # usage limit with reset timestamp (checked first — takes priority)
    m = _USAGE_LIMIT_RE.search(error_text)
    if m:
        return ClassifyResult("usage_limit", "usage limit hit", retry_after_iso=m.group(1))

    # transient patterns
    for pat in _TRANSIENT_PATTERNS:
        if pat.search(error_text):
            return ClassifyResult("transient", f"matched transient pattern: {pat.pattern}")

    # auth / fatal patterns
    for pat in _AUTH_PATTERNS:
        if pat.search(error_text):
            return ClassifyResult(
                "fatal", "auth_expired — run claude login in container"
            )

    # unknown → fatal (loud stop, not silent retry)
    last_line = error_text.strip().splitlines()[-1] if error_text.strip() else ""
    reason = last_line if last_line else f"exit code {exit_code}"
    return ClassifyResult("fatal", reason)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_core.py -k "classify" -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/simpleharness/core.py tests/test_core.py
git commit -m "feat(core): add classify_cli_error pure function with pattern table (task 003)"
```

---

### Task 3: Add compute_backoff_delay function

**Files:**
- Modify: `src/simpleharness/core.py` (new constant + function)
- Modify: `tests/test_core.py` (new tests)

- [ ] **Step 1: Write failing tests**

Add to `tests/test_core.py` imports:
```python
from simpleharness.core import (
    # ... existing ...
    DEFAULT_BACKOFF_SCHEDULE,
    compute_backoff_delay,
)
```

Add tests:
```python
# ── compute_backoff_delay ─────────────────────────────────────────────────────


def test_backoff_delay_first_retry():
    assert compute_backoff_delay(0) == 30


def test_backoff_delay_second_retry():
    assert compute_backoff_delay(1) == 60


def test_backoff_delay_third_retry():
    assert compute_backoff_delay(2) == 120


def test_backoff_delay_fourth_retry():
    assert compute_backoff_delay(3) == 240


def test_backoff_delay_fifth_retry():
    assert compute_backoff_delay(4) == 300


def test_backoff_delay_exhausted():
    assert compute_backoff_delay(5) is None


def test_backoff_delay_way_past_exhausted():
    assert compute_backoff_delay(99) is None


def test_backoff_delay_custom_schedule():
    assert compute_backoff_delay(0, schedule=(10, 20)) == 10
    assert compute_backoff_delay(1, schedule=(10, 20)) == 20
    assert compute_backoff_delay(2, schedule=(10, 20)) is None


def test_default_backoff_schedule_values():
    assert DEFAULT_BACKOFF_SCHEDULE == (30, 60, 120, 240, 300)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_core.py::test_backoff_delay_first_retry -v`
Expected: FAIL — `ImportError: cannot import name 'compute_backoff_delay'`

- [ ] **Step 3: Implement compute_backoff_delay**

In `src/simpleharness/core.py`, add the constant and function:

```python
DEFAULT_BACKOFF_SCHEDULE: tuple[int, ...] = (30, 60, 120, 240, 300)


@deal.pure
def compute_backoff_delay(
    retry_count: int,
    schedule: tuple[int, ...] = DEFAULT_BACKOFF_SCHEDULE,
) -> int | None:
    """Return the backoff delay in seconds for the given retry count, or None if exhausted."""
    if retry_count >= len(schedule):
        return None
    return schedule[retry_count]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_core.py -k "backoff_delay" -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/simpleharness/core.py tests/test_core.py
git commit -m "feat(core): add compute_backoff_delay pure function (task 003)"
```

---

### Task 4: Extend compute_post_session_state for retry logic

**Files:**
- Modify: `src/simpleharness/core.py` (extend `compute_post_session_state`)
- Modify: `tests/test_core.py` (new tests)

Note: `timedelta` must be added to the `from datetime import ...` line in `core.py`.

- [ ] **Step 1: Write failing tests for retry state transitions**

Add to `tests/test_core.py`:

```python
from datetime import timedelta

_NOW = datetime(2026, 4, 9, 15, 0, 0, tzinfo=UTC)


# ── compute_post_session_state retry logic ────────────────────────────────────


def test_post_session_clears_retry_on_success():
    state = _state(retry_count=3, retry_after="2026-04-09T14:00:00Z")
    session = _session(completed=True, exit_code=0)
    new = compute_post_session_state(
        state, "dev", session, None, 0, "pre", "post", _config(), _NOW,
        classify_result=None,
    )
    assert new.retry_count == 0
    assert new.retry_after is None


def test_post_session_transient_bumps_retry():
    state = _state(retry_count=0)
    session = _session(completed=False, exit_code=1)
    cr = ClassifyResult("transient", "matched transient pattern: overloaded")
    new = compute_post_session_state(
        state, "dev", session, None, 0, "pre", "post", _config(), _NOW,
        classify_result=cr,
    )
    assert new.retry_count == 1
    assert new.retry_after is not None
    assert new.status == "active"


def test_post_session_transient_sets_correct_backoff():
    state = _state(retry_count=2)  # 3rd retry → 120s delay
    session = _session(completed=False, exit_code=1)
    cr = ClassifyResult("transient", "503")
    new = compute_post_session_state(
        state, "dev", session, None, 0, "pre", "post", _config(), _NOW,
        classify_result=cr,
    )
    assert new.retry_count == 3
    expected_after = (_NOW + timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert new.retry_after == expected_after


def test_post_session_transient_exhausted_blocks():
    state = _state(retry_count=4)  # 5th retry → exhausted
    session = _session(completed=False, exit_code=1)
    cr = ClassifyResult("transient", "overloaded")
    new = compute_post_session_state(
        state, "dev", session, None, 0, "pre", "post", _config(), _NOW,
        classify_result=cr,
    )
    assert new.status == "blocked"
    assert "retries exhausted" in (new.blocked_reason or "")
    assert new.retry_count == 5
    assert new.retry_after is None


def test_post_session_usage_limit_parks_task():
    state = _state(retry_count=0)
    session = _session(completed=False, exit_code=1)
    cr = ClassifyResult("usage_limit", "usage limit hit", retry_after_iso="2026-04-09T17:00:00Z")
    new = compute_post_session_state(
        state, "dev", session, None, 0, "pre", "post", _config(), _NOW,
        classify_result=cr,
    )
    assert new.retry_after == "2026-04-09T17:00:00Z"
    assert new.retry_count == 0  # not bumped for usage limits
    assert new.status == "active"


def test_post_session_fatal_blocks_task():
    state = _state(retry_count=2)
    session = _session(completed=False, exit_code=1)
    cr = ClassifyResult("fatal", "auth_expired — run claude login in container")
    new = compute_post_session_state(
        state, "dev", session, None, 0, "pre", "post", _config(), _NOW,
        classify_result=cr,
    )
    assert new.status == "blocked"
    assert "auth_expired" in (new.blocked_reason or "")
    assert new.retry_count == 0
    assert new.retry_after is None


def test_post_session_no_classify_result_no_change():
    """When classify_result is None, retry fields are unchanged (backward compat)."""
    state = _state(retry_count=0)
    session = _session(completed=False, exit_code=1)
    new = compute_post_session_state(
        state, "dev", session, None, 0, "pre", "post", _config(), _NOW,
        classify_result=None,
    )
    assert new.retry_count == 0
    assert new.retry_after is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_core.py::test_post_session_clears_retry_on_success -v`
Expected: FAIL — `TypeError: compute_post_session_state() got an unexpected keyword argument 'classify_result'`

- [ ] **Step 3: Implement retry logic in compute_post_session_state**

In `src/simpleharness/core.py`, update the import line:
```python
from datetime import datetime, timedelta
```

Update `compute_post_session_state` signature to add `classify_result`:
```python
@deal.pure
def compute_post_session_state(
    state: State,
    role_name: str,
    session: SessionResult,
    prev_last_role: str | None,
    prev_consecutive_same_role: int,
    pre_hash: str,
    post_hash: str,
    config: Config,
    now: datetime,
    *,
    classify_result: ClassifyResult | None = None,
) -> State:
```

After the existing loop-guard block (the `elif` that checks `consecutive_same_role`), add:

```python
    # ── retry / backoff logic ────────────────────────────────────────────────
    if session.completed:
        # success → clear retry state
        new_state = replace(new_state, retry_count=0, retry_after=None)
    elif classify_result is not None and not session.interrupted:
        match classify_result.outcome:
            case "fatal":
                new_state = replace(
                    new_state,
                    status="blocked",
                    blocked_reason=classify_result.reason,
                    retry_count=0,
                    retry_after=None,
                )
            case "usage_limit":
                new_state = replace(
                    new_state,
                    retry_after=classify_result.retry_after_iso,
                )
            case "transient":
                new_retry = state.retry_count + 1
                delay = compute_backoff_delay(new_retry - 1)
                if delay is None:
                    new_state = replace(
                        new_state,
                        status="blocked",
                        blocked_reason=f"transient retries exhausted ({new_retry})",
                        retry_count=new_retry,
                        retry_after=None,
                    )
                else:
                    retry_at = (now + timedelta(seconds=delay)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                    new_state = replace(
                        new_state,
                        retry_count=new_retry,
                        retry_after=retry_at,
                    )

    return new_state
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_core.py -k "post_session" -v`
Expected: all PASS (both new retry tests and existing post_session tests)

- [ ] **Step 5: Commit**

```bash
git add src/simpleharness/core.py tests/test_core.py
git commit -m "feat(core): extend compute_post_session_state with retry/backoff logic (task 003)"
```

---

### Task 5: Add backoff-aware task selection to pick_next_task and plan_tick

**Files:**
- Modify: `src/simpleharness/core.py` (`TickPlan`, `pick_next_task`, `plan_tick`)
- Modify: `src/simpleharness/shell.py` (update `plan_tick` call to pass `now`)
- Modify: `tests/test_core.py` (new tests)

- [ ] **Step 1: Write failing tests for backoff filtering**

Add to `tests/test_core.py`:

```python
# ── pick_next_task backoff filtering ──────────────────────────────────────────


def test_pick_next_task_skips_backoff():
    """Task in backoff (retry_after in the future) is skipped."""
    t = _task(state=_state(retry_after="2026-04-09T16:00:00Z"))
    now = datetime(2026, 4, 9, 15, 0, 0, tzinfo=UTC)  # before retry_after
    result = pick_next_task((t,), frozenset(), now)
    assert result is None


def test_pick_next_task_picks_past_backoff():
    """Task whose retry_after is in the past is eligible."""
    t = _task(state=_state(retry_after="2026-04-09T14:00:00Z"))
    now = datetime(2026, 4, 9, 15, 0, 0, tzinfo=UTC)  # after retry_after
    result = pick_next_task((t,), frozenset(), now)
    assert result is not None
    assert result.slug == "001-test"


def test_pick_next_task_correction_overrides_backoff():
    """Task with CORRECTION.md bypasses backoff filter."""
    t = _task(slug="001-test", state=_state(slug="001-test", retry_after="2026-04-09T16:00:00Z"))
    now = datetime(2026, 4, 9, 15, 0, 0, tzinfo=UTC)
    result = pick_next_task((t,), frozenset({"001-test"}), now)
    assert result is not None
    assert result.slug == "001-test"


def test_pick_next_task_no_retry_after_is_eligible():
    """Task with retry_after=None is always eligible."""
    t = _task(state=_state(retry_after=None))
    now = datetime(2026, 4, 9, 15, 0, 0, tzinfo=UTC)
    result = pick_next_task((t,), frozenset(), now)
    assert result is not None


# ── plan_tick all_backoff ─────────────────────────────────────────────────────


def test_plan_tick_all_backoff():
    t = _task(state=_state(retry_after="2026-04-09T16:00:00Z"))
    now = datetime(2026, 4, 9, 15, 0, 0, tzinfo=UTC)
    plan = plan_tick((t,), {"default": _workflow()}, frozenset(), _config(), now)
    assert plan.kind == "all_backoff"


def test_plan_tick_run_with_now():
    """plan_tick still produces 'run' when task is not in backoff."""
    t = _task(state=_state(retry_after=None))
    now = datetime(2026, 4, 9, 15, 0, 0, tzinfo=UTC)
    plan = plan_tick((t,), {"default": _workflow()}, frozenset(), _config(), now)
    assert plan.kind == "run"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_core.py::test_pick_next_task_skips_backoff -v`
Expected: FAIL — `TypeError: pick_next_task() got an unexpected keyword argument` (or takes 2 positional arguments but 3 were given)

- [ ] **Step 3: Update TickPlan to support "all_backoff" kind**

In `src/simpleharness/core.py`, update the `TickPlan` `kind` field:

```python
    kind: Literal["no_tasks", "no_active", "waiting_on_deps", "all_backoff", "block", "run"]
```

- [ ] **Step 4: Add `now` parameter to pick_next_task with backoff filtering**

Update `pick_next_task` signature and add filter:

```python
@deal.pure
def pick_next_task(
    tasks: Sequence[Task], corrections: frozenset[str], now: datetime,
) -> Task | None:
    """Priority: CORRECTION.md exists > active + deps met > lowest slug.

    Tasks in backoff (retry_after > now) are skipped unless they have a
    CORRECTION.md override.
    """
    all_states: dict[str, str] = {t.slug: t.state.status for t in tasks}
    candidates = [
        t
        for t in tasks
        if t.state.status == "active" and (t.spec is None or deps_satisfied(t.spec, all_states))
    ]
    # filter out tasks in backoff (corrections bypass backoff)
    candidates = [
        t
        for t in candidates
        if t.slug in corrections
        or t.state.retry_after is None
        or datetime.fromisoformat(t.state.retry_after) <= now
    ]
    if not candidates:
        return None
    # tasks with CORRECTION.md take priority
    with_correction = [t for t in candidates if t.slug in corrections]
    if with_correction:
        return sorted(with_correction, key=lambda t: t.slug)[0]
    return sorted(candidates, key=lambda t: t.slug)[0]
```

- [ ] **Step 5: Add `now` parameter to plan_tick and handle all_backoff**

Update `plan_tick` signature:

```python
@deal.pure
def plan_tick(
    tasks: tuple[Task, ...],
    workflows_by_name: Mapping[str, Workflow | None],
    corrections: frozenset[str],
    config: Config,
    now: datetime,
) -> TickPlan:
```

Update the `pick_next_task` call inside `plan_tick`:

```python
    task = pick_next_task(tasks, corrections, now)
```

Update the `if task is None` block to detect backoff:

```python
    if task is None:
        has_active_in_backoff = any(
            t.state.status == "active"
            and t.state.retry_after is not None
            and datetime.fromisoformat(t.state.retry_after) > now
            for t in tasks
        )
        if has_active_in_backoff:
            return TickPlan(kind="all_backoff")
        has_active_with_unmet_deps = any(
            t.state.status == "active"
            and t.spec is not None
            and not deps_satisfied(t.spec, {tt.slug: tt.state.status for tt in tasks})
            for t in tasks
        )
        if has_active_with_unmet_deps:
            return TickPlan(kind="waiting_on_deps")
        return TickPlan(kind="no_active")
```

- [ ] **Step 6: Update ALL existing plan_tick and pick_next_task test calls**

Every existing call to `plan_tick(tasks, wf, corrections, config)` must add `now` as the 5th argument. Use a sentinel far in the past so no task is in backoff:

```python
_FAR_PAST = datetime(2000, 1, 1, tzinfo=UTC)
```

Replace all existing `plan_tick(...)` calls with `plan_tick(..., _FAR_PAST)`.
Replace all existing `pick_next_task(tasks, corrections)` calls with `pick_next_task(tasks, corrections, _FAR_PAST)`.

This is a mechanical search-and-replace. Count occurrences before and after to confirm nothing was missed.

- [ ] **Step 7: Update shell.py plan_tick call to pass `now`**

In `src/simpleharness/shell.py`, `tick_once` function, change:
```python
    plan = plan_tick(tasks, workflows_by_name, corrections, config)
```
to:
```python
    plan = plan_tick(tasks, workflows_by_name, corrections, config, datetime.now(UTC))
```

This must happen in the same commit as the signature change to avoid a broken intermediate state.

- [ ] **Step 8: Handle "all_backoff" case in tick_once match**

Add to the `match plan.kind:` block in `tick_once`, after `"waiting_on_deps"`:

```python
        case "all_backoff":
            say("all active tasks in backoff, waiting for retry window", style="dim")
            return False
```

- [ ] **Step 9: Run full test suite**

Run: `uv run pytest tests/test_core.py -v`
Expected: all PASS

- [ ] **Step 10: Commit**

```bash
git add src/simpleharness/core.py src/simpleharness/shell.py tests/test_core.py
git commit -m "feat(core): add backoff-aware task selection to pick_next_task and plan_tick (task 003)"
```

---

### Task 6: Shell integration — extract errors and wire classifier

**Files:**
- Modify: `src/simpleharness/shell.py` (`tick_once`, new `_extract_error_text`)

- [ ] **Step 1: Add imports to shell.py**

In `src/simpleharness/shell.py`, add to the `from simpleharness.core import` block:

```python
    classify_cli_error,
```

Add `import json` at the top-level imports (it is NOT currently imported in shell.py — verify with grep first).

- [ ] **Step 2: Add _extract_error_text helper function**

Add before `tick_once`:

```python
def _extract_error_text(jsonl_path: Path) -> str:
    """Extract error messages from a session's .jsonl log (I/O)."""
    errors: list[str] = []
    try:
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "error":
                error_obj = event.get("error", {})
                msg = (
                    error_obj.get("message", "")
                    if isinstance(error_obj, dict)
                    else str(error_obj)
                )
                if msg:
                    errors.append(msg)
    except OSError:
        pass
    return "\n".join(errors)
```

Note: The `plan_tick` call update and `all_backoff` case were already added in Task 5 Steps 7-8.

- [ ] **Step 3: Wire classifier into tick_once after run_session**

In the `case "run":` block, after the `session = run_session(...)` call and the `post_hash` / `current_state` reads, add error classification before `compute_post_session_state`:

```python
            # ── classify CLI errors for retry/backoff ────────────────────
            classify_result = None
            if not session.completed and not session.interrupted and session.exit_code != 0:
                log_root = worksite_sh_dir(Path(task.state.worksite)) / "logs" / task.slug
                jsonl_log = log_root / f"{task.state.total_sessions:02d}-{role_name}.jsonl"
                error_text = _extract_error_text(jsonl_log)
                classify_result = classify_cli_error(session.exit_code, error_text)
                say(
                    f"task {task.slug}: CLI error classified as {classify_result.outcome}"
                    f" — {classify_result.reason}",
                    style="yellow",
                )
```

- [ ] **Step 4: Update compute_post_session_state call to pass classify_result**

In the same `case "run":` block, update the existing `compute_post_session_state` call:

```python
            new_state = compute_post_session_state(
                current_state,
                role.name,
                session,
                prev_last_role=prev_last_role,
                prev_consecutive_same_role=prev_consecutive_same_role,
                pre_hash=pre_hash,
                post_hash=post_hash,
                config=config,
                now=datetime.now(UTC),
                classify_result=classify_result,
            )
```

- [ ] **Step 5: Run linter and type checker**

Run: `uv run ruff check src/simpleharness/shell.py`
Run: `uv run ty check`
Expected: both exit 0

- [ ] **Step 6: Commit**

```bash
git add src/simpleharness/shell.py
git commit -m "feat(shell): wire CLI error classifier into tick_once with retry/backoff (task 003)"
```

---

### Task 7: Full verification

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest -v`
Expected: all tests PASS, no regressions

- [ ] **Step 2: Run linter**

Run: `uv run ruff check .`
Expected: exit 0

- [ ] **Step 3: Run type checker**

Run: `uv run ty check`
Expected: exit 0

- [ ] **Step 4: Verify FP purity gate**

Run: `uv run python scripts/check_fp_purity.py`
Expected: all new functions in `core.py` are detected as `@deal.pure` decorated

- [ ] **Step 5: Final commit if any fixups needed**

If any of the above checks required fixes, commit them:
```bash
git add -u
git commit -m "fix: address lint/type/purity issues from verification (task 003)"
```

---

## Files to touch

- `src/simpleharness/core.py` — State fields, ClassifyResult, classify_cli_error, compute_backoff_delay, compute_post_session_state extension, pick_next_task now param, plan_tick now param, TickPlan kind extension
- `src/simpleharness/io.py` — _STATE_FIELD_ORDER, read_state, write_state (retry fields)
- `src/simpleharness/shell.py` — _extract_error_text, tick_once (classifier wiring, all_backoff handling, plan_tick now param)
- `tests/test_core.py` — _state factory, new tests for all pure functions, existing call-site updates for now param

## Risks

1. **`datetime.fromisoformat` and compiled regex in `@deal.pure` functions** — `deal.pure` may flag `datetime.fromisoformat` or access to module-level `re.compile` objects as impure. Mitigation: both are deterministic with no I/O. The compiled regex patterns (`_USAGE_LIMIT_RE`, `_TRANSIENT_PATTERNS`, `_AUTH_PATTERNS`) are module-level constants, not mutated. If deal-lint flags them, suppress with a targeted `# noqa` or use `deal.has()` with an empty set instead of `deal.pure`.

2. **Existing test breakage from signature changes** — `pick_next_task` and `plan_tick` gain a `now` parameter. All existing call sites (in tests and shell.py) must be updated. Mitigation: Task 5 Step 6 is a mechanical search-and-replace with a before/after count check.

3. **Pattern table drift** — The regex patterns may not match all real Claude CLI error formats. Mitigation: unknown errors default to `fatal` (safe), and patterns are pre-authorized to adjust. Add a test for each pattern and log the raw error text for future refinement.

4. **`.jsonl` log format changes** — If `stream_and_log` changes its event format, `_extract_error_text` may miss errors. Mitigation: the function scans for `type: "error"` events, a stable part of the stream-json spec.

5. **Timezone handling** — `retry_after` uses ISO 8601 strings. `datetime.fromisoformat` handles `Z` suffix only in Python 3.11+. Mitigation: we're on Python 3.13, and the harness writes timestamps with `Z` suffix consistently.

## Verification

After all tasks complete:

```bash
uv run pytest -v                          # all tests pass, no regressions
uv run ruff check .                       # clean lint
uv run ty check                           # clean type check
uv run python scripts/check_fp_purity.py  # all core.py functions have @deal.pure
```

Manual smoke test (optional): create a mock STATE.md with `retry_count: 2` and `retry_after: <future ISO>`, verify the watch loop skips it and logs "all active tasks in backoff".
