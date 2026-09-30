#!/usr/bin/env python
"""quota_watch — keep OpenRouter spend at zero, mechanically.

Why a hook
----------
The project runs on a $1 spend limit with a free-tier daily bucket. That was
enforced by honesty alone, which is not enforcement. This pre_tool_call hook
reads the REAL key endpoint and, above thresholds, either warns the core (so
it can switch to the opencode-free ladder) or blocks outright.

Which endpoint, and why
-----------------------
``/api/v1/credits`` returns only credits and total_usage:
    {"data": {"total_credits": 10, "total_usage": 0}}
The daily free bucket is NOT there. Parsing that shape yielded all-None
fields, and verdict() treated None as "allow" — so the hook silently
permitted everything. That is the worst possible failure for a guard.

``/api/v1/key`` carries both halves of the state we need. Observed live
2026-09-30:

    {
      "limit": 1, "limit_reset": "daily", "limit_remaining": 1,
      "usage": 0, "usage_daily": 0, "usage_weekly": 0, "usage_monthly": 0,
      "free_model_daily_requests": {"used": 1, "limit": 1000, "remaining": 999},
      "is_free_tier": false
    }

Three-state contract (this is the point of the rewrite)
-------------------------------------------------------
    known      - we parsed the numbers -> apply thresholds
    drift      - we got a response but the fields are not where we expect.
                 Enforcement is BLIND. Do NOT silently allow: surface it,
                 because a schema change means the guard stopped guarding.
    offline    - endpoint unreachable. Fail OPEN: a status page outage must
                 not block real work.

Fail-open applies only to `offline`. A silent allow on `drift` is what this
rewrite exists to prevent.

Reads:  $OPENROUTER_API_KEY / $HERMES_HOME/.openrouter_key  (never logged)
Writes: $HERMES_HOME/cache/quota-watch.json  (credits cache + warn latch)
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

API_KEY_INFO = "https://openrouter.ai/api/v1/key"
CACHE_TTL = 300          # seconds; one turn must not cost four status calls
WARN_RATIO = 0.80        # of the daily free bucket
STOP_USED = 950          # absolute daily free calls: hard stop before 1000
STOP_SPEND_RATIO = 0.90  # of the USD limit

# Thresholds, overridable for tests without touching the module globals.
WARN_USED = int(1000 * WARN_RATIO)


def home() -> str:
    return os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))


def state_path() -> str:
    p = os.path.join(home(), "cache", "quota-watch.json")
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
    except OSError:
        pass
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


def _num(value):
    """Coerce to float, or None. OpenRouter sends numbers; a schema change may
    send strings or nulls, and neither must crash the guard."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def fetch_key_info(key: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(
        API_KEY_INFO,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def parse_key_info(data: dict) -> dict:
    """Extract the state we enforce on. Returns known=False when the shape is
    not what we expect — the caller must not treat that as 'healthy'."""
    d = data.get("data") if isinstance(data, dict) else None
    if not isinstance(d, dict):
        return {"known": False, "reason": "no data object"}

    spend = _num(d.get("usage"))
    spend_limit = _num(d.get("limit"))
    if spend_limit is None:
        spend_limit = STOP_SPEND_RATIO and 1.0  # documented default

    free = d.get("free_model_daily_requests")
    free_used = free_limit = free_remaining = None
    if isinstance(free, dict):
        free_used = _num(free.get("used"))
        free_limit = _num(free.get("limit"))
        free_remaining = _num(free.get("remaining"))

    out = {
        "spend_usd": spend,
        "spend_limit_usd": spend_limit,
        "spend_daily": _num(d.get("usage_daily")),
        "free_used": free_used,
        "free_limit": free_limit,
        "free_remaining": free_remaining,
        "is_free_tier": d.get("is_free_tier"),
        "limit_reset": d.get("limit_reset"),
    }

    # Known only if at least one half of the state is actually readable.
    # A response with neither spend nor bucket means the schema moved.
    out["known"] = spend is not None or free_used is not None
    out["state"] = "known" if out["known"] else "drift"
    if not out["known"]:
        out["reason"] = ("parsed /api/v1/key but found neither 'usage' nor "
                         "'free_model_daily_requests' - schema changed?")
    return out


