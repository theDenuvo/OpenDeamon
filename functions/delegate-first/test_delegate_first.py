"""Regression test for the delegate-first pre_tool_call hook.

Runs the REAL hook as a subprocess with a REAL Hermes v0.21.4 payload.
No mocks, no invented argument names — the payload shape below was
captured from a live session (see DEPLOYMENT_REPORT "Payload shape").

Why subprocess: the hook is a separate process in production, and the
failures worth catching (import errors, stdout noise, exit codes) are
invisible when you import it in-process.

Usage:  py test_delegate_first.py [-v]
Exit code 0 = pass.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE / "pre_tool_budget.py"
PY = sys.executable

VERBOSE = "-v" in sys.argv


def _payload(tool: str, tool_input: dict, session: str = "S", turn: str = "T") -> str:
    """A payload shaped exactly like Hermes v0.21.4 sends it.

    tool_input — NOT "args" (that mistake disabled the hook silently).
    turn_id lives in extra — reading it top-level made every turn of a
    session share one budget counter.
    """
    return json.dumps({
        "hook_event_name": "pre_tool_call",
        "tool_name": tool,
        "tool_input": tool_input,
        "session_id": session,
        "cwd": "A:\\OpenDeamon",
        "profile": "default",
        "extra": {
            "task_id": "K-" + session,
            "turn_id": "%s:%s" % (session, turn),
            "tool_call_id": "call-1",
            "api_request_id": "api-1",
        },
    })


def fire(tool: str, tool_input: dict, session: str = "S", turn: str = "T") -> tuple[str, str]:
    """Run the hook; return (action, message). action is block|allow|ERROR."""
    env = dict(os.environ)
    tmp = tempfile.mkdtemp(prefix="dlf-test-")
    env["HERMES_HOME"] = tmp
    try:
        proc = subprocess.run(
            [PY, str(HOOK)],
            input=_payload(tool, tool_input, session, turn),
            capture_output=True, text=True, timeout=20, env=env,
        )
    except subprocess.TimeoutExpired:
        return "ERROR", "hook timed out"
    if proc.returncode != 0:
        return "ERROR", (proc.stderr or "").strip()[-300:]
    try:
        out = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return "ERROR", "non-JSON stdout: %r" % proc.stdout[:200]
    if not isinstance(out, dict):
        return "ERROR", "stdout is not an object: %r" % out
    return out.get("action", "allow"), out.get("message", "")


CASES = [
    # (label, tool, tool_input, expected_action, must_mention)
    ("write_file blocked", "write_file",
     {"path": "a.py", "content": "x"}, "block", "opencode run"),
    ("patch blocked", "patch", {"path": "a.py"}, "block", "opencode run"),
    ("edit blocked", "edit", {"path": "a.py"}, "block", "opencode run"),
    ("code_execution blocked", "code_execution", {"code": "1"}, "block", "opencode run"),
    ("execute_code blocked", "execute_code", {"code": "1"}, "block", "opencode run"),
    ("delegate_task coding goal blocked", "delegate_task",
     {"tasks": [{"goal": "Create fizzbuzz.js and a unit test"}]}, "block", "opencode run"),
    ("delegate_task goal= blocked", "delegate_task",
     {"goal": "Refactor the parser code"}, "block", "opencode run"),
    ("delegate_task research allowed", "delegate_task",
     {"goal": "Find 3 papers about MoA routing and summarize"}, "allow", ""),
    ("delegate_task read-only allowed", "delegate_task",
     {"tasks": [{"goal": "Search the docs"}, {"context": "read-only"}]}, "allow", ""),
    ("opencode worker never blocked", "terminal",
     {"command": "opencode run -m opencode/space-bunny-free 'x'"}, "allow", ""),
    ("search_files allowed", "search_files", {"pattern": "*.py"}, "allow", ""),
    ("read_file allowed", "read_file", {"path": "a.py"}, "allow", ""),
    ("skill_view allowed", "skill_view", {"name": "whisper"}, "allow", ""),
    ("web_search allowed", "web_search", {"query": "x"}, "allow", ""),
]


def test_cases() -> list[tuple[str, str]]:
    fails = []
    for label, tool, ti, want, mention in CASES:
        action, msg = fire(tool, ti)
        if action != want:
            fails.append(f"{label}: expected {want}, got {action} ({msg[:90]})")
        elif mention and mention not in msg:
            fails.append(f"{label}: message lacks {mention!r}: {msg[:90]}")
        elif VERBOSE:
            print(f"  ok  {label} -> {action}")
    return fails


def test_terminal_budget() -> list[str]:
    """Budget is per (session, turn) and must not leak across turns."""
    fails = []
    tmp = tempfile.mkdtemp(prefix="dlf-budget-")
    os.environ["HERMES_HOME"] = tmp
    try:
        script = r"""
