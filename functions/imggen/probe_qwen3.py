"""Probe: assemble Qwen-Image-2.1 via from_single_file (GGUF transformer).

Docs (diffusers gguf.md): "Loading GGUF checkpoints via Pipelines is currently
not supported" — the transformer must be loaded as a Model class with
from_single_file, then handed to the pipeline.

  transformer : unsloth/Qwen-Image-2.1-GGUF  (qwen-image-2.1-Q4_K_M.gguf)
  encoder/vae : Qwen/Qwen-Image-2.1           (BF16)
"""
import glob
import os
import sys
import traceback

import torch

BASE = "Qwen/Qwen-Image-2.1"
GGUF_REPO_DIR = r"A:\OpenDeamon\cache\hf\hub\models--unsloth--Qwen-Image-2.1-GGUF"


def find_gguf() -> str:
    hits = glob.glob(os.path.join(GGUF_REPO_DIR, "snapshots", "*", "*.gguf"))
    return hits[0] if hits else ""


def main() -> int:
    from diffusers import QwenImagePipeline, QwenImageTransformer2DModel, GGUFQuantizationConfig

    gguf = find_gguf()
    if not gguf:
        print("GGUF not found under", GGUF_REPO_DIR)
        return 1
    print("gguf:", os.path.basename(gguf))

    print("loading transformer from_single_file...")
    # `config` accepts a LOCAL DIRECTORY of component configs. Passing a repo id
    # makes diffusers resolve against the wrong default hub repo (it went looking
    # for stable-diffusion-v1-5). Point it at the local snapshot instead.
    snap = glob.glob(os.path.join(
        r"A:\OpenDeamon\cache\hf\hub\models--Qwen--Qwen-Image-2.1", "snapshots", "*"))
    cfg_dir = snap[0] if snap else None
    print("  local snapshot:", cfg_dir)
    try:
        transformer = QwenImageTransformer2DModel.from_single_file(
            gguf,
            quantization_config=GGUFQuantizationConfig(compute_dtype=torch.bfloat16),
            config=cfg_dir,
            subfolder="transformer",
            torch_dtype=torch.bfloat16,
        )
        print("  transformer ok")
    except Exception:
        traceback.print_exc()
        return 1

    print("loading pipeline (encoder + vae)...")
    try:
        pipe = QwenImagePipeline.from_pretrained(BASE, transformer=None, torch_dtype=torch.bfloat16)
        pipe.transformer = transformer.to("cuda")
        pipe.enable_model_cpu_offload()
        print("PIPELINE ASSEMBLED")
    except Exception:
        traceback.print_exc()
        return 1

    prompt = ("a grey cat sitting on a sunlit windowsill, photorealistic photograph, "
              "shallow depth of field, 50mm lens, natural window light, detailed fur")
    print("generating one test image (this proves the whole path)...")
    img = pipe(prompt=prompt, num_inference_steps=20, width=1024, height=1024).images[0]
    out = r"A:\OpenDeamon\_setup_tmp\qwen_test.png"
    img.save(out)
    print("SAVED", out, os.path.getsize(out), "bytes", img.width, "x", img.height)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
