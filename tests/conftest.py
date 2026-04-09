# conftest.py
#
# Intentionally minimal. Each test module defines its own local factory
# functions (_state, _task, _cfg, _env, …) because the factories are
# file-specific and have no meaningful duplication across modules.
#
# Add shared fixtures here only when two or more test files need the same
# helper and duplicating it would be a maintenance burden.
