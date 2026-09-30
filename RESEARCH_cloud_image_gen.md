# Cloud image generation — research, 2026-09-30

Question: what cloud image generation is worth adding to OpenDeamon, given the
project runs at **$0** and now has a working local Qwen-Image-2.1 fp8 on a
16 GB RTX 5060 Ti (~55–68 s per 1024px/20-step image, free, no quota)?

Hard filter applied: an ongoing free tier only. One-time signup credits are
listed separately and rejected — they are not a foundation.

## Verdict

**Cloudflare Workers AI is the only cloud option that qualifies.** It has a
real daily free allocation, no card required, and it fails closed instead of
billing on overrun — which matters for a project whose rule is $0.

The headline number: **FLUX.2 [dev] on Workers AI is ~16 images/day free**,
and FLUX.2 [dev] is a stronger image model than our local Qwen-Image-2.1 on
prompt adherence, hands, text rendering and multi-reference consistency.

Local still wins by default (no latency, no quota, no network). The cloud
slot earns its place for: preview iteration, throughput when the GPU is busy,
multi-reference consistency, and anything we cannot run locally (video,
upscaling).

## Verified free tiers (checked against primary sources 2026-09-30)

| Provider | Image models | Free quota | Card | Sustainable | Source |
|---|---|---|---|---|---|
| **Cloudflare Workers AI** | FLUX.2 [dev], FLUX.2 [klein] 4B/9B, FLUX.1 [schnell], Leonardo Phoenix 1.0 / Lucid Origin, DreamShaper 8 LCM | **10 000 neurons/day**, resets 00:00 UTC | No | **Yes** | [pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/) |
| Google AI Studio | — | — | — | **No — removed** | see below |
| Pollinations.ai | FLUX, Turbo | anonymous ~1 req/15 s, watermark | No | No — needs key now, no SLA | apiframe, bagrounds |
| fal.ai / Together.ai | — | none | — | No | bagrounds |
| HuggingFace | 300+ incl. FLUX | $0.10/month credits ≈ 83 images | No | Barely | apiframe |
| NVIDIA NIM | SDXL, ControlNet | 1 000 credits (one-off) | No | No | awesome-free-ai-apis |

### Negative finding worth keeping: Google dropped free image generation

Google AI Studio's free tier **no longer supports image generation** through
either the Gemini API (`generateContent` with image output) or the Imagen API
(`generateImages`) — both now require a paid plan. Multiple sources describe
pipelines built on it going silent. This is the most commonly recommended
"free image API" and it is simply gone. Do not propose it.

(Secondary source — a third-party engineering writeup. Worth re-checking
against Google's own pricing page before relying on it, but it is consistent
with the model-card guidance below that FLUX.2 was launched "in the face of"
the rise of closed models.)

## Neuron costs (published) and what they buy

Workers AI bills in "neurons", a normalised GPU-compute unit. Free plan:
10 000 neurons/day. Published rates:

| Model | Rate | ~Cost/image @1024², 4 steps | Free images/day |
|---|---|---|---|
| `@cf/black-forest-labs/flux-2-klein-4b` | 5.37 neurons/input tile + **26.05 per output tile** | ~104 | **~96** |
| `@cf/black-forest-labs/flux-1-schnell` | 4.80 per 512² tile + **9.60 per step** | ~58 | **~173** |
| `@cf/black-forest-labs/flux-2-klein-9b` | **1363.64 per first MP** | ~1364 | **~7** |
| `@cf/black-forest-labs/flux-2-dev` | 18.75 per input tile/step + **37.50 per output tile/step** | ~600 | **~16** |
| `@cf/leonardo/phoenix-1.0` | 530 per 512² tile + 10 per step | ~2160 | **~4** |
| `@cf/leonardo/lucid-origin` | 636 per 512² tile + 12 per step | ~2592 | **~3** |

These are **computed from published rates, not measured.** Assume ±30 % until
we run them.

Two structural notes:
- FLUX.2 billing scales with **steps × tiles**, so 1024² at 20 steps costs ~5×
  a 4-step draft. Draft cheap, finalise once.
- Output is capped at **4 megapixels**, and FLUX.2 accepts up to 4 reference
  inputs at 512² each (multipart form-data, not plain JSON).

## Cost control

- **Spend cap is structural**: Workers Free *fails with an error* past 10 000
  neurons rather than billing. This is why it suits a $0 project — no
  surprise invoices, unlike the credit-card-required trial tiers.
- Draft on `flux-2-klein-4b` (~104/image), finalise on `flux-2-dev`
  (~600/image). A 4-draft + 1-final loop costs ~1 016 neurons — a sixth of
  the daily pool for a properly iterated result.
- Keep our existing `quota-watch` hook idea in mind: Workers AI exposes
  remaining neurons in its dashboard, so the same warn/stop pattern applies.

## What we would need to use it

Free Cloudflare account, then `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID`
secrets. REST call is a single POST, no SDK:

```
POST https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/@cf/black-forest-labs/flux-2-dev
Authorization: Bearer {token}
```

Response is base64 JPEG. Nothing needs to be installed — this drops into our
existing skill structure exactly like `imggen`'s other backends.

## Recommendation

1. **Keep local Qwen-Image-2.1 as the default.** It is free, private and
   already works; ~68 s is acceptable.
2. **Add Cloudflare as tier 2** when: the GPU is busy, we need >1 image at
   once, or the task needs multi-reference consistency / better text
   rendering. ~16 FLUX.2 [dev] images/day is a real budget, not a demo.
3. **Do not add** Google (gone), Pollinations (we already shipped a 2-byte
   "image" from it), fal.ai or Together (no free tier).
4. **Re-verify before wiring**, since all numbers here are computed from
   published rates and secondary sources, not measured. A single live call
   settles it.

## Gaps in this research

- No live call was made: no Cloudflare account exists on this box yet, so the
  neuron costs are arithmetic, not observation.
- Image *quality* comparison against local Qwen-Image-2.1 is based on model
  documentation, not a side-by-side on our own prompts. FLUX.2 is claimed to
  beat Qwen-Image on text/hands/consistency; we have not measured that.
- Video and upscaling models were not surveyed — our gap list lists both, and
  Cloudflare's catalog may cover them.
- Rate limits (requests/minute) were not researched; only the daily neuron
  budget. Concurrency may bite before the budget does.
