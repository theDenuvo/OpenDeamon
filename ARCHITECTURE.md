# OpenDeamon v0.2 — Architecture (as-built, 2026-09-29)

## Principle (unchanged since v0.1)

Hermes = Agent Engine / Core. OpenDeamon = configuration + tool redirection
on top of Hermes. v0.2 adds two ready-made containers (LiteLLM, OpenWebUI),
both wired by config/env only. Still zero custom runtime code.

## Components

```
Browser → OpenWebUI 127.0.0.1:8081 (docker, data A:\OpenDeamon\owui)   │  OpenAI-compat connection → gateway (env presets)
   ▼
LiteLLM gateway 127.0.0.1:4000 (docker, config A:\OpenDeamon\gateway\)
   Single entry `opendeamon` = the agent (bridge first). Chat tiers
   (od-chat, od-groq, od-fb1/2) are hidden fallback-only. Images ride
   the same entry (bridge saves them, Hermes routes to auxiliary vision).
   Fallback-only groups hidden from OWUI via restricted virtual key
   (Postgres-backed); keys: container env only
   ▼  (Hermes CLI keeps using OpenRouter directly, v0.1 path intact)
A:\OpenDeamon\bootstrap.ps1          process-scoped env (no system changes):
  - HERMES_HOME            = A:\OpenDeamon\hermes-home
  - UV_CACHE_DIR           = A:\OpenDeamon\cache\uv
  - PIP_CACHE_DIR          = A:\OpenDeamon\cache\pip
  - NPM_CONFIG_CACHE       = A:\OpenDeamon\cache\npm
  - PLAYWRIGHT_BROWSERS_PATH = A:\OpenDeamon\cache\ms-playwright
  - TEMP/TMP               = A:\OpenDeamon\cache\tmp
  - OPENROUTER_API_KEY     loaded in-process from
                           A:\AI\daemon\config\secrets.local.toml
                           (never printed, never written to disk)
  - PATH += A:\hermes\bin  (reuse of pre-existing Hermes install)
 │
 ▼
Hermes Agent v0.21.4 (NousResearch/hermes-agent, pre-existing install:
binaries A:\hermes\bin + C:\Users\cheli\AppData\Local\hermes\hermes-agent)
  config: A:\OpenDeamon\hermes-home\config.yaml (minimal, 3 blocks)
  state:  A:\OpenDeamon\hermes-home\ (sessions, state.db, skills hub cache,
          models_dev_cache.json, logs — all on A:)
 │
 ├── model.provider=openrouter, model=nvidia/nemotron-3-super-120b-a12b:free
 ├── fallback_providers (native, per-turn, config-only):
 │     nemotron-3-ultra-550b-a55b:free → qwen3.8-27b:free → gemma-4-31b-it:free
 ├── auxiliary.vision: openrouter / gemma-4-31b-it:free (+fallback_chain:
 │     dots-3-note-preview:free, qwen3.8-27b:free)
 ├── native toolsets (Hermes defaults, no MCP servers added):
 │     web, browser, terminal, file, code_execution, vision, image_gen, tts,
 │     skills, todo, memory, session_search, delegation, computer_use
 ├── delegation: native delegate_task (max 10 parallel children);
 │     unpinned children inherit the parent fallback chain (no override set)
 └── coding worker: opencode CLI (pre-existing install, NOT reinstalled)
       invoked via Hermes terminal tool:
         opencode run -m opencode/nemotron-3.5-lightning-free '<task>'
       (official Hermes skill `opencode` from nousresearch/hermes-agent
       documents exactly this pattern; hub install was blocked by GitHub API
       rate limit, so the pattern is applied directly without the skill file)
```

## Model policy (free-first, enforced by config, no custom quota code)

1. Primary: OpenRouter `:free` model (live catalog checked 2026-09-29,
   16 models; stale hardcodes avoided — e.g. `upstage/solar-pro4:free`
   from the old config is already gone from the catalog).
2. Fallback: 3 more `:free` models, different families (native
   `fallback_providers`, per-turn failover).
