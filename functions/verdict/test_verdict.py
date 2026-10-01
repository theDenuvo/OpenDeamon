"""Тесты Слоя 6.

Здесь важнее всего НЕ дать системе уверенно объявить независимость там, где её
нет, и НЕ отнять результат у пользователя из-за чужой поломки. Обе ошибки
выглядят как «всё сработало».
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_FUNCTIONS = os.path.dirname(_HERE)
for _d in ("", "verdict", "mechanical", "reviewer"):
    _p = os.path.join(_FUNCTIONS, _d) if _d else _HERE
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import verdict as vd  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append((fn.__name__.replace("test_", "").replace("_", " "), fn))
    return fn


CORE = {"provider": "openrouter", "model": "nvidia/nemotron-3-ultra-550b-a55b:free"}
NIM = {"provider": "nvidia", "model": "openai/gpt-oss-20b"}
GROQ = {"provider": "custom", "model": "openai/gpt-oss-120b"}


def mech(passed=True, kind="", action="", status="judged"):
    return {"passed": passed, "kind": kind, "action": action,
            "status_after": status, "reasons": []}


def rev(ok=True, verdict="PASS", findings=None, new_tests=None):
    return {"ok": ok, "verdict": {"verdict": verdict,
                                  "findings": findings or [],
                                  "new_tests": new_tests or []},
            "reasons": [], "error": ""}


# --------------------------------------------------------------------------
# независимость
# --------------------------------------------------------------------------

@test
def test_full_requires_a_different_provider_and_model():
    fails = []
    if vd.independence(CORE, GROQ)["level"] != vd.FULL:
        fails.append("different provider+model is not FULL")
    if vd.independence(CORE, NIM)["level"] != vd.FULL:
        fails.append("core on OpenRouter, reviewer on NIM is not FULL")
    return fails


@test
def test_partial_is_the_same_provider_different_model():
    fails = []
    other = {"provider": "openrouter", "model": "inclusionai/ling-3.0-flash-sante:free"}
    got = vd.independence(CORE, other)
    if got["level"] != vd.PARTIAL:
        fails.append("same provider + different model gave %s" % got["level"])
    if vd.independence(CORE, CORE)["level"] != vd.SEVERE:
        fails.append("identical provider+model is not SEVERE")
    return fails


@test
def test_unknown_provider_is_never_optimistically_full():
    """Допущение об оптимизме - это объявление независимости, которой нет."""
    fails = []
    for core, reviewer in (
            ({"provider": "", "model": CORE["model"]}, NIM),
            (CORE, {"provider": "", "model": NIM["model"]}),
            ({"provider": "openrouter", "model": ""}, NIM),
            (CORE, {"provider": "nvidia", "model": ""})):
        got = vd.independence(core, reviewer)
        if got["level"] != vd.UNKNOWN:
            fails.append("%r vs %r gave %s, expected UNKNOWN"
                         % (core, reviewer, got["level"]))
    return fails


@test
def test_provider_comparison_is_case_and_space_insensitive():
    fails = []
    a = {"provider": " OpenRouter ", "model": "X"}
    b = {"provider": "openrouter", "model": "Y"}
    if vd.independence(a, b)["level"] != vd.PARTIAL:
        fails.append("provider comparison is not normalised")
    return fails


@test
def test_fallback_chain_landing_on_the_core_provider_is_flagged():
    """Ровно текущий дефект конфига: последний элемент review.fallback_chain -
    это openrouter, то есть провайдер ядра. Подстановка происходит в рантайме
    и молча."""
    fails = []
    cfg = {"fallback_chain": [
        {"provider": "custom", "model": "openai/gpt-oss-120b"},
        {"provider": "openrouter", "model": "inclusionai/ling-3.0-flash-sante:free"}]}
    bad = vd.route_is_core_provider(cfg, "openrouter")
    if not bad:
        fails.append("a chain entry on the core provider was not flagged")
    if "fallback_chain[1]" not in " ".join(bad):
        fails.append("the offending index is not named: %s" % bad)
    clean = {"fallback_chain": [{"provider": "custom", "model": "m"}]}
    if vd.route_is_core_provider(clean, "openrouter"):
        fails.append("a clean chain was flagged")
    return fails


@test
def test_collapse_of_both_rails_onto_one_provider_is_detected():
    """OpenRouter упал (ядро на NIM) и Groq упал (ревьюер на NIM) => SEVERE.
    Система обязана это обнаружить, а не заявить независимость."""
    fails = []
    got = vd.collapse_scenario("nvidia", "nvidia")
    if not got.get("collapsed"):
        fails.append("a shared fallback was not detected")
    if got.get("level") != vd.SEVERE:
        fails.append("level %s, expected SEVERE" % got.get("level"))
    if vd.collapse_scenario("nvidia", "custom").get("collapsed"):
        fails.append("different fallbacks were called a collapse")
    if vd.collapse_scenario(None, None).get("collapsed"):
        fails.append("no fallback was called a collapse")
    return fails


# --------------------------------------------------------------------------
# вердикт
# --------------------------------------------------------------------------

@test
def test_severe_caps_the_status_but_still_returns_the_result():
    """SEVERE => task_status != verified, но результат ВЫДАЁТСЯ с пометкой в
    первой строке. Отнимать результат - наказание за чужую поломку."""
    fails = []
    info = vd.independence(CORE, CORE)
    res = vd.decide(mech(), rev(), info)
    if res["task_status"] == "verified":
        fails.append("SEVERE produced a verified status")
    line = vd.first_line(res)
    if vd.SEVERE not in line:
        fails.append("the first line does not carry the SEVERE mark: %r" % line)
    if not res["headline"]:
        fails.append("no headline: the result was withheld entirely")
    return fails


@test
def test_pass_with_a_blocker_finding_is_not_a_pass():
    """Противоречие «PASS» и blocker-а разрешается в REVISE, а не в успех."""
    fails = []
    findings = [{"severity": "blocker", "where": "a.py", "what": "w",
                 "why": "y"}]
    if vd.verdict_of(rev(verdict="PASS", findings=findings)) != "REVISE":
        fails.append("PASS with a blocker was accepted")
    minor = [{"severity": "minor", "where": "a.py", "what": "w", "why": "y"}]
    if vd.verdict_of(rev(verdict="PASS", findings=minor)) != "PASS":
        fails.append("PASS with only minor findings was downgraded")
    return fails


@test
def test_no_reviewer_verdict_is_never_a_pass():
    fails = []
    info = vd.independence(CORE, NIM)
    res = vd.decide(mech(), {"ok": False, "verdict": None, "error": "empty_content"},
                    info)
    if res["task_status"] == "verified":
        fails.append("a missing verdict produced verified")
    if res["review_verdict"] == "PASS":
        fails.append("a missing verdict was reported as PASS")
    if res["kind"] != "REVIEW_UNAVAILABLE":
        fails.append("kind %s" % res["kind"])
    return fails


@test
def test_tampering_keeps_escalate_and_never_rework():
    fails = []
    info = vd.independence(CORE, NIM)
    res = vd.decide(mech(False, kind=vd.__dict__.get("TEST_TAMPERING", "TEST_TAMPERING"),
                         action="ESCALATE"), rev(), info)
    if res["action"] != "ESCALATE":
        fails.append("tampering turned into %s" % res["action"])
    if res["task_status"] == "verified":
        fails.append("tampering produced verified")
    return fails


@test
def test_verified_requires_mechanical_verified_and_a_usable_review():
    fails = []
    info = vd.independence(CORE, GROQ)
    res = vd.decide(mech(True, status="verified"), rev(), info)
    if res["task_status"] != "verified":
        fails.append("FULL + verified + PASS gave %s" % res["task_status"])
    # Потолок Слоя 1: UNVERIFIABLE не даёт verified, даже при FULL и PASS.
    res2 = vd.decide(mech(True, status="judged"), rev(), info)
    if res2["task_status"] == "verified":
        fails.append("judged mechanical produced verified")
    return fails


@test
def test_contract_test_not_named_in_criteria_caps_the_status():
    fails = []
    info = vd.independence(CORE, GROQ)
    review = rev(new_tests=[{"file": "tests/test_new.py", "class": "contract",
                             "why": "covers the rule"}])
    res = vd.decide(mech(True, status="verified"), review, info,
                    criteria="the parser keeps the AST ordering")
    if res["task_status"] == "verified":
        fails.append("an unnamed contract test still yielded verified")
    if not res["contract_tests_missing"]:
        fails.append("the unnamed contract test was not reported")
    ok = vd.decide(mech(True, status="verified"), review, info,
                   criteria="the suite must include tests/test_new.py")
    if ok["task_status"] != "verified":
        fails.append("a named contract test blocked verified: %s"
                     % ok["task_status"])
    return fails


@test
def test_supporting_and_diagnostic_tests_do_not_cap_the_status():
    fails = []
    info = vd.independence(CORE, GROQ)
    for cls in ("supporting", "diagnostic"):
        review = rev(new_tests=[{"file": "tests/t_%s.py" % cls, "class": cls,
                                 "why": "w"}])
        res = vd.decide(mech(True, status="verified"), review, info,
                        criteria="anything")
        if res["task_status"] != "verified":
            fails.append("a %s test capped the status" % cls)
    return fails


@test
def test_collapse_caps_the_status_even_when_the_verdict_is_pass():
    fails = []
    info = vd.independence(CORE, GROQ)
    res = vd.decide(mech(True, status="verified"), rev(), info,
                    collapse=vd.collapse_scenario("nvidia", "nvidia"))
    if res["task_status"] == "verified":
        fails.append("a collapsed pair produced verified")
    if not any("COLLAPSE" in n for n in res["notes"]):
        fails.append("the collapse is not stated: %s" % res["notes"])
    return fails


@test
def test_silent_partial_risk_from_the_chain_is_reported_in_the_verdict():
    fails = []
    info = vd.independence(CORE, GROQ)
    cfg = {"fallback_chain": [{"provider": "openrouter", "model": "ling"}]}
    res = vd.decide(mech(True, status="verified"), rev(), info,
                    core_provider="openrouter", reviewer_cfg=cfg)
    if not any("SILENT PARTIAL" in n for n in res["notes"]):
        fails.append("the core-provider chain entry is not surfaced: %s"
                     % res["notes"])
    return fails


@test
def test_every_result_carries_an_action():
    """Метка без действия бесполезна: именно действие отличает «почини код»
    от «смени маршрут»."""
    fails = []
    info = vd.independence(CORE, NIM)
    cases = [
        (mech(False, kind="MECHANICAL_FAILURE", action="REWORK_CODE"), rev()),
        (mech(False, kind="INFRA_FAILURE", action="CHANGE_ROUTE"), rev()),
        (mech(True), {"ok": False, "verdict": None, "error": "overloaded"}),
        (mech(True), rev(verdict="REVISE")),
        (mech(True), rev(verdict="REJECT")),
        (mech(True), rev()),
    ]
    for m, r in cases:
        res = vd.decide(m, r, info)
        if res["kind"] in ("MECHANICAL_FAILURE", "INFRA_FAILURE") \
                and not res["action"]:
            fails.append("%s produced no action" % res["kind"])
        if res["headline"] and ":" in res["headline"] \
                and not res["headline"].split(":", 1)[1].strip():
            fails.append("empty action in headline %r" % res["headline"])
    return fails


@test
def test_unknown_verdict_string_is_not_pass():
    fails = []
    if vd.verdict_of({"verdict": {"verdict": "looks fine to me"}}) != "REVISE":
        fails.append("an unknown verdict was treated as PASS")
    if vd.verdict_of({}) != "REVISE":
        fails.append("a missing verdict was treated as PASS")
    return fails


@test
def test_broken_input_never_crashes():
    fails = []
    for bad in ({}, {"provider": None}, {"provider": 5, "model": []}):
        try:
            vd.independence(bad, NIM)
            vd.independence(CORE, bad)
        except Exception as exc:  # noqa: BLE001
            fails.append("independence raised on %r: %s" % (bad, exc))
    try:
        vd.route_is_core_provider({"fallback_chain": "nonsense"}, "openrouter")
        vd.route_is_core_provider({}, "")
        vd.collapse_scenario("", "")
        vd.verdict_of({"verdict": {"findings": "text"}})
        vd.contract_tests_missing({"verdict": {}}, "x")
        vd.decide({}, {}, vd.independence(CORE, NIM))
    except Exception as exc:  # noqa: BLE001
        fails.append("a public call raised %s: %s" % (type(exc).__name__, exc))
    return fails


@test
def test_report_bytes_are_utf8():
    fails = []
    res = vd.decide(mech(True, status="verified"), rev(), vd.independence(CORE, CORE))
    try:
        vd.render(res).encode("utf-8")
        json.dumps(res, ensure_ascii=False).encode("utf-8")
    except (UnicodeEncodeError, TypeError) as exc:
        fails.append("result is not UTF-8 safe: %s" % exc)
    return fails


def main() -> int:
    failed = 0
    for label, fn in TESTS:
        try:
            res = fn() or []
            fails = list(res[0]) if isinstance(res, tuple) else list(res)
        except AssertionError as exc:
            fails = ["assertion: %s" % exc]
        except Exception as exc:  # noqa: BLE001
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