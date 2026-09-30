"""Regression tests for the free-rank vision gate.

The bug this pins down (found 2026-09-30 by TODO phase 1.3): score_vision()
read an exclusion flag that a DIFFERENT function set as a side effect, and
only after an early return could skip it. Eligibility therefore depended on
call order, so a model that 404s on images could win the vision role. Its
probe score was 3, the highest available, so nothing else caught it.

These tests run the real rank.py logic on synthetic entries - no network, no
API keys, no writes to config.yaml.

Run: py A:/OpenDeamon/functions/free-rank/test_vision_gate.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RANK = HERE / "rank.py"

# Verbatim ids that return HTTP 404 "No endpoints support image input"
# (STRESS_TEST_REPORT.md, vision table, 2026-09-29).
DEAD_ON_IMAGES = ("nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
                  "google/gemma-4-31b-it:free")
WORKS_ON_IMAGES = "dots-studio/dots-3-note-preview:free"


def load_head():
    """Execute rank.py up to main() to get its module-level helpers."""
    src = RANK.read_text(encoding="utf-8")
    ast.parse(src)                      # syntax must be valid
    head = src.split("def main(")[0]
    ns = {"__name__": "rank_head"}
    exec(compile(head, str(RANK), "exec"), ns)
    return ns


def entry(mid, **kw):
    base = {"id": mid, "tool": True, "reason": True, "ctx": 262144,
            "vision": False, "probe": 0, "aider": False, "status": "ok"}
    base.update(kw)
    return base


def test_vision_usable_rejects_dead():
    ns = load_head()
    vu = ns["vision_usable"]
    deny = ns["VISION_DENY"]
    for mid in DEAD_ON_IMAGES:
        if mid not in deny:
            return [f"{mid} is proven 404-on-image but is NOT in VISION_DENY"]
        if vu(entry(mid, vision=True)):
            return [f"vision_usable({mid}) is True; must be False"]
    return []


def test_vision_usable_accepts_known_good():
    ns = load_head()
    vu = ns["vision_usable"]
    if not vu(entry(WORKS_ON_IMAGES, vision=True)):
        return [f"vision_usable({WORKS_ON_IMAGES}) is False; it passed a live "
                f"image test and must stay eligible"]
    return []


def test_vision_usable_rejects_text_only():
    ns = load_head()
    vu = ns["vision_usable"]
    if vu(entry("nvidia/nemotron-3.5-lightning:free", vision=False)):
        return ["a text-only model was accepted as vision-capable"]
    return []


def test_eligibility_is_order_independent():
    """The core regression: eligibility must not depend on which scorer ran
    first. Fresh dicts every time, so no side effect can leak in."""
    ns = load_head()
    vu = ns["vision_usable"]
    for mid in DEAD_ON_IMAGES:
        a = vu(entry(mid, vision=True))
        b = vu(entry(mid, vision=True))
        if a is not b:
            return [f"{mid}: vision_usable changed between calls "
                    f"({a} then {b}) - state is leaking"]
        if a:
            return [f"{mid}: eligible on a fresh entry"]
    return []


def test_no_side_effect_flags():
    """No scorer may communicate eligibility by mutating the entry."""
    ns = load_head()
    e = entry(DEAD_ON_IMAGES[0], vision=True)
    before = dict(e)
    ns["vision_usable"](e)
    if e != before:
        added = set(e) - set(before)
        return [f"vision_usable mutated the entry (added {added}); eligibility "
                f"must be a pure predicate"]
    return []


def test_deny_list_has_evidence_comment():
    """Each denied id must be justified in the source, so the list does not
    grow on rumour."""
    src = RANK.read_text(encoding="utf-8")
    block = src.split("VISION_DENY = {", 1)[-1].split("}", 1)[0]
    for mid in DEAD_ON_IMAGES:
        if mid not in block:
            return [f"{mid} missing from VISION_DENY block"]
    if "404" not in block and "404" not in src[:src.find("def vision_usable")]:
        return ["VISION_DENY cites no evidence (expected a 404 reference)"]
    return []


def test_vision_chain_excludes_denied():
    """vision_order, the list that becomes the fallback chain, must not
    contain a denied model."""
    ns = load_head()
    vu = ns["vision_usable"]
    table = [entry(m, vision=True, probe=3, ctx=262144) for m in DEAD_ON_IMAGES]
    table.append(entry(WORKS_ON_IMAGES, vision=True, probe=0, ctx=512000))
    order = [e["id"] for e in sorted(table, key=lambda e: ctx(e), reverse=True)
             if vu(e)]
    leaked = [m for m in DEAD_ON_IMAGES if m in order]
    if leaked:
        return [f"vision chain contains denied models: {leaked}"]
    if WORKS_ON_IMAGES not in order:
        return ["vision chain dropped the only proven model"]
    return []


def ctx(e):
    return e["ctx"] or 0


def test_writes_nothing_by_default():
    """A dry run must not rewrite config.yaml; that is what --apply is for."""
    src = RANK.read_text(encoding="utf-8")
    dry = src.split("if not apply:")[1].split("return")[0]
    if "apply(" in dry or "HERMES_CFG, \"w\"" in dry:
        return ["the dry-run branch appears to write files"]
    return []


TESTS = [
    ("denied models excluded from vision", test_vision_usable_rejects_dead),
    ("proven model stays eligible", test_vision_usable_accepts_known_good),
    ("text-only model rejected", test_vision_usable_rejects_text_only),
    ("eligibility order-independent", test_eligibility_is_order_independent),
    ("vision_usable is pure", test_no_side_effect_flags),
    ("deny list cites evidence", test_deny_list_has_evidence_comment),
    ("vision chain clean", test_vision_chain_excludes_denied),
    ("dry run writes nothing", test_writes_nothing_by_default),
]


def main() -> int:
    failed = 0
    for label, fn in TESTS:
        try:
            fails = fn() or []
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
