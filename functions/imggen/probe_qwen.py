"""Probe: can diffusers load the Qwen-Image-2.1 GGUF checkpoint here?

Fast, disposable probe: reports what resolved and why it failed, without
writing anything permanent. Used to decide whether imggen grows a Qwen path.
"""
import sys
import traceback

import torch


def main() -> int:
    repo = sys.argv[1] if len(sys.argv) > 1 else "vantagewithai/Qwen-Image-2.1-ComfyUI-GGUF"
    print("torch", torch.__version__, "cuda", torch.cuda.is_available())
    try:
        from diffusers import DiffusionPipeline
        pipe = DiffusionPipeline.from_pretrained(
            repo, dtype=torch.bfloat16, device_map="cuda")
        print("LOADED", type(pipe).__name__)
        print("components:", [c for c in dir(pipe) if c.endswith("Pipeline")][:3])
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