def measure(state: dict, key: str | None) -> dict:
    """Best-effort quota reading. Never raises."""
    if not key:
        return {"known": False, "state": "no_key", "reason": "no key"}

    now = time.time()
    cached = state.get("credits")
    if isinstance(cached, dict) and now - float(cached.get("ts", 0)) < CACHE_TTL:
        return {**cached, "cached": True}

    try:
        raw = fetch_key_info(key)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
        return {"known": False, "state": "offline",
                "reason": f"{type(exc).__name__}: {exc}"}

    parsed = parse_key_info(raw)
    if not parsed.get("known"):
        # Offline is fail-open; a schema drift is NOT. Distinguish them.
        parsed["state"] = "drift"
        return parsed

    parsed["ts"] = now
    parsed["state"] = "known"
    state["credits"] = parsed
    return parsed


def verdict(m: dict, warn_used: int = WARN_USED, stop_used: int = STOP_USED,
            spend_ratio: float = STOP_SPEND_RATIO) -> tuple[str, str]:
    """(action, message). action is allow | warn | block."""
    st = m.get("state")

    # Explicit state dispatch. Written as separate returns on purpose: an
    # earlier version folded this into one boolean expression and Python's
    # and/or precedence made `drift` fall through to allow - i.e. the guard
    # silently disarmed itself again, exactly the bug this rewrite exists to
    # kill. Its own test caught it.
    if st == "offline":
        return "allow", ""          # fail open: a status outage is not a block
    if st == "no_key":
        return "allow", ""          # nothing configured to enforce
    if st == "drift" or not m.get("known"):
        # Schema drift: the guard cannot see the numbers any more. Refusing
        # all work would be a self-inflicted outage; saying nothing is how we
        # got here. Surface it, keep going.
        return "warn", (
            "QUOTA GUARD BLIND: read OpenRouter /api/v1/key but the expected "
            "fields were absent (" + str(m.get("reason", "?")) + "). Quota is "
            "NOT being enforced right now. Treat spend as unlimited until this "
            "is fixed: prefer the opencode-free ladder and local functions."
        )

    # Money first: the free bucket refills tomorrow, the dollar does not.
    spend = m.get("spend_usd")
    limit = m.get("spend_limit_usd")
    if spend is not None and limit:
        if spend >= limit * spend_ratio:
            return "block", (
                f"QUOTA STOP: OpenRouter spend ${spend:g} of ${limit:g} limit "
                f"(>={int(spend_ratio * 100)}%). Do NOT call OpenRouter. Use the "
                "local opencode ladder (opencode run -m opencode/space-bunny-free) "
                "for coding, and local functions (comfy_gen/imggen, whisper, "
                "skill-finder, SearXNG) for everything else."
            )
        if spend >= limit * 0.75:
            left = limit - spend
            return "warn", (
                f"QUOTA WARNING: ${spend:g} of ${limit:g} spent (${left:g} left). "
                "Switch to the opencode-free ladder and local functions now."
            )

    used = m.get("free_used")
    flimit = m.get("free_limit")
    if used is None:
        return "allow", ""          # no bucket field: nothing to enforce there
    if flimit is None:
        flimit = 1000
    if used >= stop_used:
        return "block", (
            f"QUOTA STOP: OpenRouter free tier {int(used)}/{int(flimit)} calls "
            "used. Switch to the opencode-free worker ladder or local functions; "
            "the bucket refills daily and spending it now buys nothing."
        )
    if used >= warn_used:
        return "warn", (
            f"QUOTA WARNING: {int(used)}/{int(flimit)} OpenRouter free calls used "
            f"({int(flimit - used)} left). Prefer the opencode-free ladder for "
            "coding and local functions for everything else. Note MoA-style "
            "multi-model turns cost ~3 calls each."
        )
    return "allow", ""


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print(json.dumps({}))
        return
    if not isinstance(payload, dict):
        print(json.dumps({}))
        return

    state = load_state()
    m = measure(state, api_key())
    save_state(state)
    action, message = verdict(m)

    if action == "allow":
        print(json.dumps({}))
        return

    # Latch so a persistent state injects once, not on every single tool call.
    latch = "latch_" + str(m.get("state")) + "_" + str(
        int(m.get("free_used") or m.get("spend_usd") or 0))
    if action == "warn" and state.get(latch):
        print(json.dumps({}))
        return
    state[latch] = True
    save_state(state)
    print(json.dumps({"action": "block", "message": message}))


if __name__ == "__main__":
    main()
