"""spec_gate tests — invariants of the spec gate (SCHEME.md §8, TODO.md слой 1).

Stdlib only, no network. Every test is a regression against a specific way
this gate could rot into a rubber stamp:

  * `manual` reaching VERIFIED            -> the verified/judged boundary dies
  * a declared method with no `check`     -> "a method exists" read as verified
  * 3+ UNVERIFIABLE not escalating        -> laze-out path opens
  * a critical UNVERIFIABLE not escalating -> count-independent escalation dies
  * HTTP 200 + empty content = pass       -> gpt-oss max_tokens trap
  * 451/403 blamed on the spec            -> infra eats a rework cycle

Run: py A:/OpenDeamon/functions/specgate/test_spec_gate.py
"""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import sys
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
GATE = HERE / "spec_gate.py"

spec_obj = importlib.util.spec_from_file_location("spec_gate", GATE)
sg = importlib.util.module_from_spec(spec_obj)
spec_obj.loader.exec_module(sg)


def c(text, **kw):
    d = {"text": text}
    d.update(kw)
    return d


def spec(*criteria, **kw):
    d = {"goal": kw.pop("goal", "test task"), "criteria": list(criteria)}
    d.update(kw)
    return d


# --- fixtures -----------------------------------------------------------------

GOOD_SPEC = spec(
    c("quota_watch parses /api/v1/key", method="test",
      check="py -m pytest test_quota_watch.py::test_parse_live_schema"),
    c("verdict() returns allow on offline", method="command",
      check="py -c \"import quota_watch\" "),
    c("baseline hash is unchanged", method="invariant",
      check="LEDGER hash equality"),
    c("rendered report matches golden", method="snapshot",
      check="golden/report.txt"),
)


def _result(raw, use_llm=False):
    return sg.run(raw, use_llm=use_llm, state={})


# --- tests --------------------------------------------------------------------

def test_auto_methods_reach_verified_ceiling():
    r = _result(GOOD_SPEC)
    fails = []
    for crit in r["criteria"]:
        if crit["status"] != sg.STATUS_AUTO:
            fails.append("%s: expected auto, got %s (%s)"
                         % (crit["id"], crit["status"], crit["reason"]))
        if crit["ceiling"] != sg.CEILING_VERIFIED:
            fails.append("%s: ceiling %s" % (crit["id"], crit["ceiling"]))
    if r["outcome"] != sg.OUTCOME_PROCEED:
        fails.append("outcome %s: %s" % (r["outcome"], r["reasons"]))
    if r["exit_ceiling"] != sg.CEILING_VERIFIED:
        fails.append("exit ceiling %s" % r["exit_ceiling"])
    return fails


def test_manual_never_reaches_verified():
    """INVARIANT 1. The core of the gate: `manual` is JUDGED, forever."""
    r = _result(spec(c("looks right to a human", method="manual",
                       judge_by="owner opens it and says yes")))
    crit = r["criteria"][0]
    fails = []
    if crit["ceiling"] != sg.CEILING_JUDGED:
        fails.append("manual got ceiling %s" % crit["ceiling"])
    if crit["status"] != sg.STATUS_UNVERIFIABLE:
        fails.append("manual got status %s" % crit["status"])
    if crit["reason"] != sg.REASON_MANUAL:
        fails.append("manual reason %s" % crit["reason"])
    if r["exit_ceiling"] != sg.CEILING_JUDGED:
        fails.append("exit ceiling %s" % r["exit_ceiling"])
    if "C1" not in r["judged_ids"]:
        fails.append("manual criterion missing from judged_ids: %s"
                     % r["judged_ids"])
    # And the string must not appear anywhere in the rendered report as a
    # status we granted.
    if sg.CEILING_VERIFIED in str(crit.values()):
        fails.append("VERIFIED leaked into a manual criterion: %r" % crit)
    return fails


