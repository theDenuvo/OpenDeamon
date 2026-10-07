# OpenDeamon v0.1 — Deployment Report (2026-09-29)

Fresh build in `A:\OpenDeamon`. No legacy envs, caches, or configs reused.
No `.git` created. Nothing installed on `C:` (audit in §17).

## 1. Installed / reused components

| Component | Version | Location | Notes |
|---|---|---|---|
| Hermes Agent (binaries) | v0.21.4 (2026.9.21, upstream d3b25b52); на сервере v0.21.5 | `A:\hermes\bin\` + `C:\Users\cheli\AppData\Local\hermes\hermes-agent` | PRE-EXISTING, reused as-is, NOT reinstalled |
| Hermes Python | 3.11.16 (own venv) | inside pre-existing install | untouched |
| Hermes Node.js | bundled | `A:\hermes\node\` | untouched |
| Hermes uv | bundled | `A:\hermes\bin\uv.exe` | untouched |
| OpenCode CLI | 1.18.32 | `C:\Users\cheli\AppData\Roaming\npm\` (node `Z:\Node.js`, v22.13.0) | PRE-EXISTING, NOT reinstalled |
| Docker | 29.8.0 | `C:\Program Files\Docker\` | untouched |
| Node.js (system) | v22.13.0 | `Z:\Node.js\` | reused, untouched |
| uv (user) | 0.11.21 | `C:\Users\cheli\.local\bin\` | not used by build |
| Python launcher | 3.14.0 (`py`) | system | used only for one-off setup scripts |

**Версия Hermes на сервере: `v0.21.5+8306.gfd4dc86` (2026.9.24), метод `git`.**
Таблица выше — постановка на Windows (`v0.21.4`). На сервере версия
новее: `v0.21.4` на PyPI отсутствует (там `0.19.0`), а сборка из
исходников была бы отдельным решением владельца. Принято с пометкой:
разница версий меняет номер сборки, а не архитектуру.

NEW files created by this deployment (all on `A:`): `bootstrap.ps1`,
`hermes-home\config.yaml`, `hermes-home\*` (state/sessions/skills hub),
`cache\*`, `tests\*`, `_setup_tmp\*`, this report, `ARCHITECTURE.md`.

## 2. Hermes HOME

`A:\OpenDeamon\hermes-home` via `$env:HERMES_HOME` (process-scoped,
`bootstrap.ps1`). Verified: `hermes status`/`doctor` resolve auth file,
config, sessions, state.db, skills, logs, cache/scratch there (~57 MB,
dominated by 50 MB skills-hub index + 5 MB model catalog cache).

## 3. Provider / model / fallback

- Primary: `openrouter` / `nvidia/nemotron-3-super-120b-a12b:free`
  (tool-calling + reasoning, 262k ctx; picked from LIVE OpenRouter catalog,
  16 `:free` models on 2026-09-29 — cache had 21, i.e. catalog rotates).
- Native fallback chain (`fallback_providers`, per-turn): ultra-550b → qwen3.8-27b → gemma-4-31b.
- Delegation: no override — unpinned children inherit parent chain (documented default).
- Keyless Hermes provider `opencode-free` DOES NOT EXIST in v0.21.4
  (checked provider docs + fallback table). Free tier used = OpenRouter
  `:free` for Hermes + keyless `opencode/*-free` for the OpenCode worker.
- `OPENROUTER_API_KEY`: existing `A:\AI\daemon\config\secrets.local.toml`,
  loaded into env in-process only. Never printed, logged, or duplicated.
- Total model spend across all tests: **$0.00** (`_setup_tmp/*.usage.json`).

## 4. MCP / tools

- MCP servers added: **none**. Catalog = SaaS integrations needing API keys.
- Native toolsets used (Hermes defaults): web, browser, terminal, file,
  code_execution, vision, image_gen, tts, skills, todo, memory,
  session_search, delegation, computer_use.
- Filesystem scope: agent works in `A:\OpenDeamon`; no full-disk grants.
- Official Hermes skill `opencode` (nousresearch/hermes-agent): pattern
  verified (skill text fetched from skills.sh), hub install blocked by GitHub API 60/hr rate limit (retried 3×, incl. after
  reset); applied directly — no custom bridge.

## 5. Vision

Works on free tier. `auxiliary.vision` = `openrouter` /
`google/gemma-4-31b-it:free` + fallback_chain (dots, qwen).
First choice `qwen/qwen3.8-27b:free` hit transient upstream 429
(shared pool) — transient 429s do NOT auto-fallback by design, so the
model was rotated explicitly to gemma. Stated honestly: infra installed,
free-model availability varies with upstream rate limits.

## 6. Browser

Native: `agent-browser` via npx + pre-existing Playwright Chromium
(`%LOCALAPPDATA%\ms-playwright`, dated 2026-06-22, untouched).
`NPM_CONFIG_CACHE` + `PLAYWRIGHT_BROWSERS_PATH` redirect to `A:` for any
future downloads. Browser automation itself not exercised in v0.1 tests
(web-search toolset covered research needs).

## 7. Test results

| Test | Result | Evidence |
|---|---|---|
| T1 basic agent (terminal+file tools) | PASS | echoed string + dir listing returned |
| T2 web research (3+ sources, synthesis, RU table) | PASS | options table + recommendation, 7 API calls |
| T3 vision (`04_chart_ru.png`) | PASS (after qwen 429 → gemma) | accurate RU description incl. values 23/17/11/30/15 |
| T4 coding via OpenCode | PASS | Hermes ran `opencode run -m opencode/nemotron-3.5-lightning-free`, implemented `stats.js`+tests, verified `ALL TESTS PASSED` (re-verified independently). First attempt failed (bad model flags) — fixed with existing means, no custom adapter |
| T5 combined research→coding | PASS | research (2 sources) → native implementation (`debounce.js` + tests, opencode judged unnecessary) → `All tests passed!` (re-verified independently). Bonus: ran on fallback model `nemotron-3-ultra` after primary still limited — native failover proven live |

OpenCode probe (direct): `opencode run -m opencode/nemotron-3.5-lightning-free`
creates files headless, keyless, no approval hang — `hello.txt` = `OPENCODE-PROBE-OK`.

## 8. OpenRouter free quota incident (honest log)

After T4, OpenRouter returned `free-models-per-day` 429 on all attempts:
quota resets 03:00. All fallback entries are same-provider `:free`, so no
failover possible (documented behavior). T5 ran after reset — and itself
proved fallback: primary `super-120b` was still limited, Hermes
automatically failed over to `ultra-550b` mid-turn ($0.00, completed).
No paid models used, no quota system built. This is a free-tier property,
not a build defect.

## 9-11. OpenCode integration / research / combined

As §7. Hermes autonomously chose opencode delegation in T4 (first with
wrong flags → reported errors honestly → succeeded with working pattern).
Research works via native web toolset. Combined test = T5 pending.

## 12-14. Not connected (with reasons)

- Video/stt/kanban toolsets: disabled by default, need keys — not needed.
- MCP servers: none needed (native coverage sufficient).
- `opencode` hub skill file: rate-limited; pattern applied directly.
- TTS/image_gen: native toolsets present, not exercised (no test need).

## 15-16. (covered in §7)

## 17. C: audit

| Path | State | Verdict |
|---|---|---|
| `A:\OpenDeamon\cache\{uv,pip,npm,ms-playwright,tmp}` | our caches, 4.5 MB | on A: by design ✓ |
| `%LOCALAPPDATA%\npm-cache\_logs` 58 KB (29.09 01:44–01:49) | from pre-bootstrap inspection commands | tiny, pre-existing dir, no installs |
| `A:\OpenDeamon\cache\npm\_cacache` 1.5 MB | post-bootstrap npm ops redirected | redirect works ✓ |
| `%LOCALAPPDATA%\uv\cache` | does not exist | no uv writes ✓ |
| `%LOCALAPPDATA%\ms-playwright` | 2026-06-22, untouched | pre-existing ✓ |
| `%LOCALAPPDATA%\hermes`, `%APPDATA%\hermes`, `A:\hermes` | pre-existing install; only change: Hermes's own launcher re-copy on first run | untouched by design, not deleted |
| opencode data (`\.local\share\opencode`) | pre-existing; `opencode run` sessions land there | owned by existing install, no new installs |
| `%TEMP%\*.tmp` (~6 MB, 01:27–01:56) | harness/node noise, unattributed | left untouched (no blind deletes) |
| `C:` free space | 5.8 GB at start → 13.6 GB at end (+7.7 GB freed by unattributed system cause — no deletions performed by this deployment; likely OS maintenance) | no large writes made |

No new programs, venvs, node_modules, browser binaries, or model caches
were placed on `C:`. Old OpenDeamon/Hermes caches were NOT deleted.

## 18. DoD checklist

- [x] `A:\OpenDeamon` from scratch — [x] HERMES_HOME on A: — [x] no new C: deps/caches
- [x] OpenCode/Docker untouched — [x] no Git — [x] free provider verified live
- [x] `opencode/*-free` verified via CLI — [x] native fallback configured (failover across providers not testable without outage; chain validated by config + docs)
- [x] filesystem/browser-present/web/vision verified — [x] OpenCode delegation verified
- [x] coding test done — [x] combined test done (incl. live fallback proof)
- [x] reports written — [x] C: audit done

## 19. Limits for v0.2

UI, cron/gateway use, skill-hub install retry, browser-automation exercise,
paid-model policy, Git/GitHub integration, long-run memory curation.

---

# v0.2 — Gateway + OpenWebUI (2026-09-29, build phase)

## 20. Key list (permanent free tiers only — trials with expiry rejected)

To register (user does it): GOOGLE_API_KEY (AI Studio, no card, multimodal),
GROQ_API_KEY (forever-free 30RPM/1K RPD, OpenAI-compat), CEREBRAS_API_KEY
(1M tok/day free, no card), NVIDIA_API_KEY (build.nvidia.com), HF_TOKEN
($0.10/mo renewing), Copilot-free OAuth (verify), Qwen/MiniMax OAuth (verify),
Fireworks/Novita/DeepInfra/Z.ai/Kimi/Mistral only if perpetual no-card free.
Tools: Tavily, Brave Search, Jina (keyless), ElevenLabs. Keyless lane:
Pollinations legacy text API (gpt-oss-20b) — probed ALIVE 2026-09-29.
Rejected: OpenAI/Anthropic/DeepSeek/xAI API, Zen/Go, Cohere, time-trial credits.

## 21. LiteLLM gateway (config-only, Docker, $0)

- Container `opendeamon-litellm` (image ghcr.io/berriai/litellm:main-latest,
  layers on Docker data-root; config `gateway/litellm-config.yaml` on A:).
- Models: od-primary (OR nemotron-super free) → fb od-fb2 Pollinations
  keyless → od-fb1 (OR ultra free); od-vision (OR gemma free) → qwen free.
- Master key: self-generated, `gateway/.master_key.env` (localhost only).
- OPENROUTER_API_KEY passed as container env from bootstrap shell (in-process,
  never in files). Migration path for user keys: user-owned
  `secrets/gateway.env` + --env-file (assistant never reads it).
- Gotcha fixed: proxy `fallbacks:` must live under `router_settings:`
  (top-level key is silently ignored — found in LiteLLM docs).
- Proofs: /v1/models lists 5 groups; chat via od-primary OK; streaming
  149 chunks; FORCED failover (primary api_base broken) → exact answer
  served by `gpt-oss-20b` (fb2). 503 upstream flakiness observed (Nvidia
  overload) — free-tier property.
- Port: 127.0.0.1:4000. Restart policy: unless-stopped.

## 22. OpenWebUI (existing image v0.11.3, NOT pulled new)

- Container `opendeamon-owui`, volume `A:\OpenDeamon\owui` (webui.db + 931 MB
  embedding cache `all-MiniLM-L6-v2` auto-downloaded on first boot — on A:).
- URL: http://127.0.0.1:8081 (8080 was occupied by third-party process).
- Connection to LiteLLM pre-seeded via OPENAI_API_BASE_URLS/KEYS env
  (host.docker.internal:4000/v1).
- E2E proof with ephemeral admin (then wiped to pristine): models visible
  through OWUI (od-*), chat returned exact answer. No leftover users/chats.
- First launch = user signup in browser (admin).

## 23. Executor badge per tool (deferred)

OWUI renders tool calls natively; per-executor model badge needs a small
OWUI Function filter (sanctioned extension) — scheduled after keys phase.
No custom runtime planned.

## 25. Tier-2 Groq (user key, blind insert — 2026-09-29)

- `secrets/gateway.env` created BY USER (assistant never opened it).
  Both containers re-created with `--env-file` only; no `-e` secret
  passthrough remains. OPENROUTER copy validated (od-primary answers).
- Groq direct: HTTP 403 `Access denied.` Root-caused WITHOUT seeing the key:
  (1) egress identical for host and container (2.59.158.228, DE);
  (2) host curl 200 → key valid; (3) anonymous container request → 403,
  with browser UA → 401 → edge filters non-browser UAs (mitigated via
  extra_headers UA, kept in config); (4) authenticated container call →
  404 → `llama-3.3-70b-versatile` retired by Groq. Fix: live catalog pulled
  from user's 200 response, tier now `groq/openai/gpt-oss-120b`
  (120B, tools+reasoning, 131k ctx) — direct call OK.
- Cross-provider failover PROVEN: sabotaged primary → served by
  `openai/gpt-oss-120b` (Groq). Config reverted, gateway healthy
  (od-primary chat OK, streaming 29 chunks).
- Graceful degradation proven anyway: od-groq request auto-fell back to
  od-fb2 (Pollinations) with exact answer; added `od-groq → [fb2, fb1]`
  chain entry. Chain now: primary → groq → pollinations → ultra.
- OWUI re-verified E2E after env switch (models incl. od-groq, exact chat
  answer), wiped to pristine again.

## 24. C: audit v0.2

- New on C: by this deployment: nothing (58 KB npm _logs earlier).
- C: 13.6 → 12.0 GB during v0.2 window: cause identified — third-party
  `npm exec opencode-mobile install` (+ngrok deps, ~1.1 GB in npm-cache,
  10:14–10:17), NOT this deployment. Left untouched.
- Docker layers/images: on Docker data-root (VM disk), not C: user dirs.
- Containers: opendeamon-litellm, opendeamon-owui (unless-stopped).

## 26. Quota math (measured 2026-09-29) + opencode reserve

OpenRouter :free on this account: 20 RPM, 50/day (used 27 at check),
reset 03:00 MSK. [2026-09-29 LATER: topped up via ggsel/crypto,
free limit now 1000/day (used 29, remaining 971); key spend limit $1/day,
paid usage $0.00 — 0₽ rule holds.] Groq gpt-oss-120b free: 30 RPM / 1K RPD / 8K TPM /
200K TPD (TPD is the binding constraint for fat agent turns: ~4 turns/day;
lean OWUI chats ~100-200/day). Pollinations anon: unpublished, best-effort.
opencode/*-free: separate unmetered promo bucket ("limited time", no
published limits — never hit in practice); CANNOT be a gateway tier
(zen endpoints demand CLI-internal auth: anonymous HTTPS → 403; auth
lives in compiled binary — bridge would be custom code, deferred).
Rule enforced in SOUL.md: on OR 429 → chat via next free tiers, delegate
ALL coding to `opencode run -m opencode/<free>`; never `openrouter/*`
inside opencode CLI (shares the 50/day). $10 OR top-up noted (50→1000/day
forever) — user deferred until salary, 0₽ rule holds.

## 26-bis. Emergency paid reserve (GPT) — procedure, **НЕ АКТИВИРОВАНО** (2026-10-04)

Status, verbatim: the reserve is **документировано, не активировано**. There is
no paid route in `hermes-home/config.yaml` — `model:` is `openrouter`, and every
entry of `fallback_providers`, of `auxiliary.review.fallback_chain`, of
`auxiliary.vision.fallback_chain` and of the MoA preset is `:free` or local.
This section is a runbook for the day the cloud `:free` tier dies as a whole.
Nothing below runs in the build, and no key for it exists yet.

### (а) Триггер, и чем он отличается от 429/503 на одном провайдере

The trigger is: **the cloud `:free` catalog is gone as a whole** — the
OpenRouter `:free` entries this project routes to are no longer served (catalog
change, promo end). Every other code is a per-provider condition that resolves
by itself or by fixing the environment, and none of them are a reason to pay:

| Code / symptom | What it actually means | Where it is fixed |
|---|---|---|
| 429 `free-models-per-day` | quota spent; resets 03:00 MSK. `congested` — free-rank deliberately does not punish it | §8, §26, `free-rank/rank.py:118-119` |
| 5xx, or a dead endpoint holding the connection 75-81 s | upstream congestion; two attempts before a model is declared dead | `hermes-home/config.yaml` comment on `model.default` |
| 401 | the KEY — there is no key at all. Never a catalog change | SCHEME §3-bis |
| 403 with a known-good key | the tunnel or the geography, not the key. Regenerating keys on a 403 is pure waste | SCHEME §3-bis, `hermes-home/SOUL.md` |
| 451 (NVIDIA NIM) | sanctions block | SCHEME §3-bis |
| 404, or the id gone from `GET /v1/models` | that endpoint is `dead` and free-rank excludes it | `free-rank/rank.py:118` |

Diagnosis order, never reversed: **VPN first, keys second** (SCHEME §3-bis).
With the tunnel down, OpenRouter answers 403 and the entire scheme looks like a
dead key. Only after the tunnel is confirmed do the codes mean catalog death.

### (б) Что поднимается и какими переменными

Nothing already in the repository is reused.

- `OPENAI_API_KEYS` is **dead — HTTP 401 "Incorrect API key"** (verified
  2026-09-30, `TODO.md:1421`). `bootstrap.ps1:78-80` deliberately does not load
  it. Never revive it.
- Paid OpenAI is rejected as a path: §20 ("Rejected: OpenAI/Anthropic/DeepSeek/
  xAI API, Zen/Go, Cohere, time-trial credits"), §19 lists paid-model policy as
  a deferred v0.2 item, and `TODO.md` §2-bis.8 closes it again. (§12-14 is NOT a
  citation for this: it lists disabled toolsets and needs no keys. Citing it
  would be exactly the kind of plausible-looking but wrong reference this
  project has already paid for twice.) The reserve is therefore a *new,
  owner-created* key, and the
  owner explicitly re-decides the $0 law at that moment — that is the whole
  point of writing the procedure instead of shipping the route.
- Mechanism, taken from what this build already proves: a `custom` entry with
  `base_url` + `key_env`. `hermes-home/config.yaml` documents it verbatim for
  the Groq rail — `key_env` reads through the profile-scoped `secret_scope`, so
  the secret never lands in this git-tracked file even transiently. Vendor-
  standard variable name: `OPENAI_API_KEY`
  (`hermes-home/skills/autonomous-ai-agents/codex/SKILL.md:30`).
- **No GPT model id is recorded anywhere in this repository**, so none is written
  here. The owner reads it off the live catalogue that day; copying an id out of
  a document is how a stale paid id ends up routing traffic.
- The key goes into an owner-owned secrets file
  (`A:\AI\daemon\config\secrets.local.toml`, or `A:\OpenDeamon\secrets\gateway.env`
  for the `KEY=value` form) which the assistant never opens (ARCHITECTURE, "What
  was deliberately NOT built"), and is exported process-scoped by
  `bootstrap.ps1`. Never into `config.yaml`, never into git, never printed.

### (в) Лимит на ключе: $1/день, и почему именно маленький

The project law is $0 (§26: spend limit $1/day, paid usage $0.00 — a limit is
not a budget). A hard cap of exactly that size is what makes the reserve safe
rather than merely small. The realistic worst case of one long agent day is MoA
fan-out plus up to 10 delegated children (`config.yaml`, `delegation`), and
"every free lane is dead" is precisely the condition in which nothing gets
throttled by a 429. Above roughly $1/day the failure mode stops being "we paid a
little for uptime" and becomes "we did not notice a loop". Set the cap on the
vendor's key page *before* the first call.

### (г) Порядок действий — только конфигом, `/model` вручную никогда

The project routes without `/model` on purpose (TODO §4: `smart_model_routing`
is dead code, no per-task router exists, ARCHITECTURE "Model policy"). A manual
model switch would be invisible to `config.yaml`, to free-rank's `--apply` and
to the routing-map check — which is exactly how a paid route becomes silent.

1. Confirm the tunnel (§3-bis), then read the catalogue: the free rail's
   `GET /v1/models`. The trigger is that the `:free` ids the config needs are
   gone — not a 429 on one of them.
2. Owner creates the key with the $1/day cap and places it in the secrets file.
   Owner only; the assistant does not read that file.
3. `bootstrap.ps1` — add `OPENAI_API_KEY` to the allow-list at
   `bootstrap.ps1:80` beside `NVIDIA_API_KEY` / `GROQ_API_KEY`, so it is
   exported process-scoped and never printed. Revert this line together with
   step (д) 2.
4. `hermes-home/config.yaml` — add the reserve as the **last** element, never
   first: after the existing `:free` entries of `fallback_providers` (a
   `provider: custom` element with `base_url: https://api.openai.com/v1`,
   `model: <id from the live catalogue>`, `key_env: OPENAI_API_KEY`); if the
   core's `model:` block does not accept `custom` + `base_url` + `key_env`, the
   paid provider goes there as `model.provider` / `model.default` instead.
   Which of the two the resolver accepts is settled by the verification in step
   5, not by a guess — an unverified route is worse than no route.
5. Verify by command, not by eye: restart the consumer, then
   `hermes doctor` (auth resolves, issue count did not grow) and
   `python functions/verdict/verdict.py routes --config hermes-home/config.yaml --core-provider openrouter`
   — the reserve must appear only as the tail.
   Live proof: one real turn returning HTTP 200 with non-empty content. The
   emptiness trap is real and already documented (SOUL.md, `max_tokens` spent on
   reasoning returns 200 with no content).
6. Log the activation: date, the catalogue evidence that `:free` was gone, and
   spend before/after against the $0.00 baseline of §26.

### (д) Как вернуть `:free` обратно и чем это доказать

Same path, in reverse, still config-only. The point of this item is not the
mechanics — they are the same two edits as (г) — but the proof at the end:
«вернулось» без доказательства означает «кажется, вернулось».

1. Remove the reserve element from `hermes-home/config.yaml`.
2. Remove `OPENAI_API_KEY` from the `bootstrap.ps1:80` allow-list and from the
   secrets file.
3. `python functions/free-rank/rank.py` — a dry run must show `:free` eligible
   again; use `--apply` only if the dry run names a different winner (§33
   hysteresis keeps the incumbent otherwise).
4. Verify: `hermes doctor`; `verdict.py routes ...` shows no paid provider;
   `python functions/routing-map/test_routing_map.py` and
   `python functions/mcp-policy/test_mcp_policy.py` green (these are the $0-rail
   checks — `test_no_paid_model_is_reachable_through_the_config` is the
   interlock that turns a forgotten reserve red); one live turn on a `:free` id
   returns 200 with non-empty content.
5. Read the spend line: it must be $0.00 for the day the reserve was used. If it
   is not, the revert did not remove every paid route — that is the failure this
   whole section exists to make visible.

### (е) НЕ АКТИВИРОВАНО — явная строка

**This reserve is documented and NOT activated: не активировано.** As of this
commit there is no paid GPT entry in `model:`, in `fallback_providers`, in any
`fallback_chain`, or in the MoA preset, and no paid key is loaded by
`bootstrap.ps1`. It is a procedure, not a route, and
`functions/reserve/test_reserve_not_activated.py` asserts exactly that — so the
claim is checked on every CI run instead of promised here.

## 27. Hands bridge (chat finally has hands)

Problem: OWUI raw models have no tools/persona ("I can't launch apps").
Fix (only sanctioned custom code in build): `bridge\hands.py` — stdlib
Fix (only sanctioned custom code in build): `bridge\hands.py` — stdlib
OpenAI-compat server over `hermes -z` on 127.0.0.1:9131 (hidden pythonw,
log bridge\hands.log, one run at a time → 429 falls back to od-primary).
Gateway tier `od-hands` (timeout 600). Rejected alternatives: hermes serve
(desktop WS protocol), mcp serve (stdio-only), OWUI Function (needs admin
install; user owns the admin account).
Proven: HANDS-ALIVE; file write through full chain (hands-marker.txt);
detached notepad launch (no hang after SOUL detached-GUI rule). Fresh
Notepad window visually unconfirmed (pre-existing user windows left
untouched). Raw od-* models: persona = 3 clicks (OWUI Workspace → Models
→ System Prompt), user-side.
TODO: bridge autostart on reboot (Scheduled Task); per-executor badge
(OWUI Function, still needs admin-side install).

## 28. Seamless UX: 3 models + bridge autostart (2026-09-29)

SUPERSEDED by §29: user rightly rejected the 3-model split. Single
`opendeamon` agent entry; raw chat tiers hidden as fallback-only.

- 7 od-* groups collapsed to 3 public: `opendeamon` (chat cascade),
  `opendeamon-hands` (agent), `opendeamon-vision` (images). Fallback-only
  groups hidden via LiteLLM virtual key restricted to the 3 names
  (fallbacks still traverse hidden groups; enforce off by default).
  Key is self-generated localhost-only; stored in docker runtime config,
  never in files. User confirms final list in UI (their admin account
  exists — no test accounts created).
- Postgres 17 (container opendeamon-pg, data A:\OpenDeamon\gateway\pgdata)
  added SOLELY because LiteLLM virtual keys require DATABASE_URL.
  Bonus: spend logs → quota visibility later. Password self-generated,
  localhost-only, lives in container config.
- Bridge autostart: Startup-folder shortcut (user-level, no elevation;
  Scheduled Task refused without admin) + bootstrap.ps1 guard (starts
  hidden pythonw if :9131 closed).
- New C: footprints (disclosed, systemic): 1 KB Startup .lnk;
  Task Scheduler untouched (needed elevation — skipped).

## 29. Unified entry + freedom-of-pipelines correction (2026-09-29)

- User correction: SOUL/skill texts must DESCRIBE functions, not dictate
  pipelines — core decides when/why. SOUL.md and opencode-worker SKILL.md
  rewritten in descriptive tone ($0 stays project law; detached-GUI stays
  as technical fact, not procedure).
- `opendeamon` is now the single agent entry (bridge deployment first,
  chat tiers as hidden fallback). Bridge accepts images (base64 → tmp +
  vision hint). Restricted key narrowed to [`opendeamon`]; OWUI rewired.
- E2E fan-out through the single entry: chart screenshot in →
  correct data readout (23/17/11/30/15) + chart.html built, one turn.
  The exact user scenario (vision → describe → code → verify) works
  with zero user-side routing.

## 30. OWUI bugfix: titles + detached model (2026-09-29)

- Title mojibake: bridge captured hermes stdout in cp1251. Fixed: UTF-8
  everywhere (env, subprocess, JSON charset), byte-verified clean.
- "Model detached" incident: chat request never reached the demon
  (wrong model selected in UI). Fixed by single-entry design (§29).

## 31. Live progress + aux short-circuit (bridge v2, 2026-09-29)

- Why long: full agent loop (measured 24s for a trivial `ls`: N model
  calls on free-tier queues). Inherent to agent-everything design;
  roomy quotas (1000/day) compensate.
- Why invisible: bridge forwarded only final text. Fixed: live poll of
  Hermes state.db messages → tool calls stream as reasoning deltas
  (e.g. 15s read_file → 18s search_files → heartbeat → answer).
  Verified end-to-end. No fakes: only committed tool rows are shown.
- OWUI auxiliary calls (title/tags/follow-ups) each burned a FULL Hermes
  turn (found live in state.db). Now short-circuited locally in bridge:
  instant, free. Titles in Russian from user line.
- Bugs fixed along the way: double-sent SSE stream; consecutive-dedup
  broken by heartbeat (now global dedup); cp1251 console artifacts
  (display-only, bytes were clean).

## 32. OWUI stack retired (2026-09-29, user decision)

Verdict confirmed: with the single agent entry, OWUI was UI polish only.
Removed: containers opendeamon-owui/litellm/pg + images litellm/postgres
(~2.1 GB reclaimed, 9.65 → 7.53 GB). Kept (rollback-ready): all configs,
volumes (owui data, pgdata), bridge (still running, useful for any
OpenAI-compat client). Untouched: user's own open-webui + searxng
containers (running, incl. port 8080 squatter — now explained).
Interface now: Hermes TUI/CLI direct (`hermes` from bootstrapped shell).

## 33. free-rank plugin: auto-rotate free models by rating (2026-09-29)

- Vehicle: NOT a Hermes YAML-plugin (that system installs from
  catalog/git only) and NOT Hermes cron (needs an always-on gateway).
  Implemented as function pack + Windows Task Scheduler job
  `OpenDeamonFreeRank` (Sundays 04:00, next 04.10.2026) — same effect,
  zero new infra. Skill file describes it; core decides extra runs.
- Signals: live OR :free catalog + models.dev caps (Hermes cache) +
  own weekly micro-probes (~40 free calls: reasoning/format/vision) +
  Aider leaderboard snapshot (best-effort cache).
- Score per role (reasoning/vision/cheap): capability gate × probe ×
  leaderboard bonus. Statuses: ok / congested (429/5xx, not punished) /
  dead (401/403/404, excluded).
- Live findings: thinkingmachines/inkling*:free → 403 for this account
  (would-be winners by caps — excluded); shared-pool 429s are routine.
- Hysteresis (anti-thrash): incumbent kept unless dead or a verified-
  perfect challenger beats a non-healthy incumbent. First --apply
  attempt proved the need (winners flipped between runs); restored
  battle-tested super/gemma, then applied cleanly (super kept,
  delegation cheap set, fallbacks refreshed, doctor clean).
- Owns: ranking.json, hermes config (default/fallbacks/vision/
  delegation), gateway chat-tier order (effective on next gateway up).

## 33. Hermes Desktop UI on A: (2026-09-29, user request)

- Build: `hermes desktop` from bootstrapped shell (npm cache → A:,
  Hermes node v22.23.2 for engines ^22.22; system node 22.13 rejected
  with EBADENGINE). Gotchas hit live: $ErrorActionPreference=Stop
  aborts on npm stderr warnings (use Continue); final install into
  release/ fails across volumes (junction) — built on C: source tree,
  then robocopy /MOVE to A: (~404 MB), C: leftovers removed.
- Result: `A:\OpenDeamon\desktop\Hermes.exe` (Electron 40, unpacked).
  Launched with HERMES_HOME=A:\OpenDeamon\hermes-home (env inherit);
  verified: state.db/auth.json/cache touched at launch second = same
  core, sessions, quota, persona. --cwd A:\OpenDeamon.
- C: footprint of build: node_modules refresh inside pre-existing source
  tree only (deps were already installed); npm cache → A:; no new
  programs on C:.

## 35. delegate-first hook: conductor enforcement (2026-09-29)

- Shell `pre_tool_call` hook (`functions/delegate-first/`, stdlib):
  budget 3 direct exec calls per (session, turn); over-budget calls
  blocked with redirect (parallel delegate_task / opencode worker).
  Reads/checks/web/vision/delegate_task never counted.
- Proven live from state.db trace: 3× terminal → block fired → model
  called delegate_task (obeyed!) → oneshot denied it
  (oneshot_max_children) → model routed around via execute_code →
  bypass closed same day (matcher + tool set extended).
- Limits (honest): one-shot runs (-z, hands bridge) cannot spawn
  subagents — full fan-out only on TUI/Desktop; hook still shapes
  -z behavior toward the worker pattern. Consent via
  hooks_auto_accept (this HOME only) + HERMES_ACCEPT_HOOKS=1.
- Config incident: matcher edit dropped the YAML dash (line 70) —
  Hermes ran on last-good config until fixed; caught by config check.

## 34. User stress test received + deltas applied (2026-09-29)

- `STRESS_TEST_REPORT.md` (user): core PASSED (tools, subagents ×3 with
  output_schema, opencode-worker, vision via dots, memory 8%, $0 kept).
- Vision 404s confirmed by user: gemma-4-31b (no image endpoints) and
  nano-omni (despite cache vision:true) fail on the real tool path.
  Fix: `VISION_DENY` in rank.py (nano-omni excluded from vision roles);
  deployed config already dots → qwen → gemma (user's manual fix, kept
  by hysteresis as incumbent).
- free-rank scoring hardened: dead/congested statuses, verified-first
  replacement, hysteresis kept super+dots across runs. Dry-run verified.
- opencode-worker SKILL: added observed permission behavior
  (external_directory auto-reject outside allowed roots).
- sa-0 note: openrouter.ai/collections lists 24 :free, API returns 16 —
  API list is authoritative for calling (page counts differently).
- Bridge: user notes it retired with OWUI; process still idle-running
  (harmless, useful for any OpenAI-compat client).
