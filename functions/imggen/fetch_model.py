"""Download a HF snapshot to the A: cache. Stdlib-friendly, resumable.

Xet-backed repos fail on multi-GB files with
"File reconstruction error: Internal Writer Error: Background writer
channel closed" (observed on Kijai/flux-fp8, 12 GB). Set
HF_HUB_DISABLE_XET=1 so large files go over plain HTTP, which is slower but
does not die at ~600 MB.
"""
import os
import sys

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from huggingface_hub import snapshot_download

IGNORE = ["*.pt", "*.onnx", "*flax*", "*openvino*", "*.msgpack", "*.h5"]


def main() -> int:
    repo = sys.argv[1]
    allow = sys.argv[2:] or None
    kw = {"ignore_patterns": IGNORE} if not allow else {"allow_patterns": allow}
    path = snapshot_download(repo, max_workers=2, **kw)
    print("DONE", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