def test_ceiling_has_no_manual_path():
    """Direct guard on the invariant's implementation point.

    If someone adds `manual` to AUTO_METHODS, `_ceiling` starts returning
    VERIFIED for it. `_ceiling` is the single place that grants VERIFIED, so
    assert on it directly rather than trusting the higher-level tests.
    """
    fails = []
    if sg._ceiling(sg.MANUAL_METHOD) == sg.CEILING_VERIFIED:
        fails.append("_ceiling(manual) grants VERIFIED")
    if sg.MANUAL_METHOD in sg.AUTO_METHODS:
        fails.append("manual listed among AUTO_METHODS")
    for method in sg.AUTO_METHODS:
        if sg._ceiling(method) != sg.CEILING_VERIFIED:
            fails.append("%s cannot reach VERIFIED" % method)
    return fails


def test_method_without_check_is_unverifiable():
    """A declared method is not a verification capability.

    `method: test` with no test name looks like coverage and checks
    nothing. Without this case the gate is a formality.
    """
    r = _result(spec(c("parser handles nulls", method="test")))
    crit = r["criteria"][0]
    fails = []
    if crit["status"] != sg.STATUS_UNVERIFIABLE:
        fails.append("expected unverifiable, got %s" % crit["status"])
    if crit["reason"] != sg.REASON_NO_CHECK:
        fails.append("reason %s" % crit["reason"])
    if crit["ceiling"] != sg.CEILING_JUDGED:
        fails.append("ceiling %s" % crit["ceiling"])
    return fails


def test_no_method_is_unverifiable_but_work_continues():
    """UNVERIFIABLE != stop (§5.1). Work proceeds; the exit status is judged."""
    r = _result(spec(c("looks clean to a human"),
                     c("tests pass", method="test", check="pytest -q")))
    crit, good = r["criteria"]
    fails = []
    if crit["reason"] != sg.REASON_NO_METHOD:
        fails.append("reason %s" % crit["reason"])
    if good["status"] != sg.STATUS_AUTO:
        fails.append("the verifiable criterion was rejected too: %s"
                     % good["status"])
    if r["outcome"] != sg.OUTCOME_PROCEED:
        fails.append("work was stopped by one unverifiable: %s" % r["outcome"])
    if not r["worker_may_start"]:
        fails.append("worker_may_start is False on a 1/2 unverifiable spec")
    if r["exit_ceiling"] != sg.CEILING_JUDGED:
        fails.append("exit ceiling %s" % r["exit_ceiling"])
    return fails


def test_unknown_method_does_not_become_manual():
    """A typo must not masquerade as a deliberate human call."""
    r = _result(spec(c("x", method="tets", check="whatever")))
    crit = r["criteria"][0]
    if crit["reason"] != sg.REASON_UNKNOWN_METHOD:
        return ["typo reason %s (expected unknown_method)" % crit["reason"]]
    if crit["ceiling"] != sg.CEILING_JUDGED:
        return ["typo ceiling %s" % crit["ceiling"]]
    return []


def test_manual_without_judging_method_is_flagged():
    r = _result(spec(c("feels fast", method="manual")))
    crit = r["criteria"][0]
    if crit["reason"] != sg.REASON_MANUAL_NO_JUDGE:
        return ["reason %s" % crit["reason"]]
    text = sg.render(r)
    if "способ суждения не задан" not in text:
        return ["render did not demand a judging method"]
    return []


def test_escalation_threshold_is_strictly_above_two():
    """INVARIANT 2: >2 UNVERIFIABLE escalates BEFORE work starts."""
    fails = []
    two = _result(spec(c("a"), c("b"), c("d", method="test", check="t")))
    if two["outcome"] != sg.OUTCOME_PROCEED:
        fails.append("2 unverifiable escalated: %s" % two["outcome"])
    if two["worker_may_start"] is not True:
        fails.append("2 unverifiable blocked the worker")

    three = _result(spec(c("a"), c("b"), c("d"), c("e")))
    if three["outcome"] != sg.OUTCOME_ESCALATE:
        fails.append("3 unverifiable did not escalate: %s" % three["outcome"])
    if three["worker_may_start"] is not False:
        fails.append("escalated spec still allows the worker to start")
    if not any("unverifiable_count" in r for r in three["reasons"]):
        fails.append("no count-based reason: %s" % three["reasons"])
    return fails