import json, subprocess, sys, os
PY = sys.executable
HOOK = sys.argv[1]
def fire(tool, ti, session, turn):
    p = json.dumps({"hook_event_name": "pre_tool_call", "tool_name": tool,
                    "tool_input": ti, "session_id": session, "cwd": "A:",
                    "profile": "default",
                    "extra": {"turn_id": "%s:%s" % (session, turn),
                              "task_id": "K"}})
    r = subprocess.run([PY, HOOK], input=p, capture_output=True, text=True)
    if r.returncode != 0:
        return "ERROR"
    return json.loads(r.stdout or "{}").get("action", "allow")

HOOK_PATH = HOOK
import subprocess as sp
res = {}
# turn A: 9 plain terminal calls in one turn
res["turnA"] = [fire("terminal", {"command": "ls"}, "S1", "A") for _ in range(9)]
# turn B: same session, new turn -> budget must reset
res["turnB"] = [fire("terminal", {"command": "ls"}, "S1", "B") for _ in range(9)]
print(json.dumps(res))
"""
        proc = subprocess.run([PY, "-c", script, str(HOOK)],
                              capture_output=True, text=True, timeout=120,
                              env=dict(os.environ, HERMES_HOME=tmp))
        if proc.returncode != 0:
            return [f"budget probe crashed: {proc.stderr[-200:]}"]
        res = json.loads(proc.stdout.strip().splitlines()[-1])
        a, b = res["turnA"], res["turnB"]
        if "ERROR" in a or "ERROR" in b:
            fails.append("budget probe saw a hook ERROR")
        if a[:6] != ["allow"] * 6:
            fails.append(f"turn A: first 6 calls should allow, got {a[:6]}")
        if a[6:] != ["block"] * 3:
            fails.append(f"turn A: calls 7-9 should block, got {a[6:]}")
        if b[:6] != ["allow"] * 6:
            fails.append(f"turn B: budget must reset per turn, got {b[:6]}")
    except Exception as exc:  # noqa: BLE001
        fails.append(f"budget test error: {exc}")
    return fails


def test_fail_open() -> list[str]:
    """A malformed payload must never crash the hook (fail-open)."""
    fails = []
    for bad in ("", "not json at all", "[]", '{"tool_name": null}', "null"):
        env = dict(os.environ, HERMES_HOME=tempfile.mkdtemp(prefix="dlf-bad-"))
        try:
            proc = subprocess.run([PY, str(HOOK)], input=bad, capture_output=True,
                                  text=True, timeout=20, env=env)
        except subprocess.TimeoutExpired:
            fails.append(f"malformed {bad!r}: timed out")
            continue
        if proc.returncode != 0:
            fails.append(f"malformed {bad!r}: exit {proc.returncode}")
        else:
            try:
                json.loads(proc.stdout or "{}")
            except json.JSONDecodeError:
                fails.append(f"malformed {bad!r}: non-JSON stdout")
    return fails


def main() -> int:
    if not HOOK.is_file():
        print(f"FAIL: hook not found at {HOOK}")
        return 1
    groups = [
        ("policy cases", test_cases),
        ("terminal budget per turn", test_terminal_budget),
        ("fail-open on malformed payload", test_fail_open),
    ]
    total = 0
    for label, fn in groups:
        fails = fn()
        total += len(fails)
        if fails:
            print(f"FAIL  {label}")
            for f in fails:
                print(f"        - {f}")
        else:
            print(f"pass  {label}")
    print()
    if total:
        print(f"{total} failure(s)")
        return 1
    print(f"all groups pass ({len(CASES)} policy cases + budget + fail-open)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
