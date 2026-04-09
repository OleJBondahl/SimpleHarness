# Dev Container Setup & Usage

The SimpleHarness dev container runs the harness with full bypass permissions (`--permission-mode bypassPermissions`) safely isolated inside Docker, so agents can operate autonomously without touching your host system. It is aimed at solo developers on Windows 11 + Git Bash + Docker Desktop who want to point SimpleHarness at a local Python or JS/TS repository and let it run unattended.

---

## Prerequisites

- **Docker Desktop** (Windows) with the WSL 2 backend enabled
- **Git Bash** (included with [Git for Windows](https://git-scm.com/downloads/win))
- A **Claude Code account** with API access
- The **SimpleHarness repo** cloned locally:

```bash
git clone https://github.com/OleJBondahl/SimpleHarness.git
```

---

## First-run setup

### 1. Clone the repo (if not done)

```bash
git clone https://github.com/OleJBondahl/SimpleHarness.git
```

### 2. Build the image

```bash
cd ~/SimpleHarness
docker compose build
```

### 3. Log in to Claude Code (one-time)

Credentials are stored in a named Docker volume and persist across runs, so you only need to do this once.

```bash
docker compose run --rm --entrypoint claude simpleharness login
```

Follow the prompts in your terminal to authenticate.

### 4. Launch against a worksite

```bash
scripts/launch.sh --worksite /path/to/your/repo
```

The worksite must be a git repository. It cannot overlap the SimpleHarness toolbox directory — the launch script enforces this automatically.

---

## Steady-state usage

**Daily workflow:** once the image is built and credentials are stored, just run:

```bash
scripts/launch.sh --worksite /path/to/your/repo
```

**What the launch script does:**

1. Validates the worksite (must be a git repo, must not overlap the toolbox).
2. Builds the image from cache (fast if nothing has changed).
3. Checks that credentials are present in the volume.
4. Runs `docker compose run` with the correct mounts and environment.

**What happens inside the container:**

The `entrypoint.sh` script:
1. Installs `simpleharness` from the mounted toolbox.
2. Initialises the worksite (`simpleharness init`).
3. Opts into dangerous mode.
4. Runs `simpleharness watch` — the main tick loop.

**Ctrl+C flow:** Ctrl+C sends `SIGINT` through `tini` to the harness process. The harness catches the signal, writes a correction prompt, and exits cleanly.

**Multiple worksites in parallel:** each invocation of `launch.sh` derives a compose project name from a hash of the worksite path, so you can run multiple terminals against different repos simultaneously without conflicts.

---

## Python vs JS/TS repos

**Python repos:** the container ships Python 3.13 and `uv`. Agents can use `uv`, `pip`, `pytest`, `ruff`, and other standard Python tooling without any extra setup.

**JS/TS repos:** Node 20 LTS is pre-installed. Agents can run `npm`, `npx`, and `node` out of the box.

**No special configuration is needed for either.** The container detects what it needs from the worksite.

**Note on `.venv` and `node_modules`:** the worksite is bind-mounted from Windows, but Windows-native binaries (compiled extensions, platform-specific packages) cannot run inside a Linux container. To handle this, the container mounts anonymous Docker volumes over `.venv` and `node_modules`, masking the host directories and letting the container create fresh Linux-native ones. Your host copies are left untouched.

---

## The `--allow-toolbox-edits` flag

By default, the SimpleHarness repo is mounted **read-only** at `/opt/simpleharness` inside the container. This prevents agents from accidentally modifying the harness itself.

Pass `--allow-toolbox-edits` to mount it read-write:

```bash
scripts/launch.sh --worksite /path/to/your/repo --allow-toolbox-edits
```

This is only needed when the project-leader role needs to improve its own role or workflow definitions. Do not use it for normal autonomous work.

---

## Error handling behavior

### Usage limit (API rate limit)

- The harness detects the rate limit and reads the reset timestamp from the error response.
- The task is parked — `retry_after` appears in `STATE.md` — and resumes automatically once the reset time passes.
- This does **not** count as a retry.
- **User action:** none. Just wait.

### Transient errors (network timeouts, 503s, overloaded)

- The harness retries automatically with exponential backoff: 30 s → 60 s → 120 s → 240 s → 300 s.
- `retry_count` in `STATE.md` tracks how many retries have occurred.
- After 5 consecutive failures the task is marked `status: blocked`.
- **User action:** check your network or Docker connectivity, then manually set `status: active` and `retry_count: 0` in `STATE.md`.

### Fatal errors (auth failures, invalid model)

- The task is immediately marked `status: blocked` with a human-readable `blocked_reason`.
- No retries are attempted.
- **User action:** fix the underlying issue (re-login, verify your API key), then set `status: active` in `STATE.md`.

**On success:** `retry_count` and `retry_after` are cleared automatically.

---

## Troubleshooting

### CRLF line endings

The entrypoint runs `git config --global core.autocrlf input` inside the container. If you see spurious diffs on bind-mounted files, ensure your host repo uses LF line endings:

```bash
git config core.autocrlf input
git checkout -- .
```

### Windows path issues

`launch.sh` uses `cygpath -m` to normalise paths for Docker Desktop, and sets `MSYS_NO_PATHCONV=1` automatically to prevent path mangling. If you still get path errors, make sure you are running from **Git Bash** and not from PowerShell or cmd.

### Credential expiry

Credentials live in the `simpleharness-home` Docker volume. If your login expires:

```bash
cd ~/SimpleHarness
docker compose run --rm --entrypoint claude simpleharness login
```

### Stale image

After pulling new changes to SimpleHarness, rebuild the image:

```bash
cd ~/SimpleHarness
docker compose build
```

To fully reset — including credentials:

```bash
docker compose down -v
docker compose build
```

**Warning:** `down -v` wipes the persistent volume, so you will need to log in again afterwards.

### Self-deletion guard

`launch.sh` refuses to run if the worksite path overlaps the SimpleHarness toolbox directory. This prevents dangerous mode from accidentally modifying or deleting the harness itself. If you see this error, pass a different `--worksite` path.

---

## Cleanup

Stop a running container gracefully with Ctrl+C, or from another terminal:

```bash
docker compose down
```

Remove the local image:

```bash
docker compose down --rmi local
```

Remove everything, including stored credentials:

```bash
docker compose down -v --rmi local
```

---

## Further reading

- [Design spec](dev-container.md) — full architecture, security model, and design decisions for the dev container
- [CLI usage](usage.md) — general SimpleHarness CLI reference, TASK.md schema, and directory layout
