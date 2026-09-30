"""quota_watch tests against the REAL /api/v1/key schema.

Rewritten 2026-09-30. The previous version mocked the stale
`/api/v1/credits` shape (data.usage.limit.daily_free_models), so it stayed
green while the hook parsed nothing from the live endpoint and verdict()
silently returned allow. Mocks here are taken from a live call recorded in
quota_watch.py's docstring, and there is an explicit regression case for a
response that parses to no usable numbers.

Stdlib only. Run: py A:/OpenDeamon/functions/quota-watch/test_quota_watch.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE / "quota_watch.py"
PY = sys.executable

sys.path.insert(0, str(HERE))
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("quota_watch", HOOK)
qw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qw)


# --- fixtures -----------------------------------------------------------------

# Verbatim shape from a live GET /api/v1/key on 2026-09-30.
LIVE_KEY = {
    "label": "sk-or-v1-8c8...40b",
    "is_management_key": False,
    "limit": 1,
    "limit_reset": "daily",
    "limit_remaining": 1,
    "include_byok_in_limit": False,
    "usage": 0,
    "usage_daily": 0,
    "usage_weekly": 0,
    "usage_monthly": 0,
    "byok_usage": 0,
    "is_free_tier": False,
    "free_model_daily_requests": {"used": 1, "limit": 1000, "remaining": 999},
    "rate_limit": {"requests": -1, "interval": "10s"},
}

# The endpoint the hook used before, which carries no bucket at all.
LEGACY_CREDITS = {"data": {"total_credits": 10, "total_usage": 0}}


def parsed(**over):
    """A parsed measurement with sane defaults, as parse_key_info would return."""
    base = {
        "known": True, "state": "known",
        "spend_usd": 0.0, "spend_limit_usd": 1.0, "spend_daily": 0.0,
        "free_used": 1.0, "free_limit": 1000.0, "free_remaining": 999.0,
        "is_free_tier": False, "limit_reset": "daily",
    }
    base.update(over)
    return base


# --- tests --------------------------------------------------------------------

def test_parse_live_schema():
    """The live shape must yield both halves of the state."""
    m = qw.parse_key_info({"data": LIVE_KEY})
    assert m["known"], "live schema must parse as known"
    assert m["spend_usd"] == 0.0
    assert m["spend_limit_usd"] == 1.0
    assert m["free_used"] == 1.0
    assert m["free_limit"] == 1000.0
    return []


def test_parse_legacy_shape_is_drift():
    """Regression: the old endpoint has neither field -> NOT known.

    This is the exact case that made the guard a no-op while tests stayed
    green. It must be reported as drift, never as healthy.
    """
    m = qw.parse_key_info(LEGACY_CREDITS)
    assert not m["known"], "legacy shape must not be treated as known"
    action, msg = qw.verdict(m)
    assert action == "warn", f"drift must warn, got {action}"
    assert "BLIND" in msg, f"drift message must say enforcement is blind: {msg!r}"
    return []


def test_garbage_response_is_drift():
    for junk in ({"data": {}}, {"unexpected": True}, {}):
        m = qw.parse_key_info(junk)
        assert not m["known"], f"{junk!r} must not be known"
        action, msg = qw.verdict(m)
        assert action == "warn", f"{junk!r}: expected warn, got {action}"
    return []


def test_offline_fails_open():
    """A status endpoint outage must not block real work."""
    m = {"known": False, "state": "offline", "reason": "URLError: timeout"}
    action, _ = qw.verdict(m)
    assert action == "allow", f"offline must fail open, got {action}"
    return []


def test_no_key_fails_open():
    m = {"known": False, "state": "no_key", "reason": "no key"}
    action, _ = qw.verdict(m)
    assert action == "allow"
    return []


def test_thresholds():
    cases = [
        ("healthy", parsed(free_used=1, spend_usd=0.0), "allow"),
        ("bucket warn", parsed(free_used=850), "warn"),
        ("bucket stop", parsed(free_used=950), "block"),
        ("bucket above stop", parsed(free_used=1200), "block"),
        ("spend 75% warn", parsed(spend_usd=0.75, free_used=1), "warn"),
        ("spend 90% stop", parsed(spend_usd=0.90, free_used=1), "block"),
        ("money beats bucket", parsed(spend_usd=1.0, free_used=10), "block"),
        ("no bucket field", parsed(free_used=None, free_limit=None), "allow"),
        ("free exactly at limit", parsed(free_used=1000.0), "block"),
    ]
    fails = []
    for label, m, want in cases:
        action, msg = qw.verdict(m)
        if action != want:
            fails.append(f"{label}: expected {want}, got {action} ({msg[:70]})")
    return fails


def test_messages_mention_the_way_out():
    """A block must tell the model what to do instead, not just stop it."""
    for m, needle in (
        (parsed(free_used=1000), "opencode"),
        (parsed(spend_usd=1.0), "opencode"),
    ):
        action, msg = qw.verdict(m)
        assert action == "block", f"expected block, got {action}"
        if needle not in msg:
            return [f"block message missing {needle!r}: {msg[:120]}"]
    return []


def test_numeric_coercion():
    """A schema sending strings must not crash or be discarded."""
    m = qw.parse_key_info({"data": {"usage": "0.42", "limit": "1",
                                    "free_model_daily_requests": {"used": "800",
                                                                 "limit": "1000"}}})
    assert m["known"], "string numerics must still parse"
    if m["spend_usd"] != 0.42:
        return [f"string spend not coerced: {m['spend_usd']!r}"]
    if m["free_used"] != 800.0:
        return [f"string bucket not coerced: {m['free_used']!r}"]
    return []


def test_measure_offline_does_not_cache():
    """A failed read must not be cached as if it were a reading."""
    tmp = tempfile.mkdtemp(prefix="qw-off-")
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = tmp
    try:
        state = {}
        m = qw.measure(state, key="sk-does-not-exist")   # auth failure -> offline
        assert not m["known"], "bad credentials must not be known"
        if "credits" in state:
            return ["offline measurement was cached in state"]
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old
    return []


def test_live_no_credentials_emits_allow():
    """End-to-end: without credentials the hook must not crash and must allow."""
    tmp = tempfile.mkdtemp(prefix="qw-live-")
    env = dict(os.environ, HERMES_HOME=tmp)
    env.pop("OPENROUTER_API_KEY", None)
    env.pop("OPENROUTER_KEY", None)
    payload = json.dumps({"hook_event_name": "pre_tool_call", "tool_name": "terminal",
                          "tool_input": {"command": "ls"}, "session_id": "S",
                          "extra": {"turn_id": "T"}})
    p = subprocess.run([PY, str(HOOK)], input=payload, capture_output=True,
                       text=True, timeout=30, env=env)
    if p.returncode != 0:
        return [f"hook exited {p.returncode}: {p.stderr[-200:]}"]
    try:
        out = json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        return [f"non-JSON stdout: {p.stdout[:150]!r}"]
    if out.get("action", "allow") != "allow":
        return [f"expected allow without credentials, got {out.get('action')}"]
    return []


TESTS = [
    ("parse live /api/v1/key schema", test_parse_live_schema),
    ("legacy /credits shape -> drift, not healthy", test_parse_legacy_shape_is_drift),
    ("garbage responses -> drift", test_garbage_response_is_drift),
    ("offline fails open", test_offline_fails_open),
    ("no key fails open", test_no_key_fails_open),
    ("thresholds", test_thresholds),
    ("block messages name the way out", test_messages_mention_the_way_out),
    ("string numerics coerced", test_numeric_coercion),
    ("offline not cached", test_measure_offline_does_not_cache),
    ("live hook without credentials allows", test_live_no_credentials_emits_allow),
]


def main() -> int:
    failed = 0
    for label, fn in TESTS:
        try:
            fails = fn() or []
        except AssertionError as exc:
            fails = [f"assertion: {exc}"]
        except Exception as exc:  # noqa: BLE001
            fails = [f"{type(exc).__name__}: {exc}"]
        if fails:
            failed += 1
            print(f"FAIL  {label}")
            for f in fails:
                print(f"        - {f}")
        else:
            print(f"pass  {label}")
    print()
    if failed:
        print(f"{failed}/{len(TESTS)} groups failed")
        return 1
    print(f"all pass ({len(TESTS)} groups)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
