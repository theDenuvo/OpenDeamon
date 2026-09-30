"""delegate-first pre_tool_call hook (stdlib only).

One hard rule: the core never writes code itself. Any tool that creates or
modifies files / runs code inline (write_file, patch, edit, code_execution,
execute_code) is blocked outright and the model is told to hand the unit to
the opencode CLI worker. `terminal` stays available for looking around and
verifying, with a small per-turn budget.
Stdout: {"action": "block", "message": ...} or {} (allow).
State: HERMES_HOME/cache/delegate-first.json (best-effort, fail-open).
"""
import json
import os
import re
import sys
import time

CODE_WRITE_TOOLS = {"write_file", "patch", "edit",
                    "code_execution", "execute_code"}
EXEC_TOOLS = CODE_WRITE_TOOLS | {"terminal"}
BUDGET = 6
KEEP_TURNS = 60

# The worker escape hatch is never blocked: delegating IS the desired
# escalation, so opencode worker invocations bypass the budget.
WORKER_RE = re.compile(r"(^|[;&|])\s*opencode\s+run\b", re.IGNORECASE)

REDIRECT_CODE = (
    "You do not write code yourself, and you do not hand code to "
    "delegate_task subagents (they are slower and weaker). Hand this "
    "coding unit to the opencode CLI worker NOW, in this turn, with one "
    "terminal command:\n"
    "  opencode run -m opencode/space-bunny-free "
    "'<what to write, how, where to save, must-haves, forbidden, docs>'\n"
    "Model ladder (first working wins): space-bunny-free -> "
    "muse-spark-1.3-contributor-free -> nemotron-3-ultra-free -> "
    "big-pickle -> any other opencode/*-free.\n"
    "Then verify its work by running the tests (terminal is allowed). "
    "Do not retry this tool and do not use delegate_task for code."
)

# delegate_task is for research/recon; coding goals must go to opencode.
CODE_GOAL_RE = re.compile(
    r"\b(write|create|implement|refactor|fix|add|build|code|function|"
    r"class|script|module|unit test|tests|bug|patch|endpoint|api)\b",
    re.IGNORECASE)

REDIRECT_BUDGET = (
    "Direct execution budget spent (%d terminal calls this turn). Stop "
    "doing the work inline. If a coding unit remains, hand it to the "
    "opencode CLI worker now: opencode run -m opencode/<free-model> "
    "'<task + acceptance>'. Otherwise reply to the user."
)


def goal_text(args):
    """Flatten every text field of a delegate_task payload.

    The tool is called both as goal=<str> and tasks=[{goal, context}, ...];
    reading only "goal" silently missed the batch form (observed live).
    """
    parts = []

    def collect(val):
        if isinstance(val, str):
            parts.append(val)
        elif isinstance(val, dict):
            for v in val.values():
                collect(v)
        elif isinstance(val, (list, tuple)):
            for item in val:
                collect(item)

    collect(args)
    return " ".join(parts)


def home():
    return os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))


def state_path():
    p = os.path.join(home(), "cache", "delegate-first.json")
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
    except Exception:
        pass
    return p


def load():
    try:
        with open(state_path(), encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save(state):
    try:
        items = sorted(state.items(), key=lambda kv: kv[1].get("seen", 0))
        for k, _ in items[:-KEEP_TURNS]:
            state.pop(k, None)
        with open(state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print(json.dumps({}))
        return
    if not isinstance(payload, dict):
        print(json.dumps({}))
        return
    # Real Hermes v0.21.4 payload (verified live 2026-09-29):
    #   {hook_event_name, tool_name, tool_input, session_id, cwd, profile,
    #    extra:{task_id, tool_call_id, turn_id, api_request_id, ...}}
    # Note: tool_input, NOT "args" — reading "args" silently disabled
    # both the worker exemption and the delegate_task block.
    tool = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input", {})
    if tool == "delegate_task":
        if CODE_GOAL_RE.search(goal_text(tool_input)):
            print(json.dumps({"action": "block",
                              "message": REDIRECT_CODE}))
            return
        print(json.dumps({}))
        return
    if tool not in EXEC_TOOLS:
        print(json.dumps({}))
        return
    if tool in CODE_WRITE_TOOLS:
        print(json.dumps({"action": "block",
                          "message": REDIRECT_CODE}))
        return
    cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if WORKER_RE.search(str(cmd)):
        print(json.dumps({}))
        return
    extra = payload.get("extra", {})
    turn = (extra.get("turn_id") if isinstance(extra, dict) else None) or "?"
    key = "%s:%s" % (payload.get("session_id", "?"), turn)
    state = load()
    entry = state.get(key, {"n": 0, "seen": 0})
    entry["n"] = entry.get("n", 0) + 1
    entry["seen"] = time.time()
    state[key] = entry
    save(state)
    if entry["n"] > BUDGET:
        print(json.dumps({"action": "block",
                          "message": REDIRECT_BUDGET % BUDGET}))
    else:
        print(json.dumps({}))


if __name__ == "__main__":
    main()
