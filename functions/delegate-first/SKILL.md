# Delegate-First — conductor enforcement, not advice

A `pre_tool_call` shell hook that turns "prefer delegation" from a persona
suggestion into a mechanism: direct execution tools have a small per-turn
budget, and overspending fails the call with a redirect message naming the
exact alternatives. Any model obeys — the instruction arrives in-context
as a tool error, not as a system-prompt wish.

## What it does

- Counts direct execution calls per (session, turn):
  `terminal`, `write_file`, `patch`, `edit`, `code_execution`,
  `execute_code` (the last two added after a live bypass was observed —
  the model routed around a block via `execute_code`; closed the same day).
- HARD RULE: every code-writing tool is blocked outright —
  `write_file`, `patch`, `edit`, `code_execution`, `execute_code`. The core
  does not write code; the block message hands it the exact
  `opencode run -m opencode/<free-model> '<task + acceptance>'` command.
  No budget, no retries, no loopholes.
- `terminal` budget: 6 per turn, for looking around and verifying worker
  results (running tests, reading diffs).
- Escape hatch (learned 2026-09-29): `opencode run …` invocations are
  NEVER blocked — otherwise a model that spent its budget exploring
  could no longer reach the worker at all (observed deadlock: stall
  asking to "continue next turn"). Delegation must stay reachable
  past the budget; only raw DIY stays blocked.
- State: `hermes-home/cache/delegate-first.json` (pruned to recent turns).

## Known framework limits (observed, not fixable from here)

- One-shot runs (`hermes -z`, and therefore the hands bridge) cannot
  spawn subagents (`delegation.oneshot_max_children` exhausted) — the
  framework itself answers "do the remaining work yourself inline".
  Full delegation fan-out lives on interactive surfaces (TUI/Desktop).
  The hook still fires there and shapes behavior toward the worker
  pattern instead.
- Children get their own turn budgets, so fan-out is never throttled by
  the parent's spending.

## Why code-writing is a hard block

A budget invites negotiation, and the model negotiates: it spends the
budget on reconnaissance, then stalls ("continue next turn"). Observed live
three times. So the rule is now absolute — the core never touches code
files, only the opencode CLI worker does — while `terminal` keeps a
6-call budget for reading and verification. Delegation stays reachable at
any point of the turn.

## Payload shape (Hermes v0.21.4, verified live)

```json
{"hook_event_name": "pre_tool_call", "tool_name": "terminal",
 "tool_input": {"command": "..."}, "session_id": "...",
 "cwd": "A:\\...", "profile": "default",
 "extra": {"task_id": "...", "turn_id": "...", "tool_call_id": "..."}}
```

Two bugs lived here for hours and both were silent: reading `args`
instead of `tool_input` (so the worker exemption and the
`delegate_task` block never fired), and reading `turn_id` from the top
level instead of `extra` (so the per-turn budget never reset — every
turn of a session shared one counter, key ended in `?`). Log a payload
before trusting a matcher.

## Trust & scope

- Shell hook declared in `hermes-home/config.yaml` (`hooks:` block).
- First-use consent: `hooks_auto_accept: true` covers only this HOME,
  which contains no third-party hooks; allowlist persists alongside.
- Non-interactive runs also set `HERMES_ACCEPT_HOOKS=1` (bootstrap,
  bridge) as belt and suspenders.
