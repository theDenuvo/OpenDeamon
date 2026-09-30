---
name: capability-matrix
description: What each capability is already solved with — local first. Read BEFORE inventing a solution or calling a web service.
---

# Capability Matrix

Read this before you build anything or call any web service for a capability
listed here. Full table: `functions/capability-matrix/MATRIX.md`.

## The rule

```
locally installed  →  Skill Hub  →  GitHub  →  web service  →  build it
```

Hard exception: **creative class** (image / video / tts / ocr / diffusion /
upscale / 3d) **with a GPU on this machine → local wins over any web service.**
`skill_finder.py` prints `!! PREFER LOCAL` when this applies. Do not route
around it.

## What you already have (do not rebuild)

- **Images** → `functions/imggen` — local SD 1.5 on the RTX 5060 Ti,
  1.5 s at 512px, zero quota. Web image APIs are markedly worse and one of
  them returned a 2-byte file that was counted as success.
- **Coding** → the opencode CLI worker (a hook blocks you from writing code
  yourself).
- **Transcription** → `functions/whisper` (faster-whisper-large-v3, local).
- **Web search** → local SearXNG on Docker.
- **PDF / xlsx / docx / pptx / diagrams / browser** → installed builtin skills.

## When a capability is NOT here

1. `python A:/OpenDeamon/functions/skill-finder/skill_finder.py "<task>"`
   — searches 101k hub skills offline, costs no context.
2. If a hit exists: `hermes skills inspect <identifier>` then
   `hermes skills install <identifier>`.
3. Only if nothing fits: build it via the opencode worker.

## Honest limits

- `autonomous_tools` declares an interface but has no implementation — do
  not rely on it as a working function.
- No local video/audio-generation model is deployed yet; that row points at
  the hub on purpose.
