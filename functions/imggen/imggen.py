#!/usr/bin/env python
"""imggen — local text-to-image on the box's own GPU.

Why this exists
---------------
The core shipped a 2-byte "image" for a photo request: it called a free web
API while this machine had a 16 GB RTX 5060 Ti and a stable-diffusion model
already in the HF cache. Web APIs are markedly worse for this class of task
and burn network quota. skill_finder now prints PREFER LOCAL for creative
tasks; this is what that warning points at.

Design
------
- Local first, always. No network, no API key, no quota.
- Default resolution is deliberately modest (768x768) with few steps: this is
  a capability probe, not a print shop. --hd is opt-in.
- Verify is measurable, not vibes: the caller gets real byte size, dimensions
  and pixel variance in the result, so "did it generate an image" is a fact.
  (A web call once produced a 2-byte file and was called a success.)

Models, in preference order (all known-good in the local HF cache):
    runwayml/stable-diffusion-v1-5 -> CompVis/stable-diffusion-v1-4
Anything else works if it is a diffusers-compatible checkpoint.

Usage:
    imggen.py "a cat on a windowsill" [--out PATH] [--steps N] [--size WxH]
             [--seed N] [--hd] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

DEFAULT_SIZE = 768
DEFAULT_STEPS = 20
DEFAULT_MODEL = "auto"

# Preference order. FLUX first: SD 1.5 (2022) produces a plastic look that no
# amount of prompt tuning fixes — proven live 2026-09-30 when the user rejected
# an SD 1.5 cat as "clay, not a cat". FLUX.1 is the real quality step up.
# The official FLUX.1-schnell repo is GATED (401 without a HF token), so an
# ungated fp8 mirror is used instead; it fits the 16 GB card.
MODEL_CHAINS = {
    "auto": (
        "Freepik/flux.1-lite-8B",      # ungated FLUX-lite, full diffusers pipeline
        "runwayml/stable-diffusion-v1-5",   # already cached, fallback
    ),
    "flux": ("Freepik/flux.1-lite-8B",),
    "sd15": ("runwayml/stable-diffusion-v1-5",),
    "sd14": ("CompVis/stable-diffusion-v1-4",),
}
FALLBACKS = ("runwayml/stable-diffusion-v1-5", "CompVis/stable-diffusion-v1-4")
HD_SIZE = 1024


def _env() -> None:
    """Keep every cache on A:, never C:."""
    root = os.environ.get("OPENDEAMON_ROOT", r"A:\OpenDeamon")
    os.environ.setdefault("HF_HOME", os.path.join(root, "cache", "hf"))
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def _pick_size(spec: str | None, hd: bool) -> tuple[int, int]:
    if spec:
        try:
            w, h = (int(x) for x in spec.lower().split("x"))
            return max(64, w), max(64, h)
        except ValueError:
            pass
    side = HD_SIZE if hd else DEFAULT_SIZE
    return side, side


def _load_pipe(repo: str, dtype):
    """Load one model, adapting the loader to its family.

    FLUX is a different pipeline class from SD, so the class is chosen from the
    repo rather than hardcoded — a single try/except around a hardcoded class
    would make FLUX unreachable.
    """
    import torch
    if "flux" in repo.lower():
        from diffusers import FluxPipeline
        return FluxPipeline.from_pretrained(repo, torch_dtype=dtype)
    from diffusers import StableDiffusionPipeline
    return StableDiffusionPipeline.from_pretrained(
        repo, torch_dtype=dtype, safety_checker=None, requires_safety_checker=False)


def generate(
    prompt: str,
    out: str,
    steps: int = DEFAULT_STEPS,
    size: tuple[int, int] = (DEFAULT_SIZE, DEFAULT_SIZE),
    seed: int | None = None,
    negative: str = "blurry, lowres, watermark, text, jpeg artifacts, deformed",
    model: str = DEFAULT_MODEL,
) -> dict:
    _env()
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("no CUDA device: local GPU generation is the whole point")

    import numpy as np

    device = "cuda"
    # FLUX-lite is 8B. In bf16 it is ~16 GB of weights, which does not fit a
    # 16 GB card alongside the text encoder and VAE: a full .to("cuda") crashed
    # the interpreter (0xC0000005) at 50% shard load. bf16 + sequential CPU
    # offload is the configuration that actually survives on this box.
    width, height = size

    chain = list(MODEL_CHAINS.get(model, MODEL_CHAINS["auto"])) + list(FALLBACKS)
    pipe = None
    used_model = None
    errors: list[str] = []
    tried: set[str] = set()
    for repo in chain:
        if repo in tried:
            continue
        tried.add(repo)
        # FLUX wants bf16; SD 1.5 is happier in fp16 on this card.
        repo_dtype = torch.bfloat16 if "flux" in repo.lower() else torch.float16
        try:
            t0 = time.time()
            pipe = _load_pipe(repo, repo_dtype)
            used_model = repo
            errors.append(f"{repo}: loaded in {time.time() - t0:.1f}s")
            break
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{repo}: {type(exc).__name__}: {exc}")
            pipe = None
    if pipe is None:
        raise RuntimeError("no local model loaded:\n" + "\n".join(errors))

    is_flux = "flux" in (used_model or "").lower()
    if is_flux:
        # Keeps weights in RAM, moves one submodule to VRAM per forward pass.
        pipe.enable_model_cpu_offload()
    else:
        pipe = pipe.to(device)
        if hasattr(pipe, "enable_attention_slicing"):
            pipe.enable_attention_slicing()

    generator = None
    if seed is not None:
        generator = torch.Generator(device="cpu").manual_seed(seed)

    kwargs = {"prompt": prompt, "width": width, "height": height,
              "num_inference_steps": steps}
    # FLUX has no negative_prompt, and schnell-lite expects low guidance;
    # passing SD's negative prompt errors or distorts output.
    if not is_flux:
        kwargs["negative_prompt"] = negative
        kwargs["guidance_scale"] = 7.5
    else:
        kwargs["guidance_scale"] = 3.5
        kwargs["max_sequence_length"] = 512
    if generator is not None:
        kwargs["generator"] = generator

    t0 = time.time()
    result = pipe(**kwargs)
    elapsed = time.time() - t0
    image = result.images[0]

    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)

    # Measurable verification.
    #
    # HONEST LIMIT, learned 2026-09-30: these numbers prove the file is a real
    # image, NOT that it looks good. SD 1.5 at 8 steps/512px scored a perfect
    # pass (385 KB, 9769 colours) and the result was visibly plastic clay.
    # A byte/variance floor cannot see aesthetics, and calling that "quality
    # confirmed" was wrong. So the floor is split in two:
    #   hard floor  — measurable, blocks blank/truncated output
    #   honest note — always emitted, reminds the caller that this is a
    #                 capability probe and the prompt/steps matter
    arr = np.asarray(image.convert("RGB"), dtype="float32")
    stats = {
        "bytes": path.stat().st_size,
        "width": image.width,
        "height": image.height,
        # Variance near zero means a blank/solid canvas, not an image.
        "pixel_std": round(float(arr.std()), 2),
        "mean_luma": round(float(arr.mean()), 2),
        "distinct_colors_sampled": int(len(np.unique(
            arr[::4, ::4].reshape(-1, 3), axis=0))),
    }
    hard_floor = {
        "min_bytes": 5000,
        "min_pixel_std": 8.0,
        "min_side": 128,
    }
    passed = (
        stats["bytes"] >= hard_floor["min_bytes"]
        and stats["pixel_std"] >= hard_floor["min_pixel_std"]
        and stats["width"] >= hard_floor["min_side"]
        and stats["height"] >= hard_floor["min_side"]
    )
    aesthetics = "NOT MEASURED (see SKILL.md: use --steps 30 --size 768x768 minimum,"
    aesthetics += " and look at the image before reporting success)"
    return {
        "ok": True,
        "path": str(path),
        "model": used_model,
        "device": device,
        "gpu": torch.cuda.get_device_name(0),
        "steps": steps,
        "size": f"{width}x{height}",
        "seconds": round(elapsed, 1),
        "stats": stats,
        "hard_floor": hard_floor,
        "passes_hard_floor": passed,
        "aesthetics": aesthetics,
        "load_log": errors,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Local GPU text-to-image (no API, no quota).")
    ap.add_argument("prompt")
    ap.add_argument("--out", default=r"A:\OpenDeamon\generated\img.png")
    ap.add_argument("--steps", type=int, default=DEFAULT_STEPS)
    ap.add_argument("--size", default=None, help="WxH, default 768x768")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--hd", action="store_true", help="1024px (slower)")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    choices=list(MODEL_CHAINS) + ["auto"],
                    help="auto tries FLUX first, then SD 1.5 (default)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        info = generate(
            args.prompt, args.out, steps=args.steps,
            size=_pick_size(args.size, args.hd), seed=args.seed,
            model=args.model,
        )
    except Exception as exc:  # noqa: BLE001
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json
              else f"FAILED: {payload['error']}")
        return 1

    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    s = info["stats"]
    print(f"OK   {info['path']}")
    print(f"     {s['width']}x{s['height']}  {s['bytes']} bytes  "
          f"pixel_std={s['pixel_std']}  colors={s['distinct_colors_sampled']}")
    print(f"     {info['gpu']}  {info['seconds']}s  {info['steps']} steps")
    print(f"     model: {info['model']}")
    print(f"     hard floor: {'PASS' if info['passes_hard_floor'] else 'FAIL'}"
          f"   (proves it is a real image, NOT that it looks good)")
    if not info["passes_hard_floor"]:
        print("     blank or truncated output - do NOT report this as an image")
    print("     LOOK AT THE IMAGE before claiming quality. Low steps/resolution")
    print("     give a plastic look even when the hard floor passes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
