#!/usr/bin/env python
"""comfy_gen — local image generation via ComfyUI + GGUF (the good path).

Why ComfyUI and not diffusers directly
--------------------------------------
diffusers cannot load a GGUF checkpoint through a pipeline ("Loading GGUF
checkpoints via Pipelines is currently not supported", docs/quantization/gguf.md).
diffusers' own FLUX-lite load crashed this box's interpreter (0xC0000005) while
reading 16 GB of bf16 shards into a 16 GB card. ComfyUI's GGUF nodes keep
quantized weights in RAM and stream layers per forward pass — which is exactly
what 16 GB needs.

Model on this box: Qwen-Image-2.1 (Uncensored) GGUF Q5_K_M, the top-downloaded
text-to-image model on HF trending at 2026-09-30. SD 1.5 (2022) was rejected by
the user as "plastic clay", so the ladder is Qwen first, FLUX/SD only as fallback.

Usage:
    comfy_gen.py "prompt" [--out PATH] [--steps N] [--size WxH] [--seed N] [--json]

Start the server first:
    A:\\ComfyUI\\.venv\\Scripts\\python.exe A:\\ComfyUI\\main.py --listen 127.0.0.1 --port 8188
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
COMFY_ROOT = Path(os.environ.get("COMFYUI_PATH", r"A:\ComfyUI"))
DEFAULT_TE_DIR = COMFY_ROOT / "models" / "text_encoders"
DEFAULT_VAE_DIR = COMFY_ROOT / "models" / "vae"
Q_TE = "qwen3vl_8b_int8_convrot.safetensors"
Q_VAE = "qwen_image_2.1_vae_bf16.safetensors"
# ComfyUI reads diffusion models from several legacy dirs; the current one is
# diffusion_models, and hardcoding a single path silently found nothing.
MODEL_DIRS = [COMFY_ROOT / "models" / "diffusion_models",
              COMFY_ROOT / "models" / "unet"]
PREFER_EXT = (".safetensors", ".gguf")   # native formats first


def find_model(dirs=None) -> str | None:
    for d in (dirs or MODEL_DIRS):
        if not d.is_dir():
            continue
        files = [f for f in d.iterdir() if f.is_file()]
        for ext in PREFER_EXT:
            hits = sorted(f for f in files if f.suffix == ext)
            if hits:
                return hits[0].name
    return None


def build_workflow(model_name: str, prompt: str, steps: int,
                   width: int, height: int, seed: int,
                   te_name: str = Q_TE) -> dict:
    """Qwen-Image 2.1 text-to-image graph for ComfyUI.

    Uses the NATIVE UNETLoader, not ComfyUI-GGUF's UnetLoaderGGUF: ComfyUI
    itself supports the `qwen_image21` architecture (comfy/supported_models.py),
    while the GGUF node's arch whitelist only knows `qwen_image` and raises
    "Unexpected architecture type in GGUF file: 'qwen_image21'".
    """
    return {
        "4": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": model_name, "weight_dtype": "default"},
        },
        "5": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": te_name,
                # CLIPLoader here has no `device` input (only clip_name/type);
                # passing one made the node invisible to validation.
                "type": "qwen_image",
            },
        },
        "6": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": Q_VAE},
        },
        "3": {
            "class_type": "EmptySD3LatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["5", 0]},
        },
        "8": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": 4.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
                "model": ["4", 0],
                "positive": ["7", 0],
                "negative": ["7", 0],
                "latent_image": ["3", 0],
            },
        },
        "9": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["8", 0], "vae": ["6", 0]},
        },
        "10": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "opendeamon",
                       "images": ["9", 0]},
        },
    }


def api(path: str, payload: dict | None = None, timeout: int = 30) -> dict:
    url = f"{COMFY_URL}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def wait_for_server(timeout_s: int = 30) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            api("/system_stats", timeout=5)
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(2)
    return False


def server_alive() -> bool:
    try:
        api("/system_stats", timeout=5)
        return True
    except (urllib.error.URLError, OSError):
        return False


def generate(prompt: str, out: str, steps: int = 20,
             size: tuple[int, int] = (1024, 1024), seed: int | None = None,
             timeout_s: int = 900) -> dict:
    model = find_model()
    if not model:
        raise RuntimeError(f"no GGUF model under {MODEL_DIRS}")
    if not (DEFAULT_TE_DIR / Q_TE).is_file():
        raise RuntimeError(f"text encoder missing: {DEFAULT_TE_DIR / Q_TE}")
    if not (DEFAULT_VAE_DIR / Q_VAE).is_file():
        raise RuntimeError(f"VAE missing: {DEFAULT_VAE_DIR / Q_VAE}")
    if not server_alive():
        raise RuntimeError(
            f"ComfyUI not responding on {COMFY_URL}. Start it:\n"
            f'  "{COMFY_ROOT}\\.venv\\Scripts\\python.exe" "{COMFY_ROOT}\\main.py" '
            f"--listen 127.0.0.1 --port 8188")

    seed = seed if seed is not None else int(time.time() * 1000) % (2**32)
    wf = build_workflow(model, prompt, steps, size[0], size[1], seed)
    client_id = str(uuid.uuid4())
    res = api("/prompt", {"prompt": wf, "client_id": client_id})
    # ComfyUI keys history by prompt_id, NOT client_id. Polling
    # /history/<client_id> returns {} forever and the run looks like a timeout
    # even though the image was written.
    prompt_id = res.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"no prompt_id in response: {res}")

    deadline = time.time() + timeout_s
    images: list[str] = []
    while time.time() < deadline:
        hist = api(f"/history/{prompt_id}", timeout=15)
        entry = hist.get(prompt_id)
        if entry:
            for node_out in (entry.get("outputs") or {}).values():
                for img in node_out.get("images", []):
                    fname = img.get("filename")
                    if fname:
                        images.append((fname, img.get("subfolder") or "",
                                       img.get("type") or "output"))
            if images:
                break
        time.sleep(2)
    if not images:
        raise TimeoutError(f"no image after {timeout_s}s (prompt_id={prompt_id})")

    fname, subfolder, ftype = images[0]
    q = f"/view?filename={fname}&subfolder={subfolder}&type={ftype}"
    with urllib.request.urlopen(f"{COMFY_URL}{q}", timeout=60) as r:
        blob = r.read()
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)

    return {
        "ok": True,
        "path": str(path),
        "bytes": len(blob),
        "model": model,
        "steps": steps,
        "seed": seed,
        "size": f"{size[0]}x{size[1]}",
        "backend": "comfyui",
        "prompt_id": prompt_id,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Local image generation (ComfyUI + GGUF).")
    ap.add_argument("prompt")
    ap.add_argument("--out", default=r"A:\OpenDeamon\generated\img.png")
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--size", default=None, help="WxH, default 1024x1024")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    size = (1024, 1024)
    if args.size:
        try:
            w, h = (int(x) for x in args.size.lower().split("x"))
            size = (w, h)
        except ValueError:
            pass

    try:
        info = generate(args.prompt, args.out, steps=args.steps,
                        size=size, seed=args.seed)
    except Exception as exc:  # noqa: BLE001
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json
              else f"FAILED: {payload['error']}")
        return 1

    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    print(f"OK   {info['path']}")
    print(f"     {info['bytes']} bytes  {info['size']}  {info['model']}")
    print("     LOOK AT THE IMAGE before claiming quality; a byte count is not quality.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
