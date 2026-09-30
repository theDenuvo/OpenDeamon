# Free-Rank — autorotate free models by rating

Keeps the free pool ordered by measured quality instead of stale taste.
What it does, and how, is below; when to trigger a re-rank is the
core's call (weekly cron exists; manual runs are cheap).

## Signals (all keyless, all $0)

1. Live OpenRouter `:free` catalog — the pool itself (rotates; never
   hardcode ids).
2. Capability flags from the models.dev cache Hermes already keeps
   (tool_call, reasoning, context length, image input).
3. Own micro-probes — the honest "independent tests": 3 tiny tasks
   (reasoning, instruction-following, vision description) run weekly
   against each candidate through the gateway. ~15 free calls/week.
4. External coding signal: Aider polyglot leaderboard snapshot
   (refreshed best-effort; kept cached — a missed refresh never breaks
   a run, the previous snapshot simply stays).

## Scoring (per role)

Roles: `reasoning` (tools + reasoning + big context), `vision`
(image input), `cheap` (fast, small, for delegation).
Score = capability gate (0/1, hard) × probe score × leaderboard
presence bonus. Winners per role are written to `ranking.json`.

## Effect (files this function owns)

- `ranking.json` — winners + full table + timestamp.
- Hermes `config.yaml`: `model.default`, `fallback_providers`,
  `auxiliary.vision`, `delegation.model` (cheap winner).
- `gateway/litellm-config.yaml`: chat-tier deployment order mirrors
  the ranking; gateway restarted after rewrite.
- `aider_snapshot.md` — last parsed leaderboard (cache).

Cron `OpenDeamonFreeRank` (Windows Task Scheduler, Sundays 04:00) runs
`rank.py --weekly --apply` (probes included, ~40 free calls).
Plain `rank.py` re-scores from catalog + cache only (no calls, dry-run);
add `--apply` to rewrite configs.