def test_critical_unverifiable_escalates_independently_of_count():
    """One critical unverifiable outranks the count. §5.1: 'the button
    colour' and 'user data must not be lost' both count as one."""
    r = _result(spec(c("user data must not be lost", method="manual",
                       critical=True)))
    crit = r["criteria"][0]
    fails = []
    if r["counts"]["unverifiable"] >= sg.UNVERIFIABLE_ESCALATE_AT:
        fails.append("fixture is wrong: the count alone already escalates "
                     "(%s unverifiable), so it proves nothing about "
                     "criticality" % r["counts"]["unverifiable"])
    if r["outcome"] != sg.OUTCOME_ESCALATE:
        fails.append("critical unverifiable did not escalate: %s" % r["outcome"])
    if r["worker_may_start"]:
        fails.append("escalated on a critical criterion still lets work start")
    if not any("critical_unverifiable" in reason for reason in r["reasons"]):
        fails.append("no critical reason recorded: %s" % r["reasons"])
    return fails


def test_critical_flag_accepts_strings():
    r = _result(spec(c("data loss", method="manual", critical="yes")))
    if r["outcome"] != sg.OUTCOME_ESCALATE:
        return ["string critical flag ignored: %s" % r["outcome"]]
    r2 = _result(spec(c("data loss", method="manual", critical="false")))
    if r2["outcome"] != sg.OUTCOME_PROCEED:
        return ["string 'false' counted as critical: %s" % r2["outcome"]]
    return []


def test_empty_criterion_escalates():
    r = _result(spec(c("real", method="test", check="t"), c("")))
    crit = r["criteria"][1]
    fails = []
    if crit["reason"] != sg.REASON_EMPTY:
        fails.append("empty reason %s" % crit["reason"])
    if crit["ceiling"] != sg.CEILING_JUDGED:
        fails.append("empty ceiling %s" % crit["ceiling"])
    if not crit["critical"]:
        fails.append("an empty criterion was not treated as critical")
    if r["outcome"] != sg.OUTCOME_ESCALATE:
        fails.append("an empty criterion did not escalate: %s" % r["outcome"])
    return fails


def test_judged_criteria_are_named_explicitly():
    """§6: judged criteria are listed by name. A report that says only
    'judged' leaves the human nothing to look at."""
    r = _result(spec(c("taste", method="manual", judge_by="owner looks"),
                     c("a"), c("b"), c("c")))
    text = sg.render(r)
    fails = []
    if len(r["judged_ids"]) != 4:
        fails.append("judged_ids %s" % r["judged_ids"])
    for cid in r["judged_ids"]:
        if cid not in text:
            fails.append("%s not named in the report" % cid)
    if "НЕ verified" not in text:
        fails.append("report does not say judged != verified")
    return fails


# --- risk triggers ------------------------------------------------------------

def test_risk_triggers_detect_all_six_shapes():
    cases = [
        (sg.RISK_EXISTING_CODE, "modify the existing quota_watch hook"),
        (sg.RISK_MANY_CRITERIA, None),      # covered by count, below
        (sg.RISK_API, "the endpoint response schema stays the same"),
        (sg.RISK_STATE, "the parsed quota state persists to the cache file"),
        (sg.RISK_CONCURRENCY, "the hook must stay non-blocking under a lock"),
        (sg.RISK_DESTRUCTIVE, "delete the stale cache entry on mismatch"),
    ]
    fails = []
    for name, text in cases:
        if text is None:
            continue
        spec_ = spec(*[c("c%d" % i, method="test", check="t%d" % i)
                       for i in range(4)], goal=text)
        risk = sg.detect_risk(sg.normalize_spec(spec_), sg.mechanical(
            sg.normalize_spec(spec_)))
        if name not in risk["fired"]:
            fails.append("%s not fired for %r (fired: %s)"
                         % (name, text, sorted(risk["fired"])))
    many = spec(*[c("c%d" % i, method="test", check="t") for i in range(4)])
    risk = sg.detect_risk(sg.normalize_spec(many),
                          sg.mechanical(sg.normalize_spec(many)))
    if sg.RISK_MANY_CRITERIA not in risk["fired"]:
        fails.append("many_criteria not fired for 4 criteria")
    return fails


