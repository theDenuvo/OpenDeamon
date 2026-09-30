#!/usr/bin/env python
"""cloud_gen — Cloudflare Workers AI image backend (free tier).

Why this backend exists
-----------------------
Workers AI is the only cloud image provider with a genuine ONGOING free
tier: 10 000 neurons/day, no card required, resets 00:00 UTC, and — the part
that matters for a $0 project — it FAILS WITH AN ERROR past the limit
instead of billing. Verified against Cloudflare's own pricing page
2026-09-30; see RESEARCH_cloud_image_gen.md.

Model choice is budget-driven, not quality-driven: FLUX.2 [dev] is the
strongest model but ~600 neurons/image, which is ~16 images/day. The
workflow that actually fits a daily budget is draft cheap, finalise once:

    flux-2-klein-4b  ~104 neurons/image  ->  ~96 images/day
    flux-1-schnell   ~58  neurons/image  -> ~173 images/day
    flux-2-dev       ~600 neurons/image  ->  ~16 images/day

Credentials: CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID from
A:\\OpenDeamon\\secrets\\cloudflare.env (never in git).

Usage:
    cloud_gen.py "prompt" [--model draft|final|auto] [--steps N]
                 [--size WxH] [--out PATH] [--seed N] [--budget N] [--json]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

API = "https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{model}"
SECRETS = Path(os.environ.get("OPENDEAMON_ROOT", r"A:\OpenDeamon")) / "secrets" / "cloudflare.env"

# Published neuron rates (Cloudflare pricing page, 2026-09-30). COMPUTED, not
# measured — treat as +/-30% until a live call confirms them.
MODELS = {
    "flux-2-klein-4b": {"in_tile": 5.37, "out_tile": 26.05, "per_step": False},
    "flux-1-schnell":   {"in_tile": 4.80, "out_tile": 4.80, "per_step": True, "step": 9.60},
    "flux-2-klein-9b":  {"flat_per_mp": 1363.64},
    "flux-2-dev":       {"in_tile": 18.75, "out_tile": 37.50, "per_step": True, "step": 37.50},
}
TIERS = {"draft": "flux-2-klein-4b", "final": "flux-2-dev", "fast": "flux-1-schnell"}
DAILY_FREE = 10_000
STATE = Path(os.environ.get("HERMES_HOME", r"A:\OpenDeamon\hermes-home")) / "cache" / "cloud-nerons.json"


def load_env(path: Path = SECRETS) -> dict:
    env = {}
    for key in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"):
        v = os.environ.get(key)
        if v:
            env[key] = v
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    except OSError:
        pass
    return env


def tiles(size: tuple[int, int]) -> int:
    return max(1, -(-size[0] // 512) * max(1, -(-size[1] // 512)))


def estimate_neurons(model: str, size: tuple[int, int], steps: int, refs: int = 0) -> int:
    spec = MODELS.get(model)
    if not spec:
        return 0
    t = tiles(size)
    if "flat_per_mp" in spec:
        return int(spec["flat_per_mp"] * max(1.0, (size[0] * size[1]) / 1048576))
    if spec.get("per_step"):
        return int((spec["out_tile"] * t + spec["step"] * t) * steps
                   + spec["in_tile"] * tiles(size) * refs * steps)
    return int(spec["out_tile"] * t + spec["in_tile"] * tiles(size) * refs)


def load_state() -> dict:
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return d
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_state(d: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass


def budget_left() -> int | None:
    """Neurons left today, tracked locally. Resets with the UTC day."""
    st = load_state()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    used = st.get(today, {}).get("neurons", 0)
    return max(0, DAILY_FREE - used)


def record_spend(neurons: int) -> None:
    st = load_state()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    day = st.setdefault(today, {"neurons": 0, "calls": 0})
    day["neurons"] = day.get("neurons", 0) + int(neurons)
    day["calls"] = day.get("calls", 0) + 1
    save_state(st)


def generate(prompt: str, out: str, model: str, steps: int = 4,
             size: tuple[int, int] = (1024, 1024), seed: int | None = None,
             budget: int | None = None, timeout: int = 180) -> dict:
    env = load_env()
    token = env.get("CLOUDFLARE_API_TOKEN")
    acct = env.get("CLOUDFLARE_ACCOUNT_ID")
    missing = [k for k, v in (("CLOUDFLARE_API_TOKEN", token),
                              ("CLOUDFLARE_ACCOUNT_ID", acct)) if not v]
    if missing:
        raise RuntimeError(
            "missing credentials: " + ", ".join(missing) +
            "\nFree Cloudflare account: dash.cloudflare.com -> Workers AI -> API token.\n"
            "Put them in " + str(SECRETS) + " (gitignored):\n"
            "  CLOUDFLARE_API_TOKEN=...\n  CLOUDFLARE_ACCOUNT_ID=...")

    est = estimate_neurons(model, size, steps)
    left = budget_left()
    if budget is not None and left is not None and est > budget:
        raise RuntimeError(
            f"{model} at {size[0]}x{size[1]}/{steps} steps needs ~{est} neurons but "
            f"only {left} remain today (free pool {DAILY_FREE}/day). "
            f"Use a draft tier or fewer steps. Workers Free fails closed "
            f"past the limit — it will not bill, but the call will error.")

    url = API.format(acct=acct, model=model)
    payload = {"prompt": prompt, "num_steps": steps,
               "width": size[0], "height": size[1]}
    if seed is not None:
        payload["seed"] = seed
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from None
    elapsed = time.time() - t0

    result = body.get("result") or {}
    raw = result.get("image")
    if not raw:
        raise RuntimeError(f"no image in response: {json.dumps(body)[:300]}")
    blob = base64.b64decode(raw)

    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)

    record_spend(est)
    return {
        "ok": True,
        "path": str(path),
        "bytes": len(blob),
        "backend": "cloudflare",
        "model": model,
        "steps": steps,
        "size": f"{size[0]}x{size[1]}",
        "seconds": round(elapsed, 1),
        "neurons_estimated": est,
        "neurons_left_estimate": budget_left(),
        "estimated": True,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Cloudflare Workers AI image generation (free tier).")
    ap.add_argument("prompt")
    ap.add_argument("--model", default="auto", help="draft|final|fast|<model id>, or auto")
    ap.add_argument("--steps", type=int, default=4)
    ap.add_argument("--size", default="1024x1024")
    ap.add_argument("--out", default=r"A:\OpenDeamon\generated\cloud.png")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--budget", type=int, default=None,
                    help="refuse if the estimated cost exceeds this many neurons")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    model = args.model
    if model == "auto":
        # Final tier only when the pool can comfortably absorb it, else draft.
        left = budget_left() or 0
        model = TIERS["final"] if left >= 1500 else TIERS["draft"]

    try:
        w, h = (int(x) for x in args.size.lower().split("x"))
    except ValueError:
        w = h = 1024

    try:
        info = generate(args.prompt, args.out, model, steps=args.steps,
                        size=(w, h), seed=args.seed, budget=args.budget)
    except Exception as exc:  # noqa: BLE001
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json
              else f"FAILED: {payload['error']}")
        return 1

    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    print(f"OK   {info['path']}")
    print(f"     {info['bytes']} bytes  {info['size']}  {info['model']}  {info['seconds']}s")
    print(f"     neurons ~{info['neurons_estimated']} (estimate), ~{info['neurons_left_estimate']} left today")
    print("     LOOK AT THE IMAGE before claiming quality.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
