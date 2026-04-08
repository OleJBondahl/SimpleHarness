#!/usr/bin/env bash
# simpleharness approver mode — PreToolUse hook, fast path.
#
# Reads a tool-use envelope on stdin, matches the Bash command against
# <worksite>/simpleharness/tasks/<slug>/.approver-allowlist.txt using
# bash 'case' pattern matching. On a match, emits an allow JSON on
# stdout and exits. On a miss, exec's the Python slow path.
#
# stdout is reserved for the single verdict JSON. Diagnostics go to
# stderr. Always exits 0 — any internal failure either passes through
# to the Python slow path or silently hands control back to Claude
# Code's normal permission flow.
set -u

envelope=$(cat)

tool_name=$(printf '%s' "$envelope" | jq -r '.tool_name // ""')
if [ "$tool_name" != "Bash" ]; then
    # Matcher in .approver-settings.json should scope this hook to Bash
    # only, but be defensive: if we somehow see a non-Bash call, silent
    # exit so Claude Code continues its normal permission flow.
    exit 0
fi

command=$(printf '%s' "$envelope" | jq -r '.tool_input.command // ""')
if [ -z "$command" ]; then
    exit 0
fi

worksite="${SIMPLEHARNESS_WORKSITE:-}"
task_slug="${SIMPLEHARNESS_TASK_SLUG:-}"

# Defense-in-depth: reject path-traversal / weird shapes in the slug
# before we splice it into a filesystem path. The harness always sets
# a kebab-case slug (matching 'simpleharness new'); anything else means
# the env is untrusted — silent exit lets Claude Code's normal permission
# flow handle it (the slow path would also reject it with a diagnostic).
if [ -n "$task_slug" ] && ! [[ "$task_slug" =~ ^[A-Za-z0-9._-]+$ ]]; then
    exit 0
fi

allowlist="${worksite}/simpleharness/tasks/${task_slug}/.approver-allowlist.txt"

if [ -n "$worksite" ] && [ -n "$task_slug" ] && [ -f "$allowlist" ]; then
    while IFS= read -r pattern || [ -n "$pattern" ]; do
        # Strip trailing CR (handles CRLF line endings on Git Bash).
        pattern="${pattern%$'\r'}"
        # Strip leading / trailing whitespace.
        pattern="${pattern#"${pattern%%[![:space:]]*}"}"
        pattern="${pattern%"${pattern##*[![:space:]]}"}"
        [ -z "$pattern" ] && continue
        case "$pattern" in \#*) continue ;; esac

        # Glob match. Bash 'case' uses fnmatch-equivalent semantics
        # (*, ?, [...]) and does NOT perform shell expansion on the
        # pattern — this is pure pattern matching, not code execution.
        case "$command" in
            $pattern)
                # Construct the verdict JSON via jq so special chars in
                # the pattern (quotes, backslashes) are properly escaped.
                jq -cn --arg pattern "$pattern" '{
                  hookSpecificOutput: {
                    hookEventName: "PreToolUse",
                    permissionDecision: "allow",
                    permissionDecisionReason: ("matched approver fast path: " + $pattern)
                  }
                }'
                exit 0
                ;;
        esac
    done < "$allowlist"
fi

# Miss: hand off to the Python slow path. exec replaces this process
# so the Python startup is the only cost on the slow path — no extra
# bash overhead on top. The Python slow path reads the envelope from
# its own stdin (the here-string).
exec python -m simpleharness.approver_shell <<< "$envelope"