def test_tiny_clean_spec_triggers_no_risk_and_skips_llm():
    """The default must be mechanical-only. An LLM call on every spec is
    exactly the cost this gate exists to avoid."""
    calls = []
    real = sg.call_nim
    sg.call_nim = lambda payload, key, timeout=sg.TIMEOUT: calls.append(payload)
    try:
        r = sg.run(spec(c("text of the note is correct", method="test",
                          check="py test_note.py")), state={})
    finally:
        sg.call_nim = real
    fails = []
    if calls:
        fails.append("LLM was called without a risk trigger")
    if r["risk"]["count"] != 0:
        fails.append("clean spec fired risk: %s" % r["risk"]["fired"])
    if r["review"]["state"] != "skipped_no_risk":
        fails.append("review state %s" % r["review"]["state"])
    return fails


def test_flag_forces_risk_on_demand():
    r = _result(spec(c("x", method="test", check="t"),
                     flags={"needs_llm_review": True}))
    if sg.RISK_MANY_CRITERIA in r["risk"]["fired"] and r["risk"]["count"] == 1:
        return ["unexpected trigger set: %s" % r["risk"]["fired"]]
    if "needs_llm_review" not in r["risk"]["fired"]:
        return ["explicit flag ignored: %s" % r["risk"]["fired"]]
    return []


# --- reviewer leg: the traps -------------------------------------------------

def _http_error(code):
    return urllib.error.HTTPError(
        "u", code, "err", {}, io.BytesIO(b""))


def test_review_empty_content_is_not_a_pass():
    """gpt-oss eats max_tokens on reasoning and returns '' with HTTP 200."""
    real = sg.call_nim
    real_key = sg.api_key
    sg.api_key = lambda: "nvapi-test"
    sg.call_nim = lambda p, k, timeout=sg.TIMEOUT: {"choices": [
        {"message": {"content": ""}}]}
    try:
        out = sg.review_with_llm(sg.normalize_spec(GOOD_SPEC),
                                 sg.mechanical(sg.normalize_spec(GOOD_SPEC)))
    finally:
        sg.call_nim = real
        sg.api_key = real_key
    if out["state"] != "empty_content":
        return ["empty content was not caught: %s" % out]
    return []


def test_review_rejects_bad_json_and_unknown_verdict():
    fails = []
    real = sg.call_nim
    real_key = sg.api_key
    sg.api_key = lambda: "nvapi-test"

    def with_content(text):
        sg.call_nim = lambda p, k, timeout=sg.TIMEOUT: {"choices": [
            {"message": {"content": text}}]}

    try:
        spec_ = sg.normalize_spec(GOOD_SPEC)
        mech = sg.mechanical(spec_)
        with_content("no braces here")
        out = sg.review_with_llm(spec_, mech)
        if out["state"] != "bad_json":
            fails.append("prose accepted: %s" % out)

        with_content('{"verdict": "maybe"}')
        out = sg.review_with_llm(spec_, mech)
        if out["state"] != "bad_json":
            fails.append("unknown verdict accepted: %s" % out)

        with_content('```json\n{"verdict": "accept", "reason": "ok"}\n```')
        out = sg.review_with_llm(spec_, mech)
        if out["state"] != "ok" or out["verdict"] != "accept":
            fails.append("fenced JSON rejected: %s" % out)
    finally:
        sg.call_nim = real
        sg.api_key = real_key
    return fails


