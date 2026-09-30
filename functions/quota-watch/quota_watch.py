#!/usr/bin/env python
"""quota_watch — keep OpenRouter spend at zero, mechanically.

Why a hook
----------
The project runs on a $1 spend limit with a free-tier daily bucket. That was
enforced by honesty alone, which is not enforcement. This pre_tool_call hook
reads the real credit endpoint and, above thresholds, does two things:

  * warns once per threshold by injecting the numbers into context, so the
    core can switch to the opencode-free ladder before it burns the bucket;
  * hard-stops OpenRouter work at the limit and tells the model to use the
    local worker instead.

Reads:  $OPENROUTER_API_KEY / ~/.openrouter_key  (never logged)
Writes: $HERMES_HOME/cache/quota-watch.json  (state + warning latch)

Fail-open by design: a network error must never block the core. If the
quota cannot be read, the call is allowed and the reason is recorded.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://openrouter.ai/api/v1/credits"
CACHE_TTL = 300          # seconds; one turn must not cost four credit calls
WARN_RATIO = 0.80        # of the daily free bucket
STOP_USED = 950          # absolute daily free calls: hard stop before 1000
STOP_SPEND_USD = 1.00    # the spend limit is $1; refuse to approach it


def home() -> str:
    return os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))


def state_path() -> str:
    p = os.path.join(home(), "cache", "quota-watch.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def api_key() -> str | None:
    for name in ("OPENROUTER_API_KEY", "OPENROUTER_KEY"):
        v = os.environ.get(name)
        if v:
            return v.strip()
    path = os.path.join(home(), ".openrouter_key")
    try:
        with open(path, encoding="utf-8") as f:
            v = f.read().strip()
            return v or None
    except OSError:
        return None


def load_state() -> dict:
    try:
        with open(state_path(), encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    try:
        with open(state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


def fetch_credits(key: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(
        API, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def measure(state: dict, key: str | None) -> dict:
    """Best-effort quota reading. Never raises."""
    if not key:
        return {"known": False, "reason": "no key"}
    now = time.time()
    cached = state.get("credits") or {}
    if cached and now - float(cached.get("ts", 0)) < CACHE_TTL:
        return {"known": True, **cached, "cached": True}

    try:
        data = fetch_credits(key)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
        return {"known": False, "reason": f"{type(exc).__name__}: {exc}"}

    d = data.get("data") or {}
    usage = d.get("usage") or {}
    limits = d.get("limit") or {}
    daily = (usage.get("limit") or {}).get("daily_free_models") or {}
    out = {
        "known": True,
        "ts": now,
        "total_usage": usage.get("total_usage"),
        "daily_usage": usage.get("daily_usage"),
        "label": d.get("label"),
        "limit_usd": limits.get("limit"),
        "limit_remaining": limits.get("remaining"),
        "free_used": daily.get("used"),
        "free_limit": daily.get("limit"),
        "free_remaining": daily.get("remaining"),
    }
    state["credits"] = out
    return out


def verdict(m: dict) -> tuple[str, str]:
    """(action, message). action: allow | warn | block."""
    if not m.get("known"):
        # Fail-open: never block work because a status endpoint is down.
        return "allow", ""

    used = m.get("free_used")
    limit = m.get("free_limit") or 1000
    spend = m.get("total_usage")
    spend_limit = m.get("limit_usd") or STOP_SPEND_USD

    # Money first: the free bucket is recoverable tomorrow, $1 is not.
    if spend is not None and spend_limit and spend >= spend_limit * 0.9:
        return "block", (
            f"QUOTA STOP: OpenRouter spend ${spend} of ${spend_limit} limit (>=90%). "
            "Do NOT make OpenRouter calls. Use the local opencode ladder "
            "(opencode run -m opencode/space-bunny-free ...) for coding, and local "
            "functions (imggen, whisper, skill-finder, SearXNG) for everything else."
        )

    if used is None:
        return "allow", ""
    if used >= STOP_USED:
        return "block", (
            f"QUOTA STOP: OpenRouter free tier {used}/{limit} calls used. "
            "Switch to the opencode-free worker ladder or local functions. "
            "The bucket refills daily; spending it now buys nothing."
        )

    if limit and used >= limit * WARN_RATIO:
        remaining = limit - used
        return "warn", (
            f"QUOTA WARNING: {used}/{limit} OpenRouter free calls used "
            f"({remaining} left, ~{remaining * WARN_RATIO / 100:.0f} safe budget). "
            "Prefer the opencode-free ladder for coding and local functions for "
            "everything else. Note MoA costs ~3 calls per turn."
        )
    return "allow", ""


def main() -> None:
    try:
        json.load(sys.stdin)
    except Exception:
        print(json.dumps({}))
        return
    state = load_state()
    m = measure(state, api_key())
    save_state(state)
    action, message = verdict(m)
    if action == "allow":
        print(json.dumps({}))
    elif action == "warn":
        # Latch per threshold so the warning rides along instead of spamming.
        latch = "warned_%d" % int((m.get("free_limit") or 1000) * WARN_RATIO)
        if state.get(latch):
            print(json.dumps({}))
            return
        state[latch] = True
        save_state(state)
        print(json.dumps({"action": "block", "message": message}))
    else:
        print(json.dumps({"action": "block", "message": message}))


if __name__ == "__main__":
    main()
