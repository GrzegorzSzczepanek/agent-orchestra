---
name: delegate-simple-tasks
description: Use when an implementation task has a clear scope, known files and a test command. Delegate it to a cheaper worker through the delegate MCP tool, and review the diff with a strong model.
---

# Delegating simple tasks

1. Assess the task. Delegate only when you know the files, the acceptance criteria and the test command.
2. Prepare a `task_spec`: `prompt` (concrete, with file names), `test_cmd`, optionally `model`.
3. Call the `delegate` tool. You get the diff, the test result and token usage.
4. Check `status`. `ok` means green tests. `config_error` (no model: set ORCHESTRA_CMD_MODEL), `auth_failed`, `insufficient_credits`, `worker_not_found` are environment problems, do not retry them, tell the user about them. Retry `max_turns` and `tests_failed` at most once with a refined prompt.
5. Review the diff, not the whole worker session. After two test failures in a row, do the task yourself.
6. Do not delegate schema changes, security work or tasks that need research.