def test_vpn_and_quota_errors_are_infra_not_spec_faults():
    """451/403/429/503 must never turn into REWORK_SPEC. Otherwise a dead
    tunnel burns a spec-rework cycle that cannot fix anything."""
    fails = []
    real = sg.call_nim
    real_env = sg.api_key
    sg.api_key = lambda: "nvapi-test"
    try:
        for code in (401, 403, 429, 451, 503):
            def boom(p, k, timeout=sg.TIMEOUT, code=code):
                raise _http_error(code)

            sg.call_nim = boom
            out = sg.review_with_llm(sg.normalize_spec(GOOD_SPEC),
                                     sg.mechanical(sg.normalize_spec(GOOD_SPEC)))
            if out["state"] != "infra":
                fails.append("HTTP %s: state %s" % (code, out["state"]))
            if code == 451 and out.get("kind") != "vpn_or_egress":
                fails.append("451 misclassified: %s" % out)
            if code == 503 and out.get("kind") != "overloaded":
                fails.append("503 misclassified (NIM's overload code): %s" % out)

        sg.call_nim = lambda p, k, timeout=sg.TIMEOUT: (_ for _ in ()).throw(
            urllib.error.URLError("connection reset"))
        out = sg.review_with_llm(sg.normalize_spec(GOOD_SPEC),
                                 sg.mechanical(sg.normalize_spec(GOOD_SPEC)))
        if out["state"] != "infra":
            fails.append("URLError not infra: %s" % out)
    finally:
        sg.call_nim = real
        sg.api_key = real_env
    return fails


def test_infra_does_not_rework_the_spec():
    real = sg.call_nim
    real_env = sg.api_key
    sg.api_key = lambda: "nvapi-test"
    spec_ = spec(*[c("modify the api endpoint schema %d" % i, method="test",
                     check="t") for i in range(4)])
    try:
        def boom(p, k, timeout=sg.TIMEOUT):
            raise _http_error(451)

        sg.call_nim = boom
        r = sg.run(spec_, use_llm=True, state={})
    finally:
        sg.call_nim = real
        sg.api_key = real_env
    fails = []
    if r["outcome"] == sg.OUTCOME_REWORK_SPEC:
        fails.append("451 produced REWORK_SPEC")
    if not any("INFRA" in reason for reason in r["reasons"]):
        fails.append("INFRA not surfaced: %s" % r["reasons"])
    if r["worker_may_start"] is not True:
        fails.append("infra stopped real work: %s" % r["outcome"])
    return fails


def test_paid_model_is_refused_before_the_call():
    """The $0 law is checked before spending, not after."""
    calls = []
    real = sg.call_nim
    sg.call_nim = lambda p, k, timeout=sg.TIMEOUT: calls.append(p)
    try:
        out = sg.review_with_llm(sg.normalize_spec(GOOD_SPEC),
                                 sg.mechanical(sg.normalize_spec(GOOD_SPEC)),
                                 model="openai/gpt-4o")
    finally:
        sg.call_nim = real
    fails = []
    if calls:
        fails.append("a paid model was called")
    if out["state"] != "blocked_paid_model":
        fails.append("paid model not blocked: %s" % out)
    for model in ("openai/gpt-oss-20b", "nvidia/nemotron-3-super-120b",
                  "some/model:free"):
        if not sg._free_tier_ok(model):
            fails.append("false positive on free model %s" % model)
    return fails


def test_reviewer_params_follow_the_measured_traps():
    """max_tokens >= 700, temperature 0, json_object. Measured on Groq/NIM
    2026-09-30: anything smaller returns empty content at HTTP 200."""
    payload = sg._review_payload(sg.normalize_spec(GOOD_SPEC),
                                 sg.mechanical(sg.normalize_spec(GOOD_SPEC)))
    fails = []
    if payload["max_tokens"] < 700:
        fails.append("max_tokens %s" % payload["max_tokens"])
    if payload["temperature"] != 0:
        fails.append("temperature %s" % payload["temperature"])
    if payload["response_format"] != {"type": "json_object"}:
        fails.append("response_format %s" % payload["response_format"])
    if payload["model"] != sg.NIM_MODEL:
        fails.append("model %s" % payload["model"])
    return fails


