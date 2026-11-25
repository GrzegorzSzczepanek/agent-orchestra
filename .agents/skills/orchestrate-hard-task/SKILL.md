---
name: orchestrate-hard-task
description: Use for a large or hard implementation task that can be split into several small, testable parts. A controlling model (Claude, OpenCode, Codex) plans and reviews, and the parts are done by cheap workers through the run_plan and run_status MCP tools.
---

# Splitting a hard task and handing parts to workers

1. **Explore the repo** (grep, existing tests). Figure out how tests are run. Remember: workers see only the committed HEAD, so first commit or stash the changes the task depends on.
2. **Split the goal into 3 to 8 tasks.** Each has: `id`, a self-contained `description` (file names, signatures, expected behaviour), `files`, `acceptance`, `test_cmd` (one shell-free command, e.g. `pytest -q tests/test_x.py`, the test must fail before the change). The command runs in a clean worktree from HEAD, without `.venv` and other files outside git, so do not use paths relative to them (`.venv/bin/pytest`). Give the command from PATH (`python3 -m pytest -q ...`) or the full path to the binary. When the program is missing, the task ends with `config_error` right away, without spending tokens, `cwd` (a subdirectory of the repo where the worker and the test run, when the project is not at the root, e.g. `code`; `test_cmd` runs right there), `difficulty`, `depends_on`. The user sees the workers' work live and can send them hints (`/hint`) or cancel the run (`/cancel`). Tasks in different files without dependencies run in parallel. One task should touch no more than 1 to 3 files.
3. **Do `hard`, project-wide and security tasks, and anything without a checkable test, yourself**, do not put them in the plan. If a task depends on your part, commit it before `run_plan`.
4. Call `run_plan(goal, tasks)` (optionally `review_rounds=1` when you want an independent review of each task's diff by the reviewer role, at the cost of its limit). It returns a `run_id` right away. Tell the user that progress is visible in the TUI panel beside it (started by `orch`). Without the TUI: `orchestra watch --repo .`.
5. Poll `run_status(run_id, wait_s=60)` until `finished=true`. On `config_error`, `auth_failed`, `insufficient_credits` do not retry, tell the user.
6. **Review the result:** `git diff HEAD...<integration_branch>` (the branch from `integration_branch`). Do tasks with status `failed`, `blocked` or `over_budget` yourself. Resolve the issues, then merge: `git merge --no-ff <integration_branch>`.
7. Run the project's full tests before finishing and show the result.

A single, simple task without a plan: skill `delegate-simple-tasks` (the `delegate` tool).

The user sees the task table in the TUI and can send hints to workers or messages to running agents. You can see the agents from the TUI through `list_agents` and write to them through `post_message`.
