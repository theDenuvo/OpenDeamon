"""quota_watch threshold tests with mocked credit responses (no network).

Checks the decision table: money first, then the daily free bucket, then
warn-once, and fail-open when the endpoint is unreachable.
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE / "quota_watch.py"
PY = sys.executable

spec = importlib.util.spec_from_file_location("quota_watch", HOOK)
qw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qw)


def m(**kw):
    base = {"known": True, "ts": 0.0, "free_used": 0, "free_limit": 1000,
            "total_usage": 0.0, "limit_usd": 1.0}
    base.update(kw)
    return base


CASES = [
    ("healthy", m(free_used=100), "allow"),
    ("warn zone", m(free_used=850), "warn"),
    ("at stop", m(free_used=950), "block"),
    ("spend 90%", m(free_used=100, total_usage=0.92), "block"),
    ("spend high wins over bucket", m(free_used=10, total_usage=1.0), "block"),
    ("unknown fails open", {"known": False}, "allow"),
    ("no free info", m(known=True, free_used=None), "allow"),
]


def main() -> int:
    fails = []
    for label, data, want in CASES:
        action, msg = qw.verdict(data)
        got = {"allow": "allow", "warn": "warn", "block": "block"}[action]
        if got != want:
            fails.append(f"{label}: expected {want}, got {got} ({msg[:70]})")
        else:
            print(f"  ok  {label} -> {got}")

    # budget direction check: verdict must not depend on wall clock
    print("\nlive hook (no key => must fail open):")
    tmp = tempfile.mkdtemp(prefix="qw-")
    env = dict(os.environ, HERMES_HOME=tmp)
    env.pop("OPENROUTER_API_KEY", None)
    payload = json.dumps({"hook_event_name": "pre_tool_call", "tool_name": "terminal",
                          "tool_input": {"command": "ls"}, "session_id": "S",
                          "extra": {"turn_id": "T"}})
    p = subprocess.run([PY, str(HOOK)], input=payload, capture_output=True,
                       text=True, timeout=30, env=env)
    if p.returncode != 0:
        fails.append(f"live hook exited {p.returncode}: {p.stderr[-150:]}")
    elif json.loads(p.stdout or "{}").get("action", "allow") != "allow":
        fails.append("live hook blocked without a key; it must fail open")
    else:
        print("  ok  fails open without credentials")

    print()
    if fails:
        print("FAIL")
        for f in fails:
            print("   -", f)
        return 1
    print(f"all pass ({len(CASES)} thresholds + fail-open)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