def test_reject_cycles_are_capped_at_two():
    """A spec the reviewer keeps rejecting must reach a human, not loop."""
    real = sg.call_nim
    real_key = sg.api_key
    sg.api_key = lambda: "nvapi-test"
    sg.call_nim = lambda p, k, timeout=sg.TIMEOUT: {"choices": [
        {"message": {"content": '{"verdict":"reject","reason":"ambiguous"}'}}]}
    spec_ = spec(*[c("modify the api endpoint schema %d" % i, method="test",
                     check="t") for i in range(4)])
    try:
        state = {}
        outcomes = [sg.run(copy.deepcopy(spec_), True, state)["outcome"]
                    for _ in range(4)]
        sticky = sg.run(copy.deepcopy(spec_), True, state)["outcome"]
    finally:
        sg.call_nim = real
        sg.api_key = real_key
    fails = []
    if outcomes[0] != sg.OUTCOME_REWORK_SPEC:
        fails.append("first reject -> %s" % outcomes[0])
    if outcomes[1] != sg.OUTCOME_REWORK_SPEC:
        fails.append("second reject -> %s" % outcomes[1])
    if outcomes[2] != sg.OUTCOME_ESCALATE:
        fails.append("third reject did not escalate: %s" % outcomes[2])
    if sticky != sg.OUTCOME_ESCALATE:
        fails.append("escalation was not sticky: %s" % sticky)
    return fails


def test_reject_counter_is_keyed_per_spec():
    """Two different specs must not share a counter, or one spec's rejection
    blocks another's rework."""
    real = sg.call_nim
    real_key = sg.api_key
    sg.api_key = lambda: "nvapi-test"
    sg.call_nim = lambda p, k, timeout=sg.TIMEOUT: {"choices": [
        {"message": {"content": '{"verdict":"reject","reason":"x"}'}}]}
    a = spec(*[c("modify the api endpoint %d" % i, method="test", check="t")
               for i in range(4)])
    b = spec(*[c("delete the storage row %d" % i, method="test", check="t")
               for i in range(4)])
    try:
        state = {}
        for _ in range(2):
            sg.run(copy.deepcopy(a), True, state)
        out_b = sg.run(copy.deepcopy(b), True, state)
    finally:
        sg.call_nim = real
        sg.api_key = real_key
    if out_b["outcome"] != sg.OUTCOME_REWORK_SPEC:
        return ["spec B inherited spec A's reject counter: %s" % out_b["outcome"]]
    return []


def test_spec_id_is_stable_and_content_sensitive():
    a, b = sg.spec_id(GOOD_SPEC), sg.spec_id(copy.deepcopy(GOOD_SPEC))
    c_id = sg.spec_id(spec(c("different", method="test", check="t")))
    fails = []
    if a != b:
        fails.append("id not stable across identical specs")
    if a == c_id:
        fails.append("different specs share an id")
    return fails


# --- input handling -----------------------------------------------------------

def test_broken_input_does_not_crash_the_gate():
    """Garbage in, verdict out. A gate that dies is a gate that is skipped."""
    fails = []
    for raw in ({}, {"criteria": "not a list"}, {"criteria": {"a": 1}},
                {"criteria": [None, 42, {"text": "x"}]},
                [], "just a string"):
        try:
            r = sg.run(raw, use_llm=False, state={})
        except Exception as exc:                       # noqa: BLE001
            fails.append("%r raised %s: %s" % (raw, type(exc).__name__, exc))
            continue
        if r["outcome"] not in (sg.OUTCOME_PROCEED, sg.OUTCOME_ESCALATE):
            fails.append("%r -> %s" % (raw, r["outcome"]))
    r = sg.run({}, use_llm=False, state={})
    if r["counts"]["total"] != 0:
        fails.append("empty spec produced criteria: %s" % r["counts"])
    return fails


def test_criteria_from_a_plain_string_are_unverifiable():
    r = _result({"goal": "g", "criteria": ["the note reads well"]})
    crit = r["criteria"][0]
    fails = []
    if crit["status"] != sg.STATUS_UNVERIFIABLE:
        fails.append("status %s" % crit["status"])
    if crit["reason"] != sg.REASON_NO_METHOD:
        fails.append("reason %s" % crit["reason"])
    if crit["text"] != "the note reads well":
        fails.append("text lost: %r" % crit["text"])
    return fails


def test_method_aliases_resolve():
    cases = {"unit test": "test", "pytest": "test", "assert": "invariant",
             "cli": "command", "golden file": "snapshot",
             "human review": "", "human": "manual"}
    fails = []
    for alias, want in cases.items():
        got = sg.normalize_method(alias)
        if got != want:
            fails.append("%r -> %r (want %r)" % (alias, got, want))
    return fails