3. Vision: dedicated free multimodal model via `auxiliary.vision`
   (main model is text-only).
4. Paid OpenRouter: never selected; spend stays $0 (see usage logs in
   `_setup_tmp/*.usage.json`).
5. Keyless "opencode-free" Hermes provider does not exist in v0.21.4
   (verified against provider docs + fallback provider table). The free
   tier lives in OpenCode itself (`opencode/*-free` models, keyless) and
   is used for the OpenCode worker leg.

## Cost control notes (observed behavior)

- OpenRouter free tier is rate-limited (free-models-per-day; reset 03:00).
  Transient 429s do NOT trigger auxiliary fallback chains by design —
  only quota/payment/connection errors do. Vision model was rotated
  explicitly (qwen → gemma) after a transient 429.
- All fallback chains stay inside OpenRouter `:free`, so a failover can
  never bill the $50/day OpenRouter budget.

## What was deliberately NOT built

- No custom orchestrator / router / MCP bridge / quota manager.
- No extra MCP servers (catalog is SaaS integrations needing API keys;
  native toolsets already cover filesystem, browser, web, vision, shell,
  skills, TTS, image_gen, computer-use).
- No UI (v0.2), no Git repo (banned for v0.1), no OpenWebUI.
- No secret duplication: no `.env` with keys inside the build.

## Core (v0.3, stage 1 — DONE)

- Core = reasoning model + `hermes-home\SOUL.md` persona (OpenDeamon
  identity: proactive orchestrator, RU default, $0 + quota policy,
  detached-GUI rule). Verified: answers as OpenDeamon core.
- Functions = skill packs in `A:\OpenDeamon\functions\`, mounted read-only
  via `skills.external_dirs` (no copies). First pack: `opencode-worker`
  (proven CLI pattern). Builtin `opencode` skill also present — ours
  complements it with machine-verified flags/quota rules.
- Pipelines are NOT fixed: core freely picks research / native tools /
  subagents / opencode-worker per task (understand → research → plan →
  execute/delegate → verify → report).

## Hands bridge (v0.3, stage 2 — DONE)

- `bridge\hands.py` (stdlib only, localhost 127.0.0.1:9131): OpenAI-compat
  `/v1/chat|models` over `hermes -z`. Runs hidden via pythonw; log
  `bridge\hands.log`. One run at a time (429 → gateway falls back).
- Gateway tier `od-hands` (custom openai-compat, timeout 600):
  chat → hands → full Hermes (tools/skills/SOUL) → reply. Fallback:
  od-hands → od-primary. Now the ONLY sanctioned custom code in the build
  (no ready alternative: hermes serve = desktop WS protocol, mcp = stdio).
- Proven: file creation through OWUI→gateway→hands→disk; detached GUI
  launch (no hangs). Persistence across reboot: TODO (Scheduled Task).
- Raw od-* chat models stay dumb terminals (persona = 3 clicks in OWUI
  Workspace → Models, user-side); `od-hands` carries persona + hands.

## Layout

```
A:\OpenDeamon\
  bootstrap.ps1          env bootstrap (dot-source or run with args)
  hermes-home\           HERMES_HOME (config.yaml, SOUL.md, sessions, state)
  desktop\               Hermes Electron UI, unpacked (built from source,
                         moved here; launched with HERMES_HOME above)
  functions\<pack>\SKILL.md   function packs (mounted, not copied)
  cache\{uv,pip,npm,ms-playwright,tmp}   all redirected caches
  gateway\               LiteLLM config + pgdata (v0.2+)
  bridge\                hands.py + hands.log (autostart: Startup lnk + guard)
  owui\                  OpenWebUI data volume (v0.2)
  secrets\               user-owned env file (never read by assistant)
  tests\coding-test\     TEST 4 project (TASK.md, stats.js, stats.test.js)
  tests\combined-test\   TEST 5 project
  tests\HelloWorld.cpp   notepad proof
  _setup_tmp\            one-off setup scripts + *.usage.json (proof of $0)
  DEPLOYMENT_REPORT.md   full report
  ARCHITECTURE.md        this file
```
