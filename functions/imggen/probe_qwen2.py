"""Probe: assemble Qwen-Image-2.1 from a GGUF transformer + BF16 encoder/VAE.

The ComfyUI-GGUF repos ship no model_index.json, so diffusers cannot load them
directly. The working combination on this box:
  transformer : unsloth/Qwen-Image-2.1-GGUF  (qwen-image-2.1-Q4_K_M.gguf)
  encoder/vae : Qwen/Qwen-Image-2.1           (BF16, ~5 GB)
Disposal probe: reports what worked, writes nothing.
"""
import sys
import traceback

import torch

GGUF = "unsloth/Qwen-Image-2.1-GGUF"
BASE = "Qwen/Qwen-Image-2.1"


def main() -> int:
    try:
        from diffusers import QwenImagePipeline, GGUFQuantizationConfig
        from transformers import Qwen2_5_VLForConditionalGeneration, AutoProcessor

        print("loading GGUF transformer...")
        quant = GGUFQuantizationConfig(compute_dtype=torch.bfloat16)
        transformer = QwenImagePipeline.transformer_load(
            GGUF,
            quantization_config=quant,
            torch_dtype=torch.bfloat16,
            variant="Q4_K_M",
        )
        print("  transformer ok:", type(transformer).__name__)

        pipe = QwenImagePipeline.from_pretrained(
            BASE,
            transformer=None,
            torch_dtype=torch.bfloat16,
            quantize=8,
        )
        pipe.transformer = transformer
        pipe.to("cuda")
        print("PIPELINE ASSEMBLED:", type(pipe).__name__)
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