def test_state_is_fail_open():
    """A broken cache must never block work — only lose the counter."""
    import tempfile

    old = sg.os.environ.get("HERMES_HOME")
    sg.os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="sg-")
    try:
        with open(sg.state_path(), "w", encoding="utf-8") as f:
            f.write("{not json")
        if sg.load_state() != {}:
            return ["corrupt state did not load as empty"]
        sg.save_state({"a": 1})
        if sg.load_state() != {"a": 1}:
            return ["state did not round-trip"]
        r = sg.run(GOOD_SPEC, use_llm=False)
        if r["outcome"] != sg.OUTCOME_PROCEED:
            return ["corrupt cache blocked work: %s" % r["outcome"]]
    finally:
        if old is None:
            sg.os.environ.pop("HERMES_HOME", None)
        else:
            sg.os.environ["HERMES_HOME"] = old
    return []


def test_render_says_the_gate_cannot_verify():
    """SCHEME §10: mechanics never grant verified. The report must not be
    mistakable for a verification."""
    r = _result(GOOD_SPEC)
    text = sg.render(r)
    fails = []
    if "слоя 4" not in text:
        fails.append("render does not point at layer 4")
    if "VERIFIED" not in r["gate_cannot_verify"]:
        fails.append("gate_cannot_verify lost its point")
    for line in text.splitlines():
        if line.startswith("=> гейт пройден") and "VERIFIED" not in line:
            fails.append("pass line implies more than it should: %r" % line)
    return fails


def test_cli_end_to_end_without_network(tmp=None):
    import subprocess
    import tempfile

    tmp = tmp or tempfile.mkdtemp(prefix="sg-cli-")
    path = Path(tmp) / "spec.json"
    path.write_text(json.dumps(GOOD_SPEC, ensure_ascii=False),
                    encoding="utf-8")
    env = dict(__import__("os").environ)
    env["HERMES_HOME"] = tmp
    env.pop("NVIDIA_API_KEY", None)
    proc = subprocess.run(
        [sys.executable, str(GATE), str(path), "--json", "--no-llm"],
        capture_output=True, text=True, timeout=60, env=env)
    fails = []
    if proc.returncode != 0:
        fails.append("exit %d: %s" % (proc.returncode, proc.stderr[-300:]))
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return fails + ["non-JSON stdout: %r" % proc.stdout[:200]]
    if out["outcome"] != sg.OUTCOME_PROCEED:
        fails.append("cli outcome %s" % out["outcome"])
    if out["spec_id"] != sg.spec_id(GOOD_SPEC):
        fails.append("cli spec_id %s" % out["spec_id"])
    text = subprocess.run(
        [sys.executable, str(GATE), str(path), "--no-llm"],
        capture_output=True, text=True, timeout=60, env=env).stdout
    if "SPEC GATE" not in text:
        fails.append("human render missing header")
    return fails


def test_cli_exit_code_signals_escalation():
    import os
    import subprocess
    import tempfile

    tmp = tempfile.mkdtemp(prefix="sg-cli2-")
    path = Path(tmp) / "spec.json"
    path.write_text(json.dumps(spec(c("a"), c("b"), c("c"))),
                    encoding="utf-8")
    env = dict(os.environ, HERMES_HOME=tmp)
    proc = subprocess.run(
        [sys.executable, str(GATE), str(path), "--no-llm"],
        capture_output=True, text=True, timeout=60, env=env)
    if proc.returncode == 0:
        return ["escalation returned exit 0 — a scheduler could not see it"]
    return []


