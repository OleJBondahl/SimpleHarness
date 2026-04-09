# 01 — Brainstorm (brainstormer)

## Context

**What I read:**
- `TASK.md` — 5 deliverables: Dockerfile, compose.yml, launch.sh, entrypoint.sh, .dockerignore
- `00-kickoff.md` — project-leader assessment, key risks identified
- `docs/dev-container.md` — full spec (§1–§14), contains complete file contents for all 5 artifacts
- Current codebase state: pyproject.toml, shell.py, core.py, session.py, config.yaml, .gitattributes

**Worksite state:**
- No Docker infrastructure exists yet (no Dockerfile, no compose files, no .devcontainer/)
- `scripts/` directory already exists at repo root
- `.gitattributes` has `* text=auto eol=lf` — already covers all file types including *.sh, Dockerfile, compose.yml
- Python 3.13, uv, deal/pyyaml/rich dependencies
- FC/IS architecture fully in place

## Clarifying Questions

1. **Do all spec line references match the current codebase?** `[self-answerable]`
   Yes. Verified all 6 references:
   - `shell.py:699-709` (sandbox check) — exact match
   - `core.py:177` (toolbox_root) — exact match
   - `shell.py:88-93` (worksite_root precedence) — exact match
   - `core.py:714-761` (build_claude_cmd) — exact match
   - `session.py:54-73` (spawn_claude) — minor drift, function extends to line 75 not 73. No impact on artifacts.
   - `config.yaml:35-48` (permissions block) — exact match

2. **Does `simpleharness init --worksite /worksite` exist?** `[self-answerable]`
   Yes. `shell.py:844` registers the `init` subcommand, wired to `cmd_init()` at lines 378–390. The `--worksite` flag is a common argument inherited by all subcommands (lines 836–839).

3. **Does `uv tool install -e /opt/simpleharness` work for editable installs?** `[self-answerable]`
   Yes. `pyproject.toml` has `[project.scripts]` with `simpleharness = "simpleharness.shell:main"`, making it installable as a uv tool. The `-e` flag enables editable mode so the bind mount at `/opt/simpleharness` is the live source.

4. **Is `.gitattributes` coverage sufficient?** `[self-answerable]`
   Yes. The wildcard pattern `* text=auto eol=lf` normalizes all text files to LF. No per-extension rules needed for `*.sh`, `Dockerfile`, or `compose.yml`.

5. **Is `sha1sum` available in Git Bash on Windows?** `[self-answerable]`
   Yes. Git Bash ships with GNU coreutils including sha1sum.

6. **Does the Claude Code installer work from /tmp in Docker?** `[self-answerable]`
   The spec explicitly notes the installer must run from `/tmp` — at `/` it scans the entire filesystem and hangs. The Dockerfile's `WORKDIR /tmp` before the install step handles this. This is a known gotcha documented in the spec.

7. **Should §12 (CLI error handling) be implemented?** `[self-answerable]`
   No. TASK.md boundaries say "Do not implement error handling / retry logic — that is task 003." §12 is marked "proposed design" in the spec.

**No `[needs user]` questions remain.** The spec is comprehensive and validated.

## Possible Approaches

### A. Verbatim transcription from spec
Copy the code blocks from §4.1–4.5 exactly as written. Run shellcheck on both scripts. Done.

**Trade-off:** Fastest path. Risk is that any subtle issues in the spec (e.g., shellcheck warnings, Docker build failures) are discovered only at build time. But the spec was carefully written and refined in task 001, so this risk is low.

### B. Transcription with proactive shellcheck/lint pass
Same as A, but the developer runs shellcheck on both scripts *before* committing, and fixes any warnings inline. Also does a dry `docker compose config` to validate compose.yml syntax.

**Trade-off:** Slightly more work upfront, but catches issues before the commit. Given that shellcheck compliance is a success criterion, this is essentially required anyway.

### C. Incremental build-and-test
Create each file one at a time, testing each: build the image after Dockerfile, validate compose after compose.yml, run shellcheck after each script. Fix issues as they surface.

**Trade-off:** Most thorough but slowest. Overkill given that the spec contains complete, tested file contents. Better suited if the spec were a rough sketch.

## Recommended Direction

**Approach B: Transcription with proactive validation.** The spec is implementation-ready — all line references verified, all CLI commands confirmed to exist, all assumptions validated. The developer should copy the 5 code blocks from §4.1–4.5, make minimal adaptations only where the codebase requires it (none identified so far), and run shellcheck + `docker compose config` as validation before committing. The only known deviation is the minor `session.py` line drift (73→75), which doesn't affect any artifact.

## Deviations from Spec

None identified. The spec aligns perfectly with the current codebase state. The developer should proceed with confidence.

## Risks to Watch

1. **shellcheck compliance** — Both launch.sh and entrypoint.sh must pass with zero errors. The spec code looks clean but hasn't been shellchecked yet.
2. **Named-volume shadowing** — The compose.yml comment documents the trap, but the developer should verify the persistent home volume behavior works as described.
3. **Windows path handling** — `cygpath` and `MSYS_NO_PATHCONV` in launch.sh need testing on Git Bash. Can only be fully validated by running on the actual Windows host.
4. **Claude Code installer stability** — The `curl | bash` installer from `claude.ai/install.sh` is an external dependency. If the installer URL or behavior changes, the Dockerfile build breaks. Low risk for v0.1.

## Subagents Dispatched

| Model | Task | Result |
|-------|------|--------|
| Haiku | Read TASK.md, STATE.md, 00-kickoff.md, docs/dev-container.md | Full contents returned |
| Haiku | Read codebase state (pyproject.toml, shell.py, core.py, .gitattributes, directory listings) | Structure mapped, no Docker files exist yet |
| Haiku | Verify all 6 spec line references against actual source | All match (one minor drift: session.py 54-75 not 54-73) |
| Haiku | Check init subcommand, uv tool install, --worksite flag | All confirmed to exist |
