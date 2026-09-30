# OpenCode Worker

The opencode CLI as an autonomous coding worker: implement, refactor,
review, test, fix — in its own context, while the core keeps the plan.
What it's good at and how it behaves is below; whether to use it on a
given task is the core's call.

## What it offers

- Self-contained coding units with verifiable acceptance (a test command,
  a build, a review checklist).
- Isolated context: worker burns its own tokens, core stays clean.
- Non-interactive one-shot runs from the terminal tool.

## Verified invocation (this machine)

```bash
opencode run -m opencode/space-bunny-free '<task + acceptance>'
```

The `-m` flag is not optional: without it the CLI falls back to its
configured default, which on this machine has repeatedly resolved to an
`openrouter/*` route — burning the shared OpenRouter daily bucket.
Observed live 2026-09-29: a run without `-m` used
`openrouter/nemotron-3-super` instead of the ladder. Always pass `-m`
with a ladder id, top-down; first working wins.

## Model ladder (project decision 2026-09-29 — quality/speed over OR
subagents for ALL coding; verified live, top-down, first working wins)

1. `opencode/space-bunny-free` (reordered 2026-09-29: user promoted it to
   the top; everything else shifted down one)
2. `opencode/muse-spark-1.3-contributor-free` (+ reasoning Xhigh where the
   CLI supports it) — contributor tier: Meta may train on prompts, so no
   secrets or personal data in these tasks, ever.
3. `opencode/nemotron-3-ultra-free`
4. `opencode/big-pickle`
5. any other `opencode/*-free` from the live list (`opencode models`).

## Handoff protocol (core → worker → verify)

1. Core sends a complete unit: what to write (X), how (Y), where to save
   (Z), must-haves (W), forbiddens (Q), docs/links (U) — everything the
   worker needs, no follow-up questions expected.
2. Worker replies with confirmation + what it did (its "done" is a claim).
3. Core verifies independently: runs the tests/build, reads the diff.
   Failures go back to the worker with the error text; only then manual.
4. "ALL TESTS PASSED" is NOT verification. Always re-read the test file
   and check the assertions still encode the ORIGINAL spec, not the
   worker's own output. Observed 2026-09-29: asked for `'$1,234.50'`,
   the worker shipped `',234.50'` and rewrote the assertion to match its
   own bug; the test went green on a wrong implementation. Green tests
   prove the worker's self-consistency, never the spec.

- Models under `opencode/` need no API key (`opencode models` lists them;
  ids rotate — promo lineup, check live on failure).
- Note: `openrouter/*` inside opencode CLI draws from the same OpenRouter
  daily bucket as the main agent; `opencode/*-free` ids don't.

## Observed behavior

- Given explicit acceptance criteria, the worker implements + runs checks
  itself; results still deserve independent verification (run the tests,
  read the diff) since its "passed" is a claim, not proof.
- On model errors, another `opencode/*-free` id from the live list has
  worked; wrong flags (e.g. bad `--model` values) fail fast with clear
  errors — also observed live.
- Permission system (observed 2026-09-29): the worker auto-rejects
  `external_directory` on arbitrary paths — tasks landing outside
  allowed roots need pre-created dirs or adjusted scope.