def test_report_bytes_are_utf8():
    """This machine's console is cp866/cp1251 while every reader of the
    output expects UTF-8. Unpinned, Python hands over cp1251 bytes and the
    report arrives as mojibake - a gate nobody can read is a gate nobody
    obeys. Checked on raw bytes, because the local console lies either way.
    """
    import os
    import subprocess
    import tempfile

    tmp = tempfile.mkdtemp(prefix="sg-utf8-")
    path = Path(tmp) / "spec.json"
    path.write_text(json.dumps(spec(c("читаемость", method="test",
                                      check="t"),
                                     c("manual", method="manual",
                                       judge_by="владелец"))),
                    encoding="utf-8")
    env = dict(os.environ, HERMES_HOME=tmp, PYTHONIOENCODING="")
    proc = subprocess.run([sys.executable, str(GATE), str(path), "--no-llm"],
                          capture_output=True, timeout=60, env=env)
    try:
        text = proc.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        return ["stdout is not valid UTF-8 (%s) - pin it in main()" % exc]
    fails = []
    if "читаемость" not in text:
        fails.append("criterion text did not survive the round trip")
    if "judged criteria" not in text:
        fails.append("report tail missing")
    return fails


TESTS = [
    ("auto methods reach the VERIFIED ceiling", test_auto_methods_reach_verified_ceiling),
    ("manual NEVER reaches VERIFIED (invariant 1)", test_manual_never_reaches_verified),
    ("_ceiling has no manual path to VERIFIED", test_ceiling_has_no_manual_path),
    ("declared method without check -> unverifiable", test_method_without_check_is_unverifiable),
    ("no method -> unverifiable, work continues", test_no_method_is_unverifiable_but_work_continues),
    ("unknown method does not become manual", test_unknown_method_does_not_become_manual),
    ("manual without judge_by is demanded", test_manual_without_judging_method_is_flagged),
    (">2 unverifiable escalates (invariant 2)", test_escalation_threshold_is_strictly_above_two),
    ("critical unverifiable escalates regardless of count", test_critical_unverifiable_escalates_independently_of_count),
    ("critical flag accepts strings", test_critical_flag_accepts_strings),
    ("empty criterion escalates", test_empty_criterion_escalates),
    ("judged criteria are named in the report", test_judged_criteria_are_named_explicitly),
    ("all six risk triggers fire", test_risk_triggers_detect_all_six_shapes),
    ("clean spec skips the LLM leg entirely", test_tiny_clean_spec_triggers_no_risk_and_skips_llm),
    ("explicit flag forces a trigger", test_flag_forces_risk_on_demand),
    ("HTTP 200 + empty content is not a verdict", test_review_empty_content_is_not_a_pass),
    ("bad JSON / unknown verdict rejected", test_review_rejects_bad_json_and_unknown_verdict),
    ("401/403/429/451/503 are infra, not spec faults", test_vpn_and_quota_errors_are_infra_not_spec_faults),
    ("infra never produces REWORK_SPEC", test_infra_does_not_rework_the_spec),
    ("paid model refused before the call ($0)", test_paid_model_is_refused_before_the_call),
    ("reviewer params match the measured traps", test_reviewer_params_follow_the_measured_traps),
    ("reject cycles capped at 2, then escalate", test_reject_cycles_are_capped_at_two),
    ("reject counter is per-spec", test_reject_counter_is_keyed_per_spec),
    ("spec id stable and content-sensitive", test_spec_id_is_stable_and_content_sensitive),
    ("broken input never crashes the gate", test_broken_input_does_not_crash_the_gate),
    ("plain-string criteria are unverifiable", test_criteria_from_a_plain_string_are_unverifiable),
    ("method aliases resolve", test_method_aliases_resolve),
    ("state cache is fail-open", test_state_is_fail_open),
    ("report says the gate cannot verify", test_render_says_the_gate_cannot_verify),
    ("cli --json end to end", test_cli_end_to_end_without_network),
    ("cli exit code signals escalation", test_cli_exit_code_signals_escalation),
    ("report bytes are utf-8", test_report_bytes_are_utf8),
]


def main() -> int:
    failed = 0
    for label, fn in TESTS:
        try:
            fails = fn() or []
        except AssertionError as exc:
            fails = ["assertion: %s" % exc]
        except Exception as exc:                       # noqa: BLE001
            fails = ["%s: %s" % (type(exc).__name__, exc)]
        if fails:
            failed += 1
            print("FAIL  %s" % label)
            for line in fails:
                print("        - %s" % line)
        else:
            print("pass  %s" % label)
    print()
    if failed:
        print("%d/%d groups failed" % (failed, len(TESTS)))
        return 1
    print("all pass (%d groups)" % len(TESTS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())